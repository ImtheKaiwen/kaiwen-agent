"""Package 4: side-effect admission through fenced action intents (ADR 0016).

Real `Agent -> ToolExecutor -> SQLite action intents` under a real fenced `WorkflowController`,
with a scripted model. Ownership is lost at exact points by advancing the injected clock.

What is established: a stale owner cannot be *admitted* to start a new side-effecting handler, and
cannot record the outcome of one that is already running; such an intent stays EXECUTING and
recovery marks it uncertain, after which a restarted run blocks instead of repeating the effect.
What is not: a handler that was already admitted keeps running and its external effect cannot be
undone, and arbitrary worker code that does not use the platform tool executor is not covered.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from kaiwen_agent.exceptions import FencedConfigurationError, OwnershipLostError
from kaiwen_agent.runtime import (
    DurableRuntimeStores,
    RuntimeRecoveryManager,
    RuntimeRunStatus,
)
from kaiwen_agent.standards import StandardResolution
from kaiwen_agent.tools import ToolExecutor
from kaiwen_agent.tools.durability import (
    ActionIntentStatus,
    DurableActionScope,
    FencedActionIntents,
)
from kaiwen_agent.types import ModelResponse
from kaiwen_agent.workflow import TaskIdentityError
from kaiwen_agent.workflow.context import trusted_action_scope
from tests.test_agent_runtime_worker import owner, plan, spec
from tests.test_durable_action_scope import (
    Effects,
    Script,
    add_worker,
    context_for,
    intents,
    single_call,
    tool_call,
    wait_started,
)
from tests.test_fenced_writes import TTL, FencedFlow, run_async

RETRY = {"retry_policy": {"max_attempts": 2, "retry_on": ["execution_error"]}}


async def go(flow: FencedFlow, run: Any, workflow: Any, **options: Any) -> Any:
    return await asyncio.wait_for(
        flow.fenced_controller(**options).execute(
            run=run,
            identity=owner(),
            plan=workflow,
            standards=StandardResolution(pack_refs=(), requirements=(), prompt_refs=()),
            protected_input_fingerprint="objective-1",
        ),
        timeout=30,
    )


async def statuses(flow: FencedFlow, run: Any) -> list[ActionIntentStatus]:
    return [item.status for item in await intents(flow, run.id)]


def lose_around(flow: FencedFlow, *, before: bool, status: ActionIntentStatus) -> None:
    """Make the clock pass the lease expiry just before or just after an intent transition."""

    original = flow.store.transition_action_intent

    async def wrapper(**kwargs: Any) -> Any:
        hit = kwargs.get("status") is status and ActionIntentStatus.PREPARED in kwargs["expected"]
        if before and hit:
            flow.clock.advance(TTL + 1)
        result = await original(**kwargs)
        if not before and hit:
            flow.clock.advance(TTL + 1)
        return result

    flow.store.transition_action_intent = wrapper  # type: ignore[method-assign]


def test_a_fenced_worker_action_is_admitted_and_recorded_under_the_epoch(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        effects = Effects()
        add_worker(flow, "w", Script(tool_call("call-1")), effects)
        run = await flow.new_run()
        outcome = await go(flow, run, plan(spec("t", "w")))
        assert outcome.status.value == "completed" and effects.count == 1
        assert await statuses(flow, run) == [ActionIntentStatus.SUCCEEDED]

    run_async(scenario)


def test_ownership_lost_before_the_claim_means_the_handler_never_starts(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        effects = Effects()
        add_worker(flow, "w", Script(tool_call("call-1")), effects)
        run = await flow.new_run()
        lose_around(flow, before=True, status=ActionIntentStatus.EXECUTING)
        with pytest.raises(OwnershipLostError):
            await go(flow, run, plan(spec("t", "w")))
        assert effects.count == 0  # no side effect was authorized for the stale owner
        assert await statuses(flow, run) == [ActionIntentStatus.PREPARED]  # never EXECUTING

    run_async(scenario)


def test_ownership_lost_after_the_claim_cannot_record_a_receipt_and_is_never_retried(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        effects = Effects()
        model = Script(tool_call("call-1"), ModelResponse(text="done"))
        add_worker(flow, "w", model, effects)
        run = await flow.new_run()
        lose_around(flow, before=False, status=ActionIntentStatus.EXECUTING)
        with pytest.raises(OwnershipLostError):
            await go(flow, run, plan(spec("t", "w", **RETRY)))
        # The handler was admitted before the loss (a documented limit) and ran exactly once.
        assert effects.count == 1
        # Its outcome could not be committed: not SUCCEEDED, not FAILED, not a false UNCERTAIN.
        assert await statuses(flow, run) == [ActionIntentStatus.EXECUTING]

    run_async(scenario)


def test_ownership_lost_while_the_handler_runs_leaves_the_intent_for_recovery(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        effects = Effects(hold=True)
        add_worker(flow, "w", Script(tool_call("call-1"), ModelResponse(text="done")), effects)
        run = await flow.new_run()
        running = asyncio.ensure_future(go(flow, run, plan(spec("t", "w"))))
        await wait_started(effects)
        flow.clock.advance(TTL + 1)  # the lease expires while the external call is in flight
        effects.release.set()
        with pytest.raises(OwnershipLostError):
            await running
        assert effects.count == 1
        assert await statuses(flow, run) == [ActionIntentStatus.EXECUTING]
        assert await flow.status(run) is RuntimeRunStatus.RUNNING  # nothing recorded for the run

        # Recovery (a new process with the lease authority) marks the action uncertain and blocks.
        restarted = flow.store
        report = await RuntimeRecoveryManager(
            stores=DurableRuntimeStores(runs=restarted, actions=restarted, approvals=restarted),
            leases=flow.leases,
        ).reconcile(owner_id="process-2")
        assert len(report.uncertain_action_ids) == 1 and report.blocked_run_ids == (run.id,)
        assert await statuses(flow, run) == [ActionIntentStatus.UNCERTAIN]

        # The restarted run blocks on the unresolved action instead of repeating the effect.
        outcome = await go(flow, run, plan(spec("t", "w")))
        assert outcome.status.value == "blocked"
        assert (outcome.reason or "").startswith("uncertain_action:")
        assert effects.count == 1  # not executed again

    run_async(scenario)


def test_a_platform_tool_call_without_the_epoch_cannot_be_admitted_on_a_fenced_run(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        run = await flow.new_run()
        await flow.owned(run)  # the run is RUNNING and fenced
        effects = Effects()
        executor = ToolExecutor(action_intent_store=flow.store)
        scope = DurableActionScope(
            runtime_run_id=run.id,
            task_id="task-1",
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
        )
        # Model-authored "authority" in the arguments or the context metadata grants nothing.
        call = single_call()
        call = call.model_copy(update={"arguments": {"value": 1}})
        context = context_for(runtime_run_id=run.id, owner_epoch="forged", fence=99)
        with pytest.raises(OwnershipLostError) as caught:
            await executor.execute(
                effects.tool(), call, context, execution_scope="r", action_scope=scope
            )
        assert caught.value.code == "fenced_run_requires_epoch"
        assert effects.count == 0

    run_async(scenario)


def test_a_fenced_run_cannot_mint_an_unfenced_action_scope(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        run = await flow.new_run()
        await flow.owned(run)
        record = await flow.runs.get(run_id=run.id, identity=owner())
        assert record is not None
        # Outside the controller's context there is no epoch to bind.
        with pytest.raises(TaskIdentityError):
            trusted_action_scope(flow.task(run), record)

    run_async(scenario)


def test_a_fenced_scope_over_a_store_that_cannot_fence_is_refused_not_downgraded(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        run = await flow.new_run()
        _, epoch = await flow.owned(run)

        class PlainStore:
            is_durable = True  # a durable store that declares no fenced contract

        effects = Effects()
        executor = ToolExecutor(action_intent_store=PlainStore())  # type: ignore[arg-type]
        scope = DurableActionScope(
            runtime_run_id=run.id,
            task_id="task-1",
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            owner_epoch=epoch,
        )
        with pytest.raises(FencedConfigurationError):
            await executor.execute(
                effects.tool(),
                single_call(),
                context_for(runtime_run_id=run.id),
                execution_scope="r",
                action_scope=scope,
            )
        assert effects.count == 0
        with pytest.raises(ValueError):
            FencedActionIntents(PlainStore(), epoch)

    run_async(scenario)


def test_an_action_scope_rejects_an_epoch_of_another_run_or_owner(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        run = await flow.new_run()
        _, epoch = await flow.owned(run)
        with pytest.raises(ValueError):
            DurableActionScope(
                runtime_run_id="runtime_other",
                task_id="t",
                tenant_id="tenant-1",
                user_id="user-1",
                session_id="session-1",
                owner_epoch=epoch,
            )

    run_async(scenario)


async def retried(tmp_path: Path, first: ModelResponse, second: ModelResponse) -> tuple[int, int]:
    flow = FencedFlow(tmp_path)
    effects = Effects()
    model = Script(first, RuntimeError("model transport failed"), second, ModelResponse(text="ok"))
    add_worker(flow, "w", model, effects)
    run = await flow.new_run()
    outcome = await go(flow, run, plan(spec("t", "w", **RETRY)))
    assert outcome.status.value == "completed"
    return effects.count, len(await intents(flow, run.id))


def test_replay_semantics_are_unchanged_by_fencing(tmp_path: Path) -> None:
    """Same call id or same application key: one effect. A new call id and no key: runs again."""

    async def scenario() -> None:
        assert await retried(tmp_path / "same", tool_call("call-A"), tool_call("call-A")) == (1, 1)
        assert await retried(tmp_path / "new", tool_call("call-A"), tool_call("call-B")) == (2, 2)
        keyed = await retried(
            tmp_path / "key",
            tool_call("call-A", key="record-7"),
            tool_call("call-B", key="record-7"),
        )
        assert keyed == (1, 1)

    run_async(scenario)

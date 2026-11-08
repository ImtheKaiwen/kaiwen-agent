"""PR-0 R3: the durable-scope capability contract and same-run unresolved actions.

Signature inspection is not proof that an executor enforces durable ownership, so support is a
declared capability. And an unresolved action of a run must stop that same run from restarting
into a new side effect, whatever tool-call id the new attempt uses.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from kaiwen_agent.context import AgentContext
from kaiwen_agent.persistence import SQLitePersistence
from kaiwen_agent.runtime import AutonomyLevel, RuntimeRunStatus
from kaiwen_agent.tools import ToolExecutor
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.tools.durability import ActionIntentStatus
from kaiwen_agent.tools.results import PassthroughToolResultProcessor, ToolResultProcessingExecutor
from kaiwen_agent.types import ToolCall, ToolResult
from kaiwen_agent.workflow import WorkflowOutcomeStatus
from tests.test_agent_runtime_worker import Flow, owner, plan, spec
from tests.test_durable_action_scope import (
    ALL_STATUSES,
    PERMISSION,
    Effects,
    Script,
    add_worker,
    build,
    context_for,
    execute,
    intents,
    make_scope,
    single_call,
    tool_call,
    wait_started,
)


async def _ignore(event_type: Any, payload: Any) -> None:
    del event_type, payload


class KwargsExecutor(ToolExecutor):
    """Legacy: accepts **kwargs, never forwards or enforces the durable scope."""

    async def execute(  # type: ignore[override]
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: AgentContext,
        *,
        execution_scope: str,
        emit: Any = _ignore,
        **extra: Any,
    ) -> ToolResult:
        return await super().execute(
            definition, call, context, execution_scope=execution_scope, emit=emit
        )


class NamedUndeclaredExecutor(ToolExecutor):
    """Accepts and forwards `action_scope` but does not declare the capability."""

    async def execute(  # type: ignore[override]
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: AgentContext,
        *,
        execution_scope: str,
        emit: Any = _ignore,
        action_scope: Any = None,
    ) -> ToolResult:
        return await super().execute(
            definition,
            call,
            context,
            execution_scope=execution_scope,
            emit=emit,
            action_scope=action_scope,
        )


class DeclaredExecutor(ToolExecutor):
    """Defines `execute` itself, forwards the scope and declares the capability."""

    supports_action_scope = True

    async def execute(  # type: ignore[override]
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: AgentContext,
        *,
        execution_scope: str,
        emit: Any = _ignore,
        action_scope: Any = None,
    ) -> ToolResult:
        return await super().execute(
            definition,
            call,
            context,
            execution_scope=execution_scope,
            emit=emit,
            action_scope=action_scope,
        )


class DeclaredByParentOnly(NamedUndeclaredExecutor):
    """Declares the capability but inherits an `execute` it did not vouch for."""

    supports_action_scope = True


class InheritingExecutor(ToolExecutor):
    """Does not override `execute`, so it inherits the enforcing implementation."""


# ---------------------------------------------------------------- capability contract


def test_a_kwargs_executor_is_rejected_before_any_tool_runs_and_before_it_was_misattributed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        flow = Flow(tmp_path)
        effects = Effects()
        runtime = flow.runtime(
            Script(tool_call()),  # type: ignore[arg-type]
            tools=(effects.tool(),),
            permissions=frozenset({PERMISSION}),
        )
        runtime.tool_executor = KwargsExecutor(action_intent_store=flow.store)
        identity = owner().model_copy(update={"permissions": frozenset({PERMISSION})})
        with pytest.raises(TypeError, match="durable action scope"):
            await runtime.action(
                "go",
                identity=identity,
                tool_names=("demo.write",),
                autonomy=AutonomyLevel.AUTONOMOUS,
                action_scope=make_scope(),
            )
        assert effects.count == 0
        assert await flow.store.list_action_intents(statuses=ALL_STATUSES, limit=10) == []

    asyncio.run(scenario())


def test_the_decorator_rejects_kwargs_and_undeclared_executors_for_a_scope(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "s.sqlite3")
        for executor in (
            KwargsExecutor(action_intent_store=store),
            NamedUndeclaredExecutor(action_intent_store=store),
            DeclaredByParentOnly(action_intent_store=store),
        ):
            effects = Effects()
            wrapped = ToolResultProcessingExecutor(executor, PassthroughToolResultProcessor())
            with pytest.raises(TypeError, match="durable action scope"):
                await wrapped.execute(
                    effects.tool(),
                    single_call(),
                    context_for(),
                    execution_scope="run_x",
                    action_scope=make_scope(),
                )
            assert effects.count == 0
        assert await store.list_action_intents(statuses=ALL_STATUSES, limit=10) == []

    asyncio.run(scenario())


def test_declared_and_inherited_support_is_accepted_and_owned_by_the_durable_run(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        for index, cls in enumerate((ToolExecutor, DeclaredExecutor, InheritingExecutor)):
            store = SQLitePersistence(tmp_path / f"s{index}.sqlite3")
            effects = Effects()
            wrapped = ToolResultProcessingExecutor(
                cls(action_intent_store=store), PassthroughToolResultProcessor()
            )
            await wrapped.execute(
                effects.tool(),
                single_call(),
                context_for(),
                execution_scope="run_x",
                action_scope=make_scope(),
            )
            (stored,) = await store.list_action_intents(statuses=ALL_STATUSES, limit=10)
            assert stored.runtime_run_id == make_scope().runtime_run_id
            assert effects.count == 1

    asyncio.run(scenario())


def test_standalone_execution_keeps_working_for_every_executor_shape(tmp_path: Path) -> None:
    async def scenario() -> None:
        for index, cls in enumerate(
            (KwargsExecutor, NamedUndeclaredExecutor, DeclaredExecutor, InheritingExecutor)
        ):
            store = SQLitePersistence(tmp_path / f"s{index}.sqlite3")
            effects = Effects()
            wrapped = ToolResultProcessingExecutor(
                cls(action_intent_store=store), PassthroughToolResultProcessor()
            )
            result = await wrapped.execute(
                effects.tool(), single_call(), context_for(), execution_scope="run_x"
            )
            assert result.output == {"ok": True} and effects.count == 1
            (stored,) = await store.list_action_intents(statuses=ALL_STATUSES, limit=10)
            assert stored.runtime_run_id == "run_x"

    asyncio.run(scenario())


# ---------------------------------------------------------------- same-run unresolved actions


async def interrupt_first_attempt(flow: Flow, effects: Effects, run: Any) -> None:
    """Run 1 holds a side effect in flight and is cancelled: BLOCKED with an UNCERTAIN action."""

    running = asyncio.create_task(execute(flow, run, plan(spec("t", "w"))))
    await wait_started(effects)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    record = await flow.runs.get(run_id=run.id, identity=owner())
    assert record is not None and record.status == RuntimeRunStatus.BLOCKED
    (stored,) = await intents(flow, run.id)
    assert stored.status == ActionIntentStatus.UNCERTAIN


def test_a_blocked_run_with_an_uncertain_action_cannot_restart_into_a_new_side_effect(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        flow, _ = build(tmp_path)
        effects = Effects(hold=True)
        add_worker(flow, "w", Script(tool_call("call-1")), effects)
        run = await flow.new_run()
        await interrupt_first_attempt(flow, effects, run)
        assert effects.count == 1

        # The restart's model uses a different call id: that is not a different operation.
        effects.hold = False
        flow.workers = type(flow.workers)()
        flow.registered.clear()
        add_worker(flow, "w", Script(tool_call("call-2")), effects)
        blocked = await execute(flow, run, plan(spec("t", "w")))
        assert blocked.status == WorkflowOutcomeStatus.BLOCKED
        assert (blocked.reason or "").startswith("uncertain_action:")
        assert effects.count == 1  # no second side effect
        record = await flow.runs.get(run_id=run.id, identity=owner())
        assert record is not None and record.status == RuntimeRunStatus.BLOCKED

    asyncio.run(scenario())


def test_an_operator_resolution_allows_the_blocked_run_to_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow, _ = build(tmp_path)
        effects = Effects(hold=True)
        add_worker(flow, "w", Script(tool_call("call-1")), effects)
        run = await flow.new_run()
        await interrupt_first_attempt(flow, effects, run)
        (stored,) = await intents(flow, run.id)

        effects.hold = False
        flow.workers = type(flow.workers)()
        flow.registered.clear()
        add_worker(flow, "w", Script(tool_call("call-2")), effects)
        resolved = await flow.store.transition_action_intent(
            intent_id=stored.id,
            tenant_id="tenant-1",
            user_id="user-1",
            expected=frozenset({ActionIntentStatus.UNCERTAIN}),
            status=ActionIntentStatus.CANCELLED,
            error_code="operator_reconciled",
        )
        assert resolved is not None
        done = await execute(flow, run, plan(spec("t", "w")))
        assert done.status == WorkflowOutcomeStatus.COMPLETED
        assert effects.count == 2

    asyncio.run(scenario())


def test_an_unrelated_quality_blocked_run_without_actions_can_still_restart(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        flow, _ = build(tmp_path)
        effects = Effects()
        add_worker(flow, "w", Script(tool_call("call-1")), effects)
        run = await flow.new_run()
        first = await execute(flow, run, plan(spec("t", "w")))
        assert first.status == WorkflowOutcomeStatus.COMPLETED
        other = await flow.new_run()
        await flow.runs.start(run_id=other.id, identity=owner())
        await flow.runs.block(run_id=other.id, identity=owner(), reason="quality_blocked")
        again = await execute(flow, other, plan(spec("t", "w")))
        assert again.status == WorkflowOutcomeStatus.COMPLETED

    asyncio.run(scenario())


def test_a_same_id_action_of_another_owner_does_not_block_this_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        from tests.test_durable_action_scope import legacy_intent

        flow, _ = build(tmp_path)
        effects = Effects()
        add_worker(flow, "w", Script(tool_call("call-1")), effects)
        run = await flow.new_run()
        await legacy_intent(flow.store, user="user-2", intent_id="intent_other", run_id=run.id)
        outcome = await execute(flow, run, plan(spec("t", "w")))
        assert outcome.status == WorkflowOutcomeStatus.COMPLETED and effects.count == 1

    asyncio.run(scenario())


def test_an_incomplete_or_failed_action_lookup_blocks_the_restart(tmp_path: Path) -> None:
    class Failing:
        def __init__(self, inner: SQLitePersistence) -> None:
            self.inner = inner

        async def list_run_action_intents(self, **_: Any) -> Any:
            raise RuntimeError("store unavailable")

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

    async def scenario() -> None:
        flow, _ = build(tmp_path)
        effects = Effects()
        add_worker(flow, "w", Script(tool_call("call-1")), effects)
        run = await flow.new_run()
        outcome = await execute(flow, run, plan(spec("t", "w")), action_intents=Failing(flow.store))
        assert outcome.status == WorkflowOutcomeStatus.BLOCKED
        assert outcome.reason == "uncertain_action:unverified"
        assert effects.count == 0

    asyncio.run(scenario())


def test_a_safely_cancelled_run_cannot_restart_at_all(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow, _ = build(tmp_path)
        effects = Effects()
        add_worker(flow, "w", Script(tool_call("call-1")), effects)
        run = await flow.new_run()
        await flow.runs.start(run_id=run.id, identity=owner())
        await flow.runs.cancel(run_id=run.id, identity=owner())
        with pytest.raises(ValueError, match="cannot transition"):
            await execute(flow, run, plan(spec("t", "w")))
        assert effects.count == 0

    asyncio.run(scenario())


def test_instance_overrides_and_wrapped_unsupported_executors_are_not_vouched_for(
    tmp_path: Path,
) -> None:
    from kaiwen_agent.tools.execution import executor_supports_action_scope

    store = SQLitePersistence(tmp_path / "s.sqlite3")
    assert executor_supports_action_scope(ToolExecutor())
    assert executor_supports_action_scope(DeclaredExecutor())
    assert not executor_supports_action_scope(KwargsExecutor())
    patched = ToolExecutor(action_intent_store=store)
    patched.execute = KwargsExecutor().execute  # type: ignore[method-assign]
    assert not executor_supports_action_scope(patched)
    wrapper = ToolResultProcessingExecutor(KwargsExecutor(), PassthroughToolResultProcessor())
    assert not executor_supports_action_scope(wrapper)  # the inner executor decides too
    assert executor_supports_action_scope(
        ToolResultProcessingExecutor(DeclaredExecutor(), PassthroughToolResultProcessor())
    )


# ---------------------------------------------------------------- legacy inventory


def _recovery(store: SQLitePersistence) -> Any:
    from kaiwen_agent.coordination import InMemoryLeaseStore
    from kaiwen_agent.runtime import DurableRuntimeStores, RuntimeRecoveryManager

    return RuntimeRecoveryManager(
        stores=DurableRuntimeStores(runs=store, actions=store, approvals=store),
        leases=InMemoryLeaseStore(),
    )


def test_an_empty_recovery_report_does_not_prove_there_is_nothing_to_reconcile(
    tmp_path: Path,
) -> None:
    from tests.test_durable_action_scope import legacy_intent

    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "s.sqlite3")
        # Already uncertain before any recovery pass (for example an earlier startup).
        await legacy_intent(store, status=ActionIntentStatus.UNCERTAIN)
        recovery = _recovery(store)
        report = await recovery.reconcile(owner_id="process-2")
        assert report.uncertain_action_ids == ()
        assert report.unassociated_action_ids == ()  # the report is not an inventory
        inventory = await recovery.inventory_unassociated_actions(
            tenant_id="tenant-1", user_id="user-1"
        )
        assert inventory.action_ids == ("intent_legacy",) and inventory.complete is True

    asyncio.run(scenario())


def test_the_inventory_is_owner_scoped_read_only_and_never_infers_a_run(tmp_path: Path) -> None:
    from tests.test_durable_action_scope import legacy_intent

    async def scenario() -> None:
        flow, _ = build(tmp_path)
        store = flow.store
        known = await flow.new_run()
        await legacy_intent(store, intent_id="intent_a")
        await legacy_intent(store, intent_id="intent_b", status=ActionIntentStatus.UNCERTAIN)
        await legacy_intent(store, intent_id="intent_other", user="user-2")
        await legacy_intent(store, intent_id="intent_known", run_id=known.id)
        recovery = _recovery(store)
        inventory = await recovery.inventory_unassociated_actions(
            tenant_id="tenant-1", user_id="user-1"
        )
        assert inventory.action_ids == ("intent_a", "intent_b")
        other = await recovery.inventory_unassociated_actions(
            tenant_id="tenant-1", user_id="user-2"
        )
        assert other.action_ids == ("intent_other",)
        empty = await recovery.inventory_unassociated_actions(
            tenant_id="tenant-2", user_id="user-1"
        )
        assert empty.action_ids == ()
        # Read-only: nothing was transitioned or rewritten.
        untouched = await store.get_action_intent(
            intent_id="intent_a", tenant_id="tenant-1", user_id="user-1"
        )
        assert untouched is not None and untouched.status == ActionIntentStatus.EXECUTING
        assert untouched.runtime_run_id == "run_legacyinvocation"

        # The opt-in guard and the inventory agree.
        effects = Effects()
        add_worker(flow, "w", Script(tool_call("call-1")), effects)
        run = await flow.new_run()
        blocked = await execute(flow, run, plan(spec("t", "w")), block_unassociated_actions=True)
        assert blocked.reason == f"unassociated_action:{inventory.action_ids[0]}"

    asyncio.run(scenario())


def test_a_truncated_inventory_says_it_is_incomplete(tmp_path: Path) -> None:
    from tests.test_durable_action_scope import legacy_intent

    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "s.sqlite3")
        for index in range(3):
            await legacy_intent(store, intent_id=f"intent_{index}")
        recovery = _recovery(store)
        small = await recovery.inventory_unassociated_actions(
            tenant_id="tenant-1", user_id="user-1", limit=3
        )
        assert small.complete is False  # a full page may hide more
        full = await recovery.inventory_unassociated_actions(
            tenant_id="tenant-1", user_id="user-1", limit=10
        )
        assert full.complete is True and len(full.action_ids) == 3
        with pytest.raises(ValueError):
            await recovery.inventory_unassociated_actions(
                tenant_id="tenant-1", user_id="user-1", limit=0
            )

    asyncio.run(scenario())


def test_a_resolved_action_leaves_the_inventory(tmp_path: Path) -> None:
    from tests.test_durable_action_scope import legacy_intent

    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "s.sqlite3")
        await legacy_intent(store, status=ActionIntentStatus.UNCERTAIN)
        recovery = _recovery(store)
        await store.transition_action_intent(
            intent_id="intent_legacy",
            tenant_id="tenant-1",
            user_id="user-1",
            expected=frozenset({ActionIntentStatus.UNCERTAIN}),
            status=ActionIntentStatus.FAILED,
            error_code="operator_reconciled",
        )
        inventory = await recovery.inventory_unassociated_actions(
            tenant_id="tenant-1", user_id="user-1"
        )
        assert inventory.action_ids == ()

    asyncio.run(scenario())


# ------------------------------------------------ start-of-run guard: EXECUTING orphans (R3)
#
# Invariant: when a controller starts a run, no EXECUTING or UNCERTAIN action owned by that run
# may exist. The run is not RUNNING at that point and, with a lease store, the controller holds
# the exclusive run lease, so such an action belongs to a dead owner and its outcome is unknown.
# The controller never runs `RuntimeRecoveryManager.reconcile` itself, so it cannot rely on it.


def test_an_orphaned_executing_action_of_the_same_run_blocks_the_start_without_recovery(
    tmp_path: Path,
) -> None:
    from tests.test_durable_action_scope import legacy_intent

    async def scenario() -> None:
        flow, _ = build(tmp_path)
        effects = Effects()
        add_worker(flow, "w", Script(tool_call("call-9")), effects)
        run = await flow.new_run()
        # A crash left the action EXECUTING; no recovery has run before this start.
        await legacy_intent(
            flow.store,
            status=ActionIntentStatus.EXECUTING,
            intent_id="intent_orphan",
            run_id=run.id,
        )
        outcome = await execute(flow, run, plan(spec("t", "w")))
        assert outcome.status == WorkflowOutcomeStatus.BLOCKED
        assert outcome.reason == "uncertain_action:intent_orphan"
        assert effects.count == 0  # no new side effect was started
        (stored,) = await intents(flow, run.id)
        assert stored.status == ActionIntentStatus.EXECUTING  # the controller never rewrites it
        record = await flow.runs.get(run_id=run.id, identity=owner())
        assert record is not None and record.status == RuntimeRunStatus.BLOCKED

    asyncio.run(scenario())


def test_the_orphan_guard_also_holds_when_this_controller_owns_the_run_lease(
    tmp_path: Path,
) -> None:
    from kaiwen_agent.coordination import InMemoryLeaseStore
    from tests.test_durable_action_scope import legacy_intent

    async def scenario() -> None:
        flow, _ = build(tmp_path)
        effects = Effects()
        add_worker(flow, "w", Script(tool_call("call-9")), effects)
        run = await flow.new_run()
        leases = InMemoryLeaseStore()
        await legacy_intent(
            flow.store,
            status=ActionIntentStatus.EXECUTING,
            intent_id="intent_orphan",
            run_id=run.id,
        )
        outcome = await execute(flow, run, plan(spec("t", "w")), leases=leases)
        assert outcome.status == WorkflowOutcomeStatus.BLOCKED
        assert outcome.reason == "uncertain_action:intent_orphan" and effects.count == 0
        # Fencing is preserved: the blocked run released its lease like any other outcome.
        free = await leases.acquire(key=f"runtime-run:{run.id}", owner_id="next", ttl_seconds=5)
        assert free is not None

    asyncio.run(scenario())


def test_a_live_owner_of_the_run_lease_is_never_overridden_by_a_second_start(
    tmp_path: Path,
) -> None:
    from kaiwen_agent.coordination import InMemoryLeaseStore
    from kaiwen_agent.exceptions import RunOwnershipError
    from tests.test_durable_action_scope import legacy_intent

    async def scenario() -> None:
        flow, _ = build(tmp_path)
        effects = Effects()
        add_worker(flow, "w", Script(tool_call("call-9")), effects)
        run = await flow.new_run()
        leases = InMemoryLeaseStore()
        held = await leases.acquire(key=f"runtime-run:{run.id}", owner_id="live", ttl_seconds=30)
        assert held is not None
        await legacy_intent(
            flow.store,
            status=ActionIntentStatus.EXECUTING,
            intent_id="intent_live",
            run_id=run.id,
        )
        with pytest.raises(RunOwnershipError):
            await execute(flow, run, plan(spec("t", "w")), leases=leases)
        # The live owner's in-flight action and the run were not touched by the contender.
        (stored,) = await intents(flow, run.id)
        assert stored.status == ActionIntentStatus.EXECUTING and effects.count == 0
        record = await flow.runs.get(run_id=run.id, identity=owner())
        assert record is not None and record.status == RuntimeRunStatus.CREATED

    asyncio.run(scenario())


def test_an_executing_action_of_another_verified_run_does_not_block_this_run(
    tmp_path: Path,
) -> None:
    from tests.test_durable_action_scope import legacy_intent

    async def scenario() -> None:
        flow, _ = build(tmp_path)
        effects = Effects()
        add_worker(flow, "w", Script(tool_call("call-1")), effects)
        mine = await flow.new_run()
        active = await flow.new_run()
        await flow.runs.start(run_id=active.id, identity=owner())  # a verified, running run
        await legacy_intent(
            flow.store,
            status=ActionIntentStatus.EXECUTING,
            intent_id="intent_active",
            run_id=active.id,
        )
        outcome = await execute(flow, mine, plan(spec("t", "w")), block_unassociated_actions=True)
        assert outcome.status == WorkflowOutcomeStatus.COMPLETED and effects.count == 1
        # Contrast: the same kind of action whose run cannot be verified does block.
        other = await flow.new_run()
        await legacy_intent(
            flow.store,
            status=ActionIntentStatus.EXECUTING,
            intent_id="intent_orphan",
            run_id="run_unknown_invocation",
        )
        blocked = await execute(flow, other, plan(spec("t", "w")), block_unassociated_actions=True)
        assert blocked.status == WorkflowOutcomeStatus.BLOCKED
        assert blocked.reason == "unassociated_action:intent_orphan"
        assert effects.count == 1

    asyncio.run(scenario())

"""PR-0: durable action ownership of workflow worker tools.

A workflow `AgentRuntimeWorker` runs its tools inside a per-invocation `AgentRun`. The
durable action intent and approval of a side-effecting tool must nevertheless belong to the
durable workflow run, otherwise run-scoped cancellation and startup recovery cannot see the
action. These tests use the real Agent -> ToolExecutor -> SQLite action-intent path with a
deterministic scripted model; no mocks bypass the store.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from kaiwen_agent.context import AgentContext
from kaiwen_agent.coordination import InMemoryLeaseStore
from kaiwen_agent.exceptions import (
    ActionOutcomeUncertainError,
    ApprovalDeniedError,
    DurableApprovalRequiredError,
    IdempotencyConflictError,
)
from kaiwen_agent.persistence import SQLitePersistence
from kaiwen_agent.runtime import (
    AutonomyLevel,
    DurableRuntimeStores,
    RuntimeRecoveryManager,
    RuntimeRunStatus,
)
from kaiwen_agent.tasks import TaskScheduler, TaskStatus
from kaiwen_agent.tools import ToolExecutor
from kaiwen_agent.tools.approval import DurableApprovalGate
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.tools.durability import (
    ActionIntentRecord,
    ActionIntentStatus,
    DurableActionScope,
)
from kaiwen_agent.types import ModelResponse, ToolCall
from kaiwen_agent.workflow import WorkflowController, WorkflowOutcomeStatus
from tests.test_agent_runtime_worker import Flow, owner, plan, spec

ALL_STATUSES = frozenset(ActionIntentStatus)
PERMISSION = "demo.write"


class Payload(BaseModel):
    value: int = 0


class Script:
    """Scripted model: each item is a `ModelResponse` or an exception to raise."""

    def __init__(self, *items: ModelResponse | Exception) -> None:
        self.items = list(items)
        self.calls = 0

    async def respond(self, agent_input, *, context, tools, tool_results, provider_state):  # type: ignore[no-untyped-def]
        self.calls += 1
        if not self.items:
            return ModelResponse(text="done")
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def tool_call(call_id: str = "call-1", *, value: int = 1, key: str | None = None) -> ModelResponse:
    return ModelResponse(
        tool_calls=[
            ToolCall(id=call_id, name="demo.write", arguments={"value": value}, idempotency_key=key)
        ]
    )


class Effects:
    """Counts real side effects and can hold the handler in flight."""

    def __init__(self, *, hold: bool = False) -> None:
        self.count = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.hold = hold

    def tool(self, *, approval: bool = False) -> ToolDefinition:
        async def handler(context: AgentContext, arguments: BaseModel) -> object:
            self.count += 1
            self.started.set()
            if self.hold:
                await self.release.wait()
            return {"ok": True}

        return ToolDefinition(
            name="demo.write",
            description="A side-effecting demo tool.",
            input_model=Payload,
            handler=handler,
            permissions=frozenset({PERMISSION}),
            side_effect="reversible",
            requires_approval=approval,
        )


def build(tmp_path: Path, *, approval: bool = False) -> tuple[Flow, DurableApprovalGate | None]:
    flow = Flow(tmp_path)
    gate = DurableApprovalGate(flow.store) if approval else None
    return flow, gate


def add_worker(
    flow: Flow,
    role: str,
    model: Script,
    effects: Effects,
    *,
    gate: DurableApprovalGate | None = None,
    approval: bool = False,
) -> None:
    runtime = flow.runtime(
        model,  # type: ignore[arg-type]
        tools=(effects.tool(approval=approval),),
        permissions=frozenset({PERMISSION}),
        approval_gate=gate,
        name=role,
    )
    flow.worker(role, runtime, permissions=frozenset({PERMISSION}), tool_names=("demo.write",))


def controller(flow: Flow, **options: Any) -> WorkflowController:
    base = flow.controller()
    return WorkflowController(
        runs=flow.runs,
        compiler=base.compiler,
        scheduler_factory=lambda graph: TaskScheduler(
            graph=graph, workers=flow.workers, store=flow.store, max_concurrency=4
        ),
        completion_gate=base.completion_gate,
        **{"action_intents": flow.store, **options},
    )


async def execute(flow: Flow, run: Any, workflow: Any, **options: Any) -> Any:
    from kaiwen_agent.standards import StandardResolution

    # Bounded: a regression must fail the test, never hang pytest.
    return await asyncio.wait_for(
        controller(flow, **options).execute(
            run=run,
            identity=owner(),
            plan=workflow,
            standards=StandardResolution(pack_refs=(), requirements=(), prompt_refs=()),
            protected_input_fingerprint="objective-1",
        ),
        timeout=30,
    )


async def intents(flow: Flow, run_id: str, **who: str) -> list[ActionIntentRecord]:
    return await flow.store.list_run_action_intents(
        tenant_id=who.get("tenant", "tenant-1"),
        user_id=who.get("user", "user-1"),
        runtime_run_id=run_id,
        statuses=ALL_STATUSES,
    )


async def all_intents(flow: Flow) -> list[ActionIntentRecord]:
    return await flow.store.list_action_intents(statuses=ALL_STATUSES, limit=1_000)


async def wait_started(effects: Effects) -> None:
    await asyncio.wait_for(effects.started.wait(), timeout=10)


# ---------------------------------------------------------------- reproduction (was red)


def test_a_worker_action_intent_belongs_to_the_durable_workflow_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow, _ = build(tmp_path)
        effects = Effects()
        add_worker(flow, "w", Script(tool_call()), effects)
        run = await flow.new_run()
        outcome = await execute(flow, run, plan(spec("t", "w")))
        assert outcome.status == WorkflowOutcomeStatus.COMPLETED
        assert effects.count == 1
        stored = await all_intents(flow)
        assert len(stored) == 1
        # The owning run is the durable workflow run, not the per-invocation AgentRun.
        assert stored[0].runtime_run_id == run.id
        assert not stored[0].runtime_run_id.startswith("run_")
        scoped = await intents(flow, run.id)
        assert [item.id for item in scoped] == [stored[0].id]
        assert scoped[0].status == ActionIntentStatus.SUCCEEDED

    asyncio.run(scenario())


def test_cancelling_a_run_with_an_in_flight_worker_action_is_not_recorded_as_safe(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        flow, _ = build(tmp_path)
        effects = Effects(hold=True)
        add_worker(flow, "w", Script(tool_call()), effects)
        run = await flow.new_run()
        running = asyncio.create_task(execute(flow, run, plan(spec("t", "w"))))
        await wait_started(effects)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        record = await flow.runs.get(run_id=run.id, identity=owner())
        assert record is not None
        # An interrupted side effect has an unknown outcome: the run is blocked, not cancelled.
        assert record.status == RuntimeRunStatus.BLOCKED
        assert (record.blocked_reason or "").startswith("uncertain_action:")
        found = await intents(flow, run.id)
        assert [item.status for item in found] == [ActionIntentStatus.UNCERTAIN]
        # Cancellation left no orphaned worker task behind.
        assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []

    asyncio.run(scenario())


def test_recovery_marks_an_interrupted_worker_action_uncertain_and_blocks_its_run(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        flow, _ = build(tmp_path)
        effects = Effects(hold=True)
        add_worker(flow, "w", Script(tool_call()), effects)
        run = await flow.new_run()
        running = asyncio.create_task(execute(flow, run, plan(spec("t", "w"))))
        await wait_started(effects)
        # A second process starts while the action is still executing in the first.
        restarted = SQLitePersistence(tmp_path / "flow.sqlite3")
        report = await RuntimeRecoveryManager(
            stores=DurableRuntimeStores(runs=restarted, actions=restarted, approvals=restarted),
            leases=InMemoryLeaseStore(),
        ).reconcile(owner_id="process-2")
        assert len(report.uncertain_action_ids) == 1
        assert report.blocked_run_ids == (run.id,)
        assert report.unassociated_action_ids == ()
        durable = await restarted.get_runtime_run(
            run_id=run.id, tenant_id="tenant-1", user_id="user-1"
        )
        assert durable is not None and durable.status == RuntimeRunStatus.BLOCKED
        effects.release.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())


def test_a_pending_worker_approval_is_bound_to_the_durable_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow, gate = build(tmp_path, approval=True)
        effects = Effects()
        add_worker(flow, "w", Script(tool_call()), effects, gate=gate, approval=True)
        run = await flow.new_run()
        outcome = await execute(flow, run, plan(spec("t", "w")))
        assert outcome.status == WorkflowOutcomeStatus.FAILED
        assert outcome.tasks[0].status == TaskStatus.FAILED
        assert effects.count == 0
        (intent,) = await intents(flow, run.id)
        approval = await flow.store.get_approval_for_intent(
            action_intent_id=intent.id, tenant_id="tenant-1", user_id="user-1"
        )
        assert approval is not None
        assert approval.runtime_run_id == run.id
        assert intent.status == ActionIntentStatus.PREPARED

    asyncio.run(scenario())


# ---------------------------------------------------------------- execution identity


def test_two_tasks_in_one_run_do_not_collide_on_a_provider_generated_call_id(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        flow, _ = build(tmp_path)
        effects = Effects()
        # Both tasks receive the same provider-generated call id and the same arguments.
        add_worker(flow, "a", Script(tool_call("call-x")), effects)
        add_worker(flow, "b", Script(tool_call("call-x")), effects)
        run = await flow.new_run()
        outcome = await execute(flow, run, plan(spec("one", "a"), spec("two", "b")))
        assert outcome.status == WorkflowOutcomeStatus.COMPLETED
        assert effects.count == 2
        stored = await intents(flow, run.id)
        assert len(stored) == 2 and len({item.id for item in stored}) == 2
        assert {item.call_id for item in stored} == {"call-x"}
        assert all(item.status == ActionIntentStatus.SUCCEEDED for item in stored)

    asyncio.run(scenario())


def test_two_tasks_with_the_same_role_conversation_and_call_id_do_not_collide(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        flow, _ = build(tmp_path)
        effects = Effects()
        worker_script = Script(tool_call("call-x"), ModelResponse(text="ok"), tool_call("call-x"))
        add_worker(flow, "a", worker_script, effects)
        flow.workers.get("a").private_scope = "role"  # type: ignore[attr-defined]
        run = await flow.new_run()
        outcome = await execute(
            flow, run, plan(spec("one", "a"), spec("two", "a", dependencies=("one",)))
        )
        assert outcome.status == WorkflowOutcomeStatus.COMPLETED
        assert effects.count == 2
        assert len(await intents(flow, run.id)) == 2

    asyncio.run(scenario())


def test_a_retried_task_cannot_repeat_a_keyed_side_effect(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow, _ = build(tmp_path)
        effects = Effects()
        # Attempt 1 performs the effect, then the model call fails. Attempt 2 repeats the
        # same keyed action: it must be answered from the durable receipt.
        model = Script(
            tool_call("call-1", key="record-7"),
            RuntimeError("model transport failed"),
            tool_call("call-9", key="record-7"),
            ModelResponse(text="done"),
        )
        add_worker(flow, "w", model, effects)
        run = await flow.new_run()
        retry = {"retry_policy": {"max_attempts": 2, "retry_on": ["execution_error"]}}
        outcome = await execute(flow, run, plan(spec("t", "w", **retry)))
        assert outcome.tasks[0].attempts == 2
        assert outcome.status == WorkflowOutcomeStatus.COMPLETED
        assert effects.count == 1
        stored = await intents(flow, run.id)
        assert len(stored) == 1 and stored[0].status == ActionIntentStatus.SUCCEEDED

    asyncio.run(scenario())


def test_a_retried_task_with_changed_arguments_cannot_reuse_the_keyed_intent(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        flow, _ = build(tmp_path)
        effects = Effects()
        model = Script(
            tool_call("call-1", value=1, key="record-7"),
            RuntimeError("model transport failed"),
            tool_call("call-9", value=999, key="record-7"),
        )
        add_worker(flow, "w", model, effects)
        run = await flow.new_run()
        retry = {"retry_policy": {"max_attempts": 2, "retry_on": ["execution_error"]}}
        outcome = await execute(flow, run, plan(spec("t", "w", **retry)))
        assert outcome.status == WorkflowOutcomeStatus.FAILED
        assert effects.count == 1  # the changed action never executed
        assert outcome.tasks[0].status == TaskStatus.FAILED

    asyncio.run(scenario())


def test_an_approval_is_not_shared_between_tasks_that_reuse_a_call_id(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow, gate = build(tmp_path, approval=True)
        effects = Effects()
        add_worker(flow, "a", Script(tool_call("call-x")), effects, gate=gate, approval=True)
        add_worker(flow, "b", Script(tool_call("call-x")), effects, gate=gate, approval=True)
        run = await flow.new_run()
        await execute(flow, run, plan(spec("one", "a"), spec("two", "b")))
        stored = await intents(flow, run.id)
        assert len(stored) == 2
        approvals = [
            await flow.store.get_approval_for_intent(
                action_intent_id=item.id, tenant_id="tenant-1", user_id="user-1"
            )
            for item in stored
        ]
        assert all(item is not None for item in approvals)
        assert len({item.id for item in approvals if item is not None}) == 2
        assert effects.count == 0

    asyncio.run(scenario())


def test_owner_isolation_between_tenants_and_runs(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow, _ = build(tmp_path)
        effects = Effects()
        add_worker(flow, "w", Script(tool_call("c"), tool_call("c")), effects)
        run_a = await flow.new_run()
        outcome = await execute(flow, run_a, plan(spec("t", "w")))
        assert outcome.status == WorkflowOutcomeStatus.COMPLETED
        assert len(await intents(flow, run_a.id)) == 1
        assert await intents(flow, run_a.id, tenant="tenant-2") == []
        assert await intents(flow, run_a.id, user="user-2") == []
        run_b = await flow.new_run()
        assert await intents(flow, run_b.id) == []

    asyncio.run(scenario())


def test_a_failed_task_leaves_no_active_worker_or_executing_intent(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow, _ = build(tmp_path)
        effects = Effects()
        add_worker(flow, "w", Script(tool_call(), RuntimeError("boom")), effects)
        run = await flow.new_run()
        outcome = await execute(flow, run, plan(spec("t", "w")))
        assert outcome.status == WorkflowOutcomeStatus.FAILED
        assert [item.status for item in await intents(flow, run.id)] == [
            ActionIntentStatus.SUCCEEDED
        ]
        assert not [
            item
            for item in await flow.store.list_action_intents(
                statuses=frozenset({ActionIntentStatus.EXECUTING}), limit=100
            )
        ]

    asyncio.run(scenario())


def test_standalone_runtime_keeps_its_agent_run_scope(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow, _ = build(tmp_path)
        effects = Effects()
        runtime = flow.runtime(
            Script(tool_call("c1")),  # type: ignore[arg-type]
            tools=(effects.tool(),),
            permissions=frozenset({PERMISSION}),
        )
        result = await runtime.action(
            "do it",
            identity=owner().model_copy(update={"permissions": frozenset({PERMISSION})}),
            tool_names=("demo.write",),
            autonomy=AutonomyLevel.AUTONOMOUS,
        )
        stored = await all_intents(flow)
        assert len(stored) == 1
        assert stored[0].runtime_run_id == result.run_id
        assert effects.count == 1

    asyncio.run(scenario())


# ---------------------------------------------------------------- executor-level guarantees


def make_scope(
    run_id: str = "runtime_run-1", task_id: str = "task-1", **owner_fields: str
) -> DurableActionScope:
    fields = {"tenant_id": "tenant-1", "user_id": "user-1", "session_id": "session-1"}
    return DurableActionScope(runtime_run_id=run_id, task_id=task_id, **{**fields, **owner_fields})


def context_for(**metadata: Any) -> AgentContext:
    return AgentContext(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        conversation_id="conversation-1",
        permissions=frozenset({PERMISSION}),
        metadata=metadata,
    )


def single_call(call_id: str = "call-1", *, value: int = 1, key: str | None = None) -> ToolCall:
    return ToolCall(id=call_id, name="demo.write", arguments={"value": value}, idempotency_key=key)


async def run_intents(store: SQLitePersistence, run_id: str) -> list[ActionIntentRecord]:
    return await store.list_run_action_intents(
        tenant_id="tenant-1", user_id="user-1", runtime_run_id=run_id, statuses=ALL_STATUSES
    )


def test_a_durable_scope_is_validated_not_trusted() -> None:
    valid = {
        "runtime_run_id": "runtime_run-1",
        "task_id": "task-1",
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "session_id": "session-1",
    }
    for field in valid:
        with pytest.raises(ValueError):
            DurableActionScope(**{**valid, field: " "})
    with pytest.raises(ValueError):
        make_scope(task_id="a\x1fb")
    with pytest.raises(ValueError):
        make_scope(run_id="x" * 201)


def test_the_executor_rejects_a_scope_for_another_owner_or_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "s.sqlite3")
        effects = Effects()
        executor = ToolExecutor(action_intent_store=store)
        with pytest.raises(ValueError, match="execution owner"):
            await executor.execute(
                effects.tool(),
                single_call(),
                context_for(),
                execution_scope="r",
                action_scope=make_scope(tenant_id="tenant-2"),
            )
        with pytest.raises(ValueError, match="workflow run"):
            await executor.execute(
                effects.tool(),
                single_call(),
                context_for(runtime_run_id="runtime_other"),
                execution_scope="r",
                action_scope=make_scope(),
            )
        assert effects.count == 0
        assert await store.list_action_intents(statuses=ALL_STATUSES, limit=10) == []

    asyncio.run(scenario())


def test_the_runtime_rejects_a_scope_that_disagrees_with_its_identity(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow = Flow(tmp_path)
        effects = Effects()
        model = Script(tool_call())
        runtime = flow.runtime(
            model,  # type: ignore[arg-type]
            tools=(effects.tool(),),
            permissions=frozenset({PERMISSION}),
        )
        identity = owner().model_copy(update={"permissions": frozenset({PERMISSION})})
        for scope, metadata in (
            (make_scope(user_id="user-2"), {}),
            (make_scope(session_id="session-2"), {}),
            (make_scope(), {"runtime_run_id": "runtime_other"}),
        ):
            with pytest.raises(ValueError):
                await runtime.action(
                    "go",
                    identity=identity.model_copy(update={"metadata": metadata}),
                    tool_names=("demo.write",),
                    autonomy=AutonomyLevel.AUTONOMOUS,
                    action_scope=scope,
                )
        assert model.calls == 0 and effects.count == 0

    asyncio.run(scenario())


def test_context_metadata_cannot_impersonate_a_workflow_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "s.sqlite3")
        effects = Effects()
        victim = "runtime_victim"
        await ToolExecutor(action_intent_store=store).execute(
            effects.tool(),
            single_call(),
            context_for(runtime_run_id=victim),
            execution_scope="run_standalone",
        )
        (stored,) = await store.list_action_intents(statuses=ALL_STATUSES, limit=10)
        assert stored.runtime_run_id == "run_standalone"
        assert await run_intents(store, victim) == []

    asyncio.run(scenario())


def test_concurrent_identical_scoped_actions_are_single_flight(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "s.sqlite3")
        effects = Effects(hold=True)
        executor = ToolExecutor(action_intent_store=store)
        scope = make_scope()

        async def run_once() -> Any:
            return await executor.execute(
                effects.tool(),
                single_call(),
                context_for(),
                execution_scope="run_invocation",
                action_scope=scope,
            )

        first = asyncio.create_task(run_once())
        await wait_started(effects)
        second = asyncio.create_task(run_once())
        await asyncio.sleep(0.05)
        effects.release.set()
        results = await asyncio.gather(first, second)
        assert effects.count == 1
        assert [item.output for item in results] == [{"ok": True}, {"ok": True}]
        assert len(await run_intents(store, scope.runtime_run_id)) == 1

    asyncio.run(scenario())


def test_a_scoped_action_is_replayed_from_its_receipt_across_invocations(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / "s.sqlite3"
        effects = Effects()
        scope = make_scope()
        call = single_call(key="record-1")
        for invocation in ("run_a", "run_b"):
            store = SQLitePersistence(database)  # a restart between the two invocations
            result = await ToolExecutor(action_intent_store=store).execute(
                effects.tool(), call, context_for(), execution_scope=invocation, action_scope=scope
            )
            assert result.output == {"ok": True}
        assert effects.count == 1
        # The same key in another task of the same run is a different action.
        await ToolExecutor(action_intent_store=SQLitePersistence(database)).execute(
            effects.tool(),
            call,
            context_for(),
            execution_scope="run_c",
            action_scope=make_scope(task_id="task-2"),
        )
        assert effects.count == 2

    asyncio.run(scenario())


def test_changed_arguments_cannot_reuse_a_scoped_intent(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "s.sqlite3")
        effects = Effects()
        executor = ToolExecutor(action_intent_store=store)
        scope = make_scope()
        await executor.execute(
            effects.tool(),
            single_call(key="k", value=1),
            context_for(),
            execution_scope="r1",
            action_scope=scope,
        )
        with pytest.raises(IdempotencyConflictError):
            await executor.execute(
                effects.tool(),
                single_call("call-2", key="k", value=2),
                context_for(),
                execution_scope="r2",
                action_scope=scope,
            )
        assert effects.count == 1

    asyncio.run(scenario())


def test_an_interrupted_scoped_action_is_never_silently_retried(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "s.sqlite3")
        effects = Effects(hold=True)
        executor = ToolExecutor(action_intent_store=store)
        scope = make_scope()
        call = single_call(key="k")
        running = asyncio.create_task(
            executor.execute(
                effects.tool(), call, context_for(), execution_scope="r1", action_scope=scope
            )
        )
        await wait_started(effects)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        (stored,) = await run_intents(store, scope.runtime_run_id)
        assert stored.status == ActionIntentStatus.UNCERTAIN
        effects.hold = False
        with pytest.raises(ActionOutcomeUncertainError):
            await executor.execute(
                effects.tool(), call, context_for(), execution_scope="r2", action_scope=scope
            )
        assert effects.count == 1

    asyncio.run(scenario())


def resolve(gate: DurableApprovalGate, approval: Any, *, approved: bool, **override: Any) -> Any:
    fields: dict[str, Any] = {
        "approval_id": approval.id,
        "tenant_id": approval.tenant_id,
        "user_id": approval.user_id,
        "conversation_id": approval.conversation_id,
        "action_intent_id": approval.action_intent_id,
        "tool_name": approval.tool_name,
        "call_id": approval.call_id,
        "decided_by": "admin-1",
        "approved": approved,
    }
    return gate.resolve(**{**fields, **override})


def test_scoped_approval_lifecycle_pending_approved_consumed_and_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / "s.sqlite3"
        effects = Effects()
        scope = make_scope()
        call = single_call(key="k")
        store = SQLitePersistence(database)
        executor = ToolExecutor(action_intent_store=store, approval_gate=DurableApprovalGate(store))
        with pytest.raises(DurableApprovalRequiredError) as pending:
            await executor.execute(
                effects.tool(approval=True),
                call,
                context_for(),
                execution_scope="r1",
                action_scope=scope,
            )
        approval = await store.get_approval(
            approval_id=pending.value.approval_id, tenant_id="tenant-1", user_id="user-1"
        )
        assert approval is not None
        assert approval.runtime_run_id == scope.runtime_run_id  # bound to the durable run
        assert effects.count == 0

        # Restart with the pending approval persisted: still pending, still not executed.
        store = SQLitePersistence(database)
        gate = DurableApprovalGate(store)
        executor = ToolExecutor(action_intent_store=store, approval_gate=gate)
        with pytest.raises(DurableApprovalRequiredError):
            await executor.execute(
                effects.tool(approval=True),
                call,
                context_for(),
                execution_scope="r2",
                action_scope=scope,
            )
        with pytest.raises(KeyError):  # wrong owner
            await resolve(gate, approval, approved=True, user_id="user-2")
        with pytest.raises(KeyError):  # wrong tool
            await resolve(gate, approval, approved=True, tool_name="other.tool")
        with pytest.raises(KeyError):  # wrong intent
            await resolve(gate, approval, approved=True, action_intent_id="intent_x")
        await resolve(gate, approval, approved=True)

        # A different task of the same run with the same call id is not covered.
        with pytest.raises(DurableApprovalRequiredError) as other:
            await executor.execute(
                effects.tool(approval=True),
                call,
                context_for(),
                execution_scope="r3",
                action_scope=make_scope(task_id="task-2"),
            )
        assert other.value.approval_id != approval.id
        assert effects.count == 0

        # The approved task runs exactly once; the receipt answers every later attempt.
        for invocation in ("r4", "r5"):
            result = await executor.execute(
                effects.tool(approval=True),
                call,
                context_for(),
                execution_scope=invocation,
                action_scope=scope,
            )
            assert result.output == {"ok": True}
        assert effects.count == 1

    asyncio.run(scenario())


def test_a_scoped_denial_cancels_the_intent_and_never_executes(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "s.sqlite3")
        gate = DurableApprovalGate(store)
        executor = ToolExecutor(action_intent_store=store, approval_gate=gate)
        effects = Effects()
        scope = make_scope()
        with pytest.raises(DurableApprovalRequiredError) as pending:
            await executor.execute(
                effects.tool(approval=True),
                single_call(),
                context_for(),
                execution_scope="r1",
                action_scope=scope,
            )
        approval = await store.get_approval(
            approval_id=pending.value.approval_id, tenant_id="tenant-1", user_id="user-1"
        )
        assert approval is not None
        await resolve(gate, approval, approved=False)
        with pytest.raises(ApprovalDeniedError):
            await executor.execute(
                effects.tool(approval=True),
                single_call(),
                context_for(),
                execution_scope="r2",
                action_scope=scope,
            )
        assert effects.count == 0
        (stored,) = await run_intents(store, scope.runtime_run_id)
        assert stored.status == ActionIntentStatus.CANCELLED

    asyncio.run(scenario())


def test_legacy_agent_run_scoped_records_are_never_rebound_or_replayed(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "s.sqlite3")
        legacy = ActionIntentRecord(
            id="intent_legacy",
            runtime_run_id="run_legacyinvocation",  # written before durable-run ownership
            tenant_id="tenant-1",
            user_id="user-1",
            conversation_id="conversation-1",
            tool_name="demo.write",
            call_id="call-1",
            arguments_checksum="a" * 64,
            idempotency_key="run_legacyinvocation:call-1",
            side_effect="reversible",
        )
        await store.prepare_action_intent(legacy)
        await store.transition_action_intent(
            intent_id=legacy.id,
            tenant_id="tenant-1",
            user_id="user-1",
            expected=frozenset({ActionIntentStatus.PREPARED}),
            status=ActionIntentStatus.EXECUTING,
        )
        effects = Effects()
        await ToolExecutor(action_intent_store=store).execute(
            effects.tool(),
            single_call("call-1"),
            context_for(),
            execution_scope="run_legacyinvocation",
            action_scope=make_scope(),
        )
        # A new, separately owned intent was created; the legacy one was not touched.
        assert effects.count == 1
        untouched = await store.get_action_intent(
            intent_id="intent_legacy", tenant_id="tenant-1", user_id="user-1"
        )
        assert untouched is not None and untouched.status == ActionIntentStatus.EXECUTING
        assert untouched.runtime_run_id == "run_legacyinvocation"

        # Recovery makes the legacy record uncertain but cannot find an owning run, so it
        # is reported for explicit operator reconciliation rather than silently handled.
        report = await RuntimeRecoveryManager(
            stores=DurableRuntimeStores(runs=store, actions=store, approvals=store),
            leases=InMemoryLeaseStore(),
        ).reconcile(owner_id="process-2")
        assert report.uncertain_action_ids == ("intent_legacy",)
        assert report.unassociated_action_ids == ("intent_legacy",)
        assert report.blocked_run_ids == ()

    asyncio.run(scenario())


# ---------------------------------------------------------------- legacy unresolved actions


async def legacy_intent(
    store: SQLitePersistence,
    *,
    status: ActionIntentStatus = ActionIntentStatus.EXECUTING,
    user: str = "user-1",
    intent_id: str = "intent_legacy",
    run_id: str = "run_legacyinvocation",
) -> ActionIntentRecord:
    """An action written before workflow actions were owned by the durable run."""

    intent = ActionIntentRecord(
        id=intent_id,
        runtime_run_id=run_id,
        tenant_id="tenant-1",
        user_id=user,
        conversation_id="conversation-1",
        tool_name="demo.write",
        call_id="call-1",
        arguments_checksum="a" * 64,
        idempotency_key=f"{run_id}:{intent_id}",
        side_effect="reversible",
    )
    await store.prepare_action_intent(intent)
    await store.transition_action_intent(
        intent_id=intent_id,
        tenant_id="tenant-1",
        user_id=user,
        expected=frozenset({ActionIntentStatus.PREPARED}),
        status=ActionIntentStatus.EXECUTING,
    )
    if status == ActionIntentStatus.UNCERTAIN:
        await store.transition_action_intent(
            intent_id=intent_id,
            tenant_id="tenant-1",
            user_id=user,
            expected=frozenset({ActionIntentStatus.EXECUTING}),
            status=ActionIntentStatus.UNCERTAIN,
            error_code="process_interrupted",
        )
    return intent


def test_without_the_guard_an_identical_action_runs_beside_an_unresolved_legacy_one(
    tmp_path: Path,
) -> None:
    """Documents a limit: provenance is missing, so duplicate prevention is not automatic."""

    async def scenario() -> None:
        flow, _ = build(tmp_path)
        await legacy_intent(flow.store)
        effects = Effects()
        add_worker(flow, "w", Script(tool_call("call-1")), effects)
        run = await flow.new_run()
        outcome = await execute(flow, run, plan(spec("t", "w")))
        assert outcome.status == WorkflowOutcomeStatus.COMPLETED
        assert effects.count == 1  # guarantee 3 is NOT provided by default
        untouched = await flow.store.get_action_intent(
            intent_id="intent_legacy", tenant_id="tenant-1", user_id="user-1"
        )
        assert untouched is not None and untouched.status == ActionIntentStatus.EXECUTING

    asyncio.run(scenario())


def test_the_guard_blocks_work_until_an_operator_resolves_the_legacy_action(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        flow, _ = build(tmp_path)
        await legacy_intent(flow.store)
        effects = Effects()
        add_worker(flow, "w", Script(tool_call("call-1")), effects)
        run = await flow.new_run()
        blocked = await execute(flow, run, plan(spec("t", "w")), block_unassociated_actions=True)
        assert blocked.status == WorkflowOutcomeStatus.BLOCKED
        assert blocked.reason == "unassociated_action:intent_legacy"
        assert effects.count == 0 and blocked.tasks == ()
        record = await flow.runs.get(run_id=run.id, identity=owner())
        assert record is not None and record.status == RuntimeRunStatus.BLOCKED

        # Recovery makes the interrupted record uncertain and reports it; that alone does not
        # resolve it, so a new run is still refused.
        report = await RuntimeRecoveryManager(
            stores=DurableRuntimeStores(runs=flow.store, actions=flow.store, approvals=flow.store),
            leases=InMemoryLeaseStore(),
        ).reconcile(owner_id="operator")
        assert report.unassociated_action_ids == ("intent_legacy",)
        second = await flow.new_run()
        again = await execute(flow, second, plan(spec("t", "w")), block_unassociated_actions=True)
        assert again.reason == "unassociated_action:intent_legacy" and effects.count == 0

        # The operator verified the external system and records the outcome explicitly.
        resolved = await flow.store.transition_action_intent(
            intent_id="intent_legacy",
            tenant_id="tenant-1",
            user_id="user-1",
            expected=frozenset({ActionIntentStatus.UNCERTAIN}),
            status=ActionIntentStatus.CANCELLED,
            error_code="operator_reconciled",
        )
        assert resolved is not None and resolved.status == ActionIntentStatus.CANCELLED
        third = await flow.new_run()
        done = await execute(flow, third, plan(spec("t", "w")), block_unassociated_actions=True)
        assert done.status == WorkflowOutcomeStatus.COMPLETED and effects.count == 1

    asyncio.run(scenario())


def test_the_guard_ignores_other_owners_and_actions_of_known_runs(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow, _ = build(tmp_path)
        effects = Effects()
        add_worker(flow, "w", Script(tool_call("call-1")), effects)
        await legacy_intent(flow.store, user="user-2", intent_id="intent_other_owner")
        run = await flow.new_run()
        other_run = await flow.new_run()  # a real durable run owns this in-flight action
        await legacy_intent(flow.store, intent_id="intent_known_run", run_id=other_run.id)
        outcome = await execute(flow, run, plan(spec("t", "w")), block_unassociated_actions=True)
        assert outcome.status == WorkflowOutcomeStatus.COMPLETED
        assert effects.count == 1

    asyncio.run(scenario())


def test_the_guard_fails_closed_without_an_action_store_or_on_lookup_errors(
    tmp_path: Path,
) -> None:
    class Failing:
        def __init__(self, inner: SQLitePersistence) -> None:
            self.inner = inner

        async def list_action_intents(self, **_: Any) -> Any:
            raise RuntimeError("store unavailable")

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

    async def scenario() -> None:
        flow, _ = build(tmp_path)
        effects = Effects()
        add_worker(flow, "w", Script(tool_call("call-1")), effects)
        run = await flow.new_run()
        no_store = await execute(
            flow, run, plan(spec("t", "w")), action_intents=None, block_unassociated_actions=True
        )
        assert no_store.reason == "unassociated_action:no_action_store"
        second = await flow.new_run()
        broken = await execute(
            flow,
            second,
            plan(spec("t", "w")),
            action_intents=Failing(flow.store),
            block_unassociated_actions=True,
        )
        assert broken.reason == "unassociated_action:unverified"
        assert effects.count == 0

    asyncio.run(scenario())

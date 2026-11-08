"""Cancellation, resource release, and terminal-state regressions (M5).

Each test first reproduced a defect observed on the pre-M5 code: cancelling a run left
`AgentRun` as `running`, cancelling `TaskScheduler.run()` orphaned workers and locked
resources, `WorkflowController` left the durable run `running`, and `JobManager.wait()`
swallowed the caller's own cancellation.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from kaiwen_agent import Agent, AgentContext
from kaiwen_agent.artifacts import ArtifactManager
from kaiwen_agent.control import RunControl
from kaiwen_agent.events import InMemoryEventSink
from kaiwen_agent.jobs import (
    JobContext,
    JobManager,
    JobStatus,
    JobSubmitRequest,
    SchedulerTaskRuntimeAdapter,
)
from kaiwen_agent.models.fake import FakeModelProvider
from kaiwen_agent.persistence import SQLitePersistence
from kaiwen_agent.quality import CompletionGate, VerifierRegistry
from kaiwen_agent.resources import ResourceManager
from kaiwen_agent.runtime import DurableRunManager, RuntimeRunStatus
from kaiwen_agent.sessions import InMemoryRunStore
from kaiwen_agent.tasks import AgentTask, TaskGraph, TaskScheduler, TaskStatus
from kaiwen_agent.tools import ToolExecutor
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.tools.durability import ActionIntentRecord, ActionIntentStatus
from kaiwen_agent.types import AgentInput, ModelResponse, ToolCall
from kaiwen_agent.workers import FunctionWorker, TaskExecutionContext, WorkerRegistry
from kaiwen_agent.workflow import (
    WorkflowCompiler,
    WorkflowController,
    WorkflowPlan,
    WorkflowTaskSpec,
)
from tests.test_workflow_runtime import _empty_standards, _identity, _runtime_run


class _Empty(BaseModel):
    pass


class _RecordingRuns(InMemoryRunStore):
    def __init__(self) -> None:
        super().__init__()
        self.last_id: str | None = None

    async def save(self, run: Any) -> None:
        self.last_id = run.id
        await super().save(run)


class _BlockingModel:
    async def respond(self, *args: Any, **kwargs: Any) -> ModelResponse:
        await asyncio.sleep(60)
        return ModelResponse(text="unreachable")


async def _cancel(task: asyncio.Task[Any]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_cancelled_agent_run_is_terminal_cancelled_and_not_failed() -> None:
    async def scenario() -> None:
        runs = _RecordingRuns()
        events = InMemoryEventSink()
        agent = Agent(name="a", model=_BlockingModel(), run_store=runs, event_sink=events)  # type: ignore[arg-type]
        task = asyncio.create_task(
            agent.run(AgentInput(text="hi"), context=AgentContext(session_id="s"))
        )
        await asyncio.sleep(0.05)
        await _cancel(task)

        types = [event.type for event in events.events]
        assert types[-1] == "run.cancelled"
        assert "run.failed" not in types
        assert runs.last_id is not None
        stored = await runs.get(runs.last_id)
        assert stored is not None
        assert stored.status == "cancelled"
        assert stored.completed_at is not None

    asyncio.run(scenario())


def test_cancelling_a_side_effecting_tool_marks_the_action_intent_uncertain(tmp_path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "actions.sqlite3")
        started = asyncio.Event()

        async def effect(context: AgentContext, arguments: BaseModel) -> dict[str, object]:
            started.set()
            await asyncio.sleep(60)
            return {}

        tool = ToolDefinition(
            name="send",
            description="",
            input_model=_Empty,
            handler=effect,
            permissions=frozenset(),
            side_effect="reversible",
            timeout_seconds=120,
        )
        agent = Agent(
            name="a",
            model=FakeModelProvider(
                [ModelResponse(tool_calls=[ToolCall(id="c1", name="send", arguments={})])]
            ),
            tools=[tool],
            tool_executor=ToolExecutor(action_intent_store=store),
        )
        task = asyncio.create_task(
            agent.run(
                AgentInput(text="go"),
                context=AgentContext(session_id="s", tenant_id="t", user_id="u"),
            )
        )
        await started.wait()
        await _cancel(task)

        intents = await store.list_action_intents(
            statuses=frozenset({s for s in ActionIntentStatus})
        )
        assert [intent.status for intent in intents] == [ActionIntentStatus.UNCERTAIN]
        assert intents[0].error_code == "cancelled_in_flight"

    asyncio.run(scenario())


def test_scheduler_cancellation_stops_workers_and_releases_resources() -> None:
    async def scenario() -> None:
        workers = WorkerRegistry()
        started = asyncio.Event()
        worker_cancelled = asyncio.Event()

        async def slow(task: AgentTask, context: TaskExecutionContext) -> dict[str, Any]:
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                worker_cancelled.set()
                raise
            return {}

        workers.register("w", FunctionWorker(slow))
        graph = TaskGraph([AgentTask(id="t1", name="t1", agent_id="w", resource_keys=["r"])])
        resources = ResourceManager()
        events = InMemoryEventSink()
        scheduler = TaskScheduler(graph=graph, workers=workers, resources=resources, events=events)
        run = asyncio.create_task(scheduler.run())
        await started.wait()
        await _cancel(run)

        assert worker_cancelled.is_set(), "worker coroutine was left running"
        assert graph.get("t1").status == TaskStatus.CANCELLED
        assert await resources.available(["r"]), "resource lock was leaked"
        types = [event.type for event in events.events]
        assert "task.cancelled" in types
        assert "task.failed" not in types
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())


def _plan() -> WorkflowPlan:
    return WorkflowPlan(
        objective="Cancellable",
        tasks=(WorkflowTaskSpec(id="slow", name="Slow", worker_id="worker"),),
    )


async def _seed_in_flight_action(store: SQLitePersistence, run_id: str) -> None:
    intent = ActionIntentRecord(
        id="intent-1",
        runtime_run_id=run_id,
        tenant_id="tenant-1",
        user_id="user-1",
        tool_name="send",
        call_id="call-1",
        arguments_checksum="a" * 64,
        idempotency_key="key-1",
        side_effect="reversible",
    )
    await store.prepare_action_intent(intent)
    await store.transition_action_intent(
        intent_id="intent-1",
        tenant_id="tenant-1",
        user_id="user-1",
        expected=frozenset({ActionIntentStatus.PREPARED}),
        status=ActionIntentStatus.EXECUTING,
    )


async def _run_cancelled_workflow(tmp_path, *, in_flight_action: bool, with_action_store: bool):
    store = SQLitePersistence(tmp_path / "workflow.sqlite3")
    started = asyncio.Event()

    async def slow(task: AgentTask, context: TaskExecutionContext) -> dict[str, Any]:
        if in_flight_action:
            # The action is in flight while the run executes (the run is RUNNING and owns it).
            # Seeding it before the run starts would model an orphan of a dead owner, which the
            # controller deliberately refuses to start over.
            await _seed_in_flight_action(store, run.id)
        started.set()
        await asyncio.sleep(60)
        return {}

    workers = WorkerRegistry()
    workers.register("worker", FunctionWorker(slow))
    manager, run = await _runtime_run(store)
    controller = WorkflowController(
        runs=DurableRunManager(store),
        compiler=WorkflowCompiler(
            workers=workers,
            worker_allowlist=frozenset({"worker"}),
            effective_capabilities=frozenset(),
            resource_allowlist=frozenset(),
        ),
        scheduler_factory=lambda graph: TaskScheduler(
            graph=graph,
            workers=workers,
            store=store,
            control=RunControl(checkpoints=store),
        ),
        completion_gate=CompletionGate(verifiers=VerifierRegistry(), artifacts=ArtifactManager()),
        action_intents=store if with_action_store else None,
    )
    task = asyncio.create_task(
        controller.execute(
            run=run,
            identity=_identity(),
            plan=_plan(),
            standards=_empty_standards(),
            protected_input_fingerprint="input-1",
        )
    )
    await asyncio.wait_for(started.wait(), timeout=10)
    await _cancel(task)
    persisted = await manager.get(run_id=run.id, identity=_identity())
    assert persisted is not None
    return persisted


def test_workflow_cancellation_marks_the_durable_run_cancelled(tmp_path) -> None:
    persisted = asyncio.run(
        _run_cancelled_workflow(tmp_path, in_flight_action=False, with_action_store=True)
    )

    assert persisted.status == RuntimeRunStatus.CANCELLED
    assert persisted.completed_at is not None


def test_workflow_cancellation_with_uncertain_action_is_blocked_not_cancelled(tmp_path) -> None:
    persisted = asyncio.run(
        _run_cancelled_workflow(tmp_path, in_flight_action=True, with_action_store=True)
    )

    assert persisted.status == RuntimeRunStatus.BLOCKED
    assert persisted.blocked_reason == "uncertain_action:intent-1"


def _job_context() -> JobContext:
    return JobContext(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        agent_id="admin-agent",
    )


def test_job_wait_propagates_the_callers_own_cancellation() -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def work(task: AgentTask, context: TaskExecutionContext) -> dict[str, object]:
            await release.wait()
            return {}

        workers = WorkerRegistry()
        workers.register("worker-agent", FunctionWorker(work))
        manager = JobManager(
            runtime=SchedulerTaskRuntimeAdapter(workers=workers),
            allowed_worker_ids={"worker-agent"},
        )
        reference = await manager.submit(
            _job_context(), JobSubmitRequest(worker_agent_id="worker-agent", objective="Wait")
        )
        waiter = asyncio.create_task(manager.wait(_job_context(), reference.id))
        await asyncio.sleep(0.05)
        waiter.cancel()
        await asyncio.sleep(0.05)
        assert waiter.cancelled(), "wait() swallowed the caller's cancellation"
        # Cancelling the waiter must not cancel the job itself.
        release.set()
        finished = await manager.wait(_job_context(), reference.id)
        assert finished.status == JobStatus.COMPLETED

    asyncio.run(scenario())


def test_job_wait_still_returns_when_the_job_itself_is_cancelled() -> None:
    async def scenario() -> None:
        async def work(task: AgentTask, context: TaskExecutionContext) -> dict[str, object]:
            while True:  # cooperative worker: cancellation is delivered at checkpoints
                await context.checkpoint()
                await asyncio.sleep(0.01)

        workers = WorkerRegistry()
        workers.register("worker-agent", FunctionWorker(work))
        manager = JobManager(
            runtime=SchedulerTaskRuntimeAdapter(workers=workers),
            allowed_worker_ids={"worker-agent"},
        )
        reference = await manager.submit(
            _job_context(), JobSubmitRequest(worker_agent_id="worker-agent", objective="Cancel")
        )
        await asyncio.sleep(0.05)
        await manager.cancel(_job_context(), reference.id)
        finished = await asyncio.wait_for(manager.wait(_job_context(), reference.id), timeout=5)
        assert finished.status == JobStatus.CANCELLED

    asyncio.run(scenario())

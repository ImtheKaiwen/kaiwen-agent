import asyncio

import pytest

from kaiwen_agent.control import ControlMessage, RunControl, TaskCancelledError
from kaiwen_agent.events import InMemoryEventSink
from kaiwen_agent.resources import ResourceManager
from kaiwen_agent.tasks import (
    AgentTask,
    TaskGraph,
    TaskGraphError,
    TaskRetryPolicy,
    TaskScheduler,
    TaskStatus,
)
from kaiwen_agent.workers import FunctionWorker, RetryableTaskError, WorkerRegistry


def test_graph_rejects_cycles() -> None:
    first = AgentTask(id="first", name="First", agent_id="worker")
    second = AgentTask(id="second", name="Second", agent_id="worker")
    graph = TaskGraph([first, second])
    graph.add_dependency("second", "first")
    with pytest.raises(TaskGraphError, match="cycle"):
        graph.add_dependency("first", "second")


def test_scheduler_runs_independent_tasks_in_parallel_then_dependency() -> None:
    async def scenario() -> None:
        active = 0
        maximum_active = 0
        completed: list[str] = []

        async def execute(task, context):
            nonlocal active, maximum_active
            await context.checkpoint(state={"phase": "start"})
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            completed.append(task.id)
            return {"task": task.id}

        first = AgentTask(id="first", name="First", agent_id="worker")
        second = AgentTask(id="second", name="Second", agent_id="worker")
        final = AgentTask(
            id="final",
            name="Final",
            agent_id="worker",
            dependencies=["first", "second"],
        )
        registry = WorkerRegistry()
        registry.register("worker", FunctionWorker(execute))
        scheduler = TaskScheduler(
            graph=TaskGraph([first, second, final]),
            workers=registry,
            max_concurrency=2,
        )
        tasks = await scheduler.run()
        assert all(task.status == TaskStatus.COMPLETED for task in tasks)
        assert maximum_active == 2
        assert completed[-1] == "final"

    asyncio.run(scenario())


def test_resource_capacity_serializes_parallel_tasks() -> None:
    async def scenario() -> None:
        active = 0
        maximum_active = 0

        async def execute(task, context):
            nonlocal active, maximum_active
            del task, context
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return {}

        tasks = [
            AgentTask(
                id=f"print_{index}",
                name="Print",
                agent_id="printer",
                resource_keys=["printer:main"],
            )
            for index in range(2)
        ]
        registry = WorkerRegistry()
        registry.register("printer", FunctionWorker(execute))
        scheduler = TaskScheduler(
            graph=TaskGraph(tasks),
            workers=registry,
            resources=ResourceManager({"printer:main": 1}),
            max_concurrency=2,
        )
        await scheduler.run()
        assert maximum_active == 1

    asyncio.run(scenario())


def test_retry_policy_is_deterministic() -> None:
    async def scenario() -> None:
        attempts = 0
        events = InMemoryEventSink()

        async def execute(task, context):
            nonlocal attempts
            del task, context
            attempts += 1
            if attempts == 1:
                raise RetryableTaskError("temporary", code="network")
            return {"ok": True}

        task = AgentTask(
            id="retry",
            name="Retry",
            agent_id="worker",
            retry_policy=TaskRetryPolicy(
                max_attempts=2, backoff_seconds=0, retry_on=frozenset({"network"})
            ),
        )
        registry = WorkerRegistry()
        registry.register("worker", FunctionWorker(execute))
        scheduler = TaskScheduler(graph=TaskGraph([task]), workers=registry, events=events)
        await scheduler.run()
        assert task.status == TaskStatus.COMPLETED
        assert task.attempts == 2
        assert any(event.type == "task.retrying" for event in events.events)

    asyncio.run(scenario())


def test_running_task_cancels_at_checkpoint() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        continue_to_checkpoint = asyncio.Event()

        async def execute(task, context):
            del task
            entered.set()
            await continue_to_checkpoint.wait()
            await context.checkpoint(state={"safe": True})
            return {"unexpected": True}

        task = AgentTask(id="cancel", name="Cancel", agent_id="worker")
        registry = WorkerRegistry()
        registry.register("worker", FunctionWorker(execute))
        scheduler = TaskScheduler(graph=TaskGraph([task]), workers=registry)
        running = asyncio.create_task(scheduler.run())
        await entered.wait()
        await scheduler.cancel(task.id)
        continue_to_checkpoint.set()
        await running
        assert task.status == TaskStatus.CANCELLED
        checkpoint = await scheduler.control.checkpoints.latest_checkpoint(task.id)
        assert checkpoint is not None and checkpoint.state == {"safe": True}

    asyncio.run(scenario())


def test_mailbox_pause_resume_instruction_and_checkpoint() -> None:
    async def scenario() -> None:
        control = RunControl()
        await control.send("task_1", ControlMessage(type="pause", created_by="user"))
        waiting = asyncio.create_task(control.checkpoint("task_1", state={"step": 1}))
        await asyncio.sleep(0)
        assert not waiting.done()
        await control.send(
            "task_1", ControlMessage(type="instruction", content="skip cache", created_by="user")
        )
        await control.send("task_1", ControlMessage(type="resume", created_by="user"))
        messages = await waiting
        assert any(message.content == "skip cache" for message in messages)
        checkpoint = await control.checkpoints.latest_checkpoint("task_1")
        assert checkpoint is not None and checkpoint.state == {"step": 1}

        await control.send("task_1", ControlMessage(type="cancel", created_by="user"))
        with pytest.raises(TaskCancelledError):
            await control.checkpoint("task_1")

    asyncio.run(scenario())

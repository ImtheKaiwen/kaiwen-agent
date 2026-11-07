"""Resource contention across schedulers (M5).

Observed before the fix: jobs get one TaskScheduler each over a shared ResourceManager, and
a scheduler whose only task was blocked by another scheduler's lock failed with
SchedulerStalledError instead of waiting, so concurrent writers on one resource failed.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from kaiwen_agent.resources import ResourceManager
from kaiwen_agent.tasks import AgentTask, TaskGraph, TaskScheduler, TaskStatus
from kaiwen_agent.workers import FunctionWorker, WorkerRegistry


def _scheduler(resources: ResourceManager, work: Any, task_id: str) -> TaskScheduler:
    workers = WorkerRegistry()
    workers.register("w", FunctionWorker(work))
    graph = TaskGraph([AgentTask(id=task_id, name=task_id, agent_id="w", resource_keys=["repo"])])
    return TaskScheduler(graph=graph, workers=workers, resources=resources)


def test_scheduler_waits_for_a_resource_held_by_another_scheduler() -> None:
    async def scenario() -> None:
        resources = ResourceManager()
        order: list[str] = []

        async def work(task: AgentTask, context: Any) -> dict[str, Any]:
            order.append(f"start:{task.id}")
            await asyncio.sleep(0.05)
            order.append(f"end:{task.id}")
            return {}

        first = _scheduler(resources, work, "a")
        second = _scheduler(resources, work, "b")
        results = await asyncio.wait_for(asyncio.gather(first.run(), second.run()), timeout=10)

        assert all(tasks[0].status == TaskStatus.COMPLETED for tasks in results)
        assert order in (
            ["start:a", "end:a", "start:b", "end:b"],
            ["start:b", "end:b", "start:a", "end:a"],
        ), "tasks sharing a resource must not overlap"

    asyncio.run(scenario())


def test_cancelling_a_scheduler_that_waits_for_a_resource_does_not_hang() -> None:
    async def scenario() -> None:
        resources = ResourceManager()
        assert await resources.try_acquire(["repo"], "external-holder")

        async def work(task: AgentTask, context: Any) -> dict[str, Any]:
            raise AssertionError("must not run while the resource is held")

        waiting = _scheduler(resources, work, "w1")
        run = asyncio.create_task(waiting.run())
        await asyncio.sleep(0.1)
        run.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(run, timeout=5)
        assert waiting.graph.get("w1").status == TaskStatus.CANCELLED
        await resources.release("external-holder")

    asyncio.run(scenario())

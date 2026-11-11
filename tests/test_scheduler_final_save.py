"""`TaskScheduler.run` returns only after every task's final write has finished.

A worker moves its task to COMPLETED in memory before it saves the row. If another task's future
woke the scheduler loop in that window, the graph already read as complete and `run` returned while
the save was still in flight, so a reader of the store (the durable Lead proposal source, the
Lead view) saw a stale row. Reproduced here with a store whose final write is held open by an event.
"""

from __future__ import annotations

import asyncio
from typing import Any

from kaiwen_agent.tasks import AgentTask, InMemoryTaskStore, TaskGraph, TaskScheduler, TaskStatus
from kaiwen_agent.workers import FunctionWorker, WorkerRegistry


class HeldStore(InMemoryTaskStore):
    """Holds the COMPLETED write of one task until released."""

    def __init__(self, held_id: str) -> None:
        super().__init__()
        self.held_id = held_id
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def save_task(self, task: AgentTask) -> None:
        if task.id == self.held_id and task.status is TaskStatus.COMPLETED:
            self.reached.set()
            await self.release.wait()
        await super().save_task(task)


def test_run_does_not_return_while_a_final_task_write_is_in_flight() -> None:
    async def scenario() -> None:
        fast = AgentTask(id="fast", name="fast", agent_id="w")
        slow = AgentTask(id="slow", name="slow", agent_id="w")
        gate = asyncio.Event()

        async def work(task: AgentTask, context: Any) -> dict[str, Any]:
            if task.id == "slow":
                await gate.wait()  # finishes second, after `fast` woke the loop once
            return {"done": task.id}

        workers = WorkerRegistry()
        workers.register("w", FunctionWorker(work))
        store = HeldStore("fast")
        scheduler = TaskScheduler(
            graph=TaskGraph([fast, slow]), workers=workers, store=store, max_concurrency=2
        )
        running = asyncio.create_task(scheduler.run())
        await asyncio.wait_for(store.reached.wait(), timeout=10)  # `fast` is COMPLETED in memory
        gate.set()  # `slow` finishes and wakes the loop; the graph now reads complete
        for _ in range(50):
            await asyncio.sleep(0)
        assert not running.done()  # the held final write of `fast` is still pending
        stored = await store.get_task("fast")
        assert stored is not None and stored.status is not TaskStatus.COMPLETED
        store.release.set()
        tasks = await asyncio.wait_for(running, timeout=10)
        assert {task.status for task in tasks} == {TaskStatus.COMPLETED}
        for name in ("fast", "slow"):  # whatever run returned is durable
            saved = await store.get_task(name)
            assert saved is not None and saved.status is TaskStatus.COMPLETED
            assert saved.output == {"done": name}

    asyncio.run(scenario())

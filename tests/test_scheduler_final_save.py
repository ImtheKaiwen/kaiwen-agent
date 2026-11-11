"""`TaskScheduler.run` returns only after every task's final write has finished.

A worker moves its task to COMPLETED in memory before it saves the row. If another task's future
woke the scheduler loop in that window, the graph already read as complete and `run` returned while
the save was still in flight, so a reader of the store (the durable Lead proposal source, the
Lead view) saw a stale row. Reproduced here with a store whose final write is held open by an event.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kaiwen_agent.exceptions import OwnershipLostError, RunOwnershipError
from kaiwen_agent.resources import ResourceManager
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


class ScriptedStore(InMemoryTaskStore):
    """Final (COMPLETED) writes of chosen tasks are held, refused or hung, deterministically."""

    def __init__(self) -> None:
        super().__init__()
        self.refuse: dict[str, asyncio.Event] = {}  # task id -> release, then ownership is lost
        self.hang: set[str] = set()  # task ids whose final write never finishes
        self.entered: dict[str, asyncio.Event] = {}
        self.cancelled: set[str] = set()

    async def save_task(self, task: AgentTask) -> None:
        if task.status is TaskStatus.COMPLETED:
            self.entered.setdefault(task.id, asyncio.Event()).set()
            try:
                if task.id in self.refuse:
                    await self.refuse[task.id].wait()
                    raise OwnershipLostError("lease_expired")
                if task.id in self.hang:
                    await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.add(task.id)
                raise
        await super().save_task(task)


def _scheduler(store: Any, tasks: list[AgentTask], work: Any, resources: Any = None) -> Any:
    workers = WorkerRegistry()
    workers.register("w", FunctionWorker(work))
    return TaskScheduler(
        graph=TaskGraph(tasks),
        workers=workers,
        store=store,
        resources=resources,
        max_concurrency=4,
    )


def test_ownership_lost_during_a_final_save_propagates_and_nothing_else_completes() -> None:
    async def scenario() -> None:
        store = ScriptedStore()
        store.refuse["lost"] = asyncio.Event()
        store.hang.add("other")
        gate = asyncio.Event()
        resources = ResourceManager()
        tasks = [
            AgentTask(id="lost", name="lost", agent_id="w", resource_keys=["k1"]),
            AgentTask(id="other", name="other", agent_id="w", resource_keys=["k2"]),
            AgentTask(id="trigger", name="trigger", agent_id="w"),
        ]

        async def work(task: AgentTask, context: Any) -> dict[str, Any]:
            if task.id == "trigger":
                await gate.wait()  # wakes the loop after the others are COMPLETED in memory
            return {}

        running = asyncio.create_task(_scheduler(store, tasks, work, resources).run())
        for name in ("lost", "other"):
            await asyncio.wait_for(_wait(store, name), timeout=10)
        gate.set()
        for _ in range(50):
            await asyncio.sleep(0)
        assert not running.done()  # the drain is waiting on the in-flight final saves
        store.refuse["lost"].set()  # the lease is lost during that final save
        # The refusal must surface by itself; a hung sibling write must not hold it back. (A
        # wait_for timeout would also end in this error, so completion is asserted explicitly.)
        await asyncio.wait({running}, timeout=5)
        assert running.done(), "the ownership refusal was held back by another in-flight write"
        with pytest.raises(RunOwnershipError):
            running.result()
        assert store.cancelled == {"other"}  # the other active worker was cancelled
        for name in ("lost", "other"):  # no unauthorized completion was recorded
            saved = await store.get_task(name)
            assert saved is None or saved.status is not TaskStatus.COMPLETED
        assert await resources.available(["k1", "k2"])  # every resource was released

    asyncio.run(scenario())


async def _wait(store: ScriptedStore, name: str) -> None:
    while name not in store.entered:
        await asyncio.sleep(0)
    await store.entered[name].wait()


def test_cancelling_the_run_while_it_drains_stops_the_workers_and_records_nothing() -> None:
    async def scenario() -> None:
        store = ScriptedStore()
        store.hang.add("held")
        gate = asyncio.Event()
        resources = ResourceManager()
        tasks = [
            AgentTask(id="held", name="held", agent_id="w", resource_keys=["k"]),
            AgentTask(id="trigger", name="trigger", agent_id="w"),
        ]

        async def work(task: AgentTask, context: Any) -> dict[str, Any]:
            if task.id == "trigger":
                await gate.wait()
            return {}

        running = asyncio.create_task(_scheduler(store, tasks, work, resources).run())
        await _wait(store, "held")
        gate.set()
        for _ in range(50):
            await asyncio.sleep(0)
        assert not running.done()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert store.cancelled == {"held"}
        held = await store.get_task("held")
        assert held is None or held.status is not TaskStatus.COMPLETED
        assert await resources.available(["k"])

    asyncio.run(scenario())


def test_without_concurrency_or_fencing_the_scheduler_behaves_as_before() -> None:
    async def scenario() -> None:
        calls: list[str] = []

        async def work(task: AgentTask, context: Any) -> dict[str, Any]:
            calls.append(task.id)
            if task.id == "bad":
                raise RuntimeError("worker failed")
            return {"done": task.id}

        first = AgentTask(id="first", name="first", agent_id="w")
        second = AgentTask(id="second", name="second", agent_id="w", dependencies=["first"])
        bad = AgentTask(id="bad", name="bad", agent_id="w")
        scheduler = _scheduler(InMemoryTaskStore(), [first, second, bad], work)
        tasks = {task.id: task for task in await scheduler.run()}
        assert tasks["first"].status is TaskStatus.COMPLETED
        assert tasks["second"].status is TaskStatus.COMPLETED and calls.index(
            "first"
        ) < calls.index("second")
        assert (
            tasks["bad"].status is TaskStatus.FAILED
            and tasks["bad"].error_code == "execution_error"
        )
        assert scheduler._active == {}

    asyncio.run(scenario())

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

from kaiwen_agent.control import ControlMessage, RunControl, TaskCancelledError
from kaiwen_agent.events import AgentEvent, EventSink, EventType, InMemoryEventSink
from kaiwen_agent.exceptions import RunOwnershipError
from kaiwen_agent.resources import ResourceManager
from kaiwen_agent.tasks.graph import TaskGraph
from kaiwen_agent.tasks.models import AgentTask, TaskStatus
from kaiwen_agent.tasks.state import transition_task
from kaiwen_agent.tasks.store import InMemoryTaskStore, TaskStore
from kaiwen_agent.workers import RetryableTaskError, TaskExecutionContext, WorkerRegistry


class SchedulerStalledError(RuntimeError):
    pass


_RESOURCE_POLL_SECONDS = 0.02


class TaskScheduler:
    def __init__(
        self,
        *,
        graph: TaskGraph,
        workers: WorkerRegistry,
        store: TaskStore | None = None,
        events: EventSink | None = None,
        resources: ResourceManager | None = None,
        control: RunControl | None = None,
        max_concurrency: int = 4,
        agent_concurrency: dict[str, int] | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self.graph = graph
        self.workers = workers
        self.store = store or InMemoryTaskStore()
        self.events = events or InMemoryEventSink()
        self.resources = resources or ResourceManager()
        self.control = control or RunControl()
        self.max_concurrency = max_concurrency
        self.agent_concurrency = dict(agent_concurrency or {})
        if any(limit < 1 for limit in self.agent_concurrency.values()):
            raise ValueError("Agent concurrency limits must be at least 1")
        self._active: dict[str, asyncio.Task[None]] = {}
        self._agent_usage: Counter[str] = Counter()
        self._not_before: dict[str, float] = {}
        # Set once a write is refused for lost ownership. From then on nothing is written.
        self._ownership_error: RunOwnershipError | None = None

    async def run(self) -> tuple[AgentTask, ...]:
        try:
            return await self._run()
        except (asyncio.CancelledError, Exception) as error:
            # Cancellation or an internal error must never leave workers running or
            # resource locks held; unfinished tasks end as cancelled, not failed.
            await asyncio.shield(self._abort_active())
            if self._ownership_error is not None and error is not self._ownership_error:
                # The refusal that ended the run is the failure to report, not the cancellations
                # this scheduler issued to stop its own workers.
                raise self._ownership_error from None
            raise

    async def _save(self, task: AgentTask) -> None:
        """The only write path. After ownership is lost it writes nothing, ever.

        A refusal (`OwnershipLostError`) is remembered and re-raised for every later write, so a
        cleanup or failure-recording path can never turn a fenced refusal into an unauthorized
        write, and the original refusal is what surfaces.
        """

        if self._ownership_error is not None:
            raise self._ownership_error
        try:
            await self.store.save_task(task)
        except RunOwnershipError as error:
            self._ownership_error = error
            raise

    async def _abort_active(self) -> None:
        active = list(self._active.values())
        for future in active:
            future.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        self._active.clear()
        if self._ownership_error is not None:
            # Workers are stopped and process-local state is released, but no state may be
            # recorded through an invalid epoch: not cancelled, not failed, not completed.
            # Whatever was in flight stays as it was for recovery to judge.
            return
        for task in self.graph.tasks():
            if task.terminal:
                continue
            transition_task(task, TaskStatus.CANCELLED)
            task.error_code = task.error_code or "cancelled"
            await self._save(task)
            await self._emit(task, "task.cancelled", {"reason": "scheduler_stopped"})

    async def _run(self) -> tuple[AgentTask, ...]:
        self.graph.validate()
        for task in self.graph.tasks():
            await self._save(task)
            await self._emit(task, "task.created", {"agent_id": task.agent_id})

        while not self.graph.complete:
            self.graph.refresh_states()
            await self._persist_all()
            await self._wake_resource_waiters()
            scheduled = await self._schedule_ready()

            if self._active:
                done, _ = await asyncio.wait(
                    self._active.values(), return_when=asyncio.FIRST_COMPLETED
                )
                for completed in done:
                    await completed
                self._active = {
                    task_id: future for task_id, future in self._active.items() if not future.done()
                }
                continue

            delayed = [
                deadline
                for task_id, deadline in self._not_before.items()
                if self.graph.get(task_id).status == TaskStatus.READY
            ]
            if delayed:
                await asyncio.sleep(max(0, min(delayed) - asyncio.get_running_loop().time()))
                continue
            if not scheduled and not self.graph.complete:
                if any(task.status == TaskStatus.WAITING_RESOURCE for task in self.graph.tasks()):
                    # This scheduler holds nothing, so the lock belongs to another scheduler
                    # sharing the ResourceManager. Wait for it instead of failing the task.
                    await asyncio.sleep(_RESOURCE_POLL_SECONDS)
                    continue
                raise SchedulerStalledError("No runnable tasks and no active workers")

        # A worker marks its task COMPLETED in memory before it saves the row, so the graph can
        # read complete while a final write is still in flight. Return only once every worker
        # has finished, so whatever is returned is what the store holds.
        # The first failure (a refused write) surfaces at once; `run` then stops the others.
        while self._active:
            done, _ = await asyncio.wait(self._active.values(), return_when=asyncio.FIRST_EXCEPTION)
            for completed in done:
                await completed
            self._active = {
                task_id: future for task_id, future in self._active.items() if not future.done()
            }

        return self.graph.tasks()

    async def cancel(self, task_id: str, *, created_by: str = "user") -> None:
        task = self.graph.get(task_id)
        if task.terminal:
            return
        if task.status in {TaskStatus.RUNNING, TaskStatus.PAUSED}:
            await self.control.send(task_id, ControlMessage(type="cancel", created_by=created_by))
            return
        transition_task(task, TaskStatus.CANCELLED)
        await self._save(task)
        await self._emit(task, "task.cancelled", {"created_by": created_by})

    async def pause(self, task_id: str, *, created_by: str = "user") -> None:
        task = self.graph.get(task_id)
        if task.status != TaskStatus.RUNNING:
            raise ValueError("Only running tasks can be paused")
        transition_task(task, TaskStatus.PAUSED)
        await self.control.send(task_id, ControlMessage(type="pause", created_by=created_by))
        await self._save(task)
        await self._emit(task, "task.paused", {"created_by": created_by})

    async def resume(self, task_id: str, *, created_by: str = "user") -> None:
        task = self.graph.get(task_id)
        if task.status != TaskStatus.PAUSED:
            raise ValueError("Only paused tasks can be resumed")
        transition_task(task, TaskStatus.RUNNING)
        await self.control.send(task_id, ControlMessage(type="resume", created_by=created_by))
        await self._save(task)
        await self._emit(task, "task.resumed", {"created_by": created_by})

    async def send_instruction(
        self, task_id: str, content: str, *, created_by: str = "user"
    ) -> None:
        self.graph.get(task_id)
        await self.control.send(
            task_id,
            ControlMessage(type="instruction", content=content, created_by=created_by),
        )

    async def _wake_resource_waiters(self) -> None:
        for task in self.graph.tasks():
            if task.status == TaskStatus.WAITING_RESOURCE and await self.resources.available(
                task.resource_keys
            ):
                transition_task(task, TaskStatus.READY)
                await self._save(task)

    async def _schedule_ready(self) -> bool:
        scheduled = False
        loop_time = asyncio.get_running_loop().time()
        for task in self.graph.ready_tasks():
            if len(self._active) >= self.max_concurrency:
                break
            if self._not_before.get(task.id, 0) > loop_time:
                continue
            agent_limit = self.agent_concurrency.get(task.agent_id, self.max_concurrency)
            if self._agent_usage[task.agent_id] >= agent_limit:
                continue
            if not await self.resources.try_acquire(task.resource_keys, task.id):
                transition_task(task, TaskStatus.WAITING_RESOURCE)
                await self._save(task)
                await self._emit(task, "resource.waiting", {"resource_keys": task.resource_keys})
                continue

            self._not_before.pop(task.id, None)
            transition_task(task, TaskStatus.QUEUED)
            await self._save(task)
            await self._emit(task, "task.queued", {})
            if task.resource_keys:
                await self._emit(task, "resource.acquired", {"resource_keys": task.resource_keys})
            self._agent_usage[task.agent_id] += 1
            self._active[task.id] = asyncio.create_task(self._execute_task(task))
            scheduled = True
        return scheduled

    async def _execute_task(self, task: AgentTask) -> None:
        try:
            transition_task(task, TaskStatus.RUNNING)
            task.attempts += 1
            await self._save(task)
            await self._emit(task, "task.started", {"attempt": task.attempts})
            worker = self.workers.get(task.agent_id)
            context = TaskExecutionContext(
                task_id=task.id,
                control=self.control,
                progress_callback=lambda progress, details: self._progress(task, progress, details),
            )
            execution = worker.execute(task, context)
            if task.timeout_seconds is None:
                output = await execution
            else:
                output = await asyncio.wait_for(execution, timeout=task.timeout_seconds)
            task.output = output
            task.error = None
            task.error_code = None
            transition_task(task, TaskStatus.COMPLETED)
            await self._save(task)
            await self._emit(task, "task.completed", {"attempts": task.attempts})
        except RunOwnershipError as error:
            # Authority is gone: no failure, retry or cancellation may be recorded. The main
            # loop sees the refusal, stops dispatch and cancels the other workers.
            self._ownership_error = self._ownership_error or error
            raise
        except TaskCancelledError as error:
            task.error = str(error)
            task.error_code = "cancelled"
            transition_task(task, TaskStatus.CANCELLED)
            await self._save(task)
            await self._emit(task, "task.cancelled", {})
        except TimeoutError as error:
            await self._handle_failure(task, error, "timeout")
        except RetryableTaskError as error:
            await self._handle_failure(task, error, error.code)
        except Exception as error:
            await self._handle_failure(task, error, "execution_error")
        finally:
            await self.resources.release(task.id)
            if task.resource_keys:
                await self._emit(task, "resource.released", {"resource_keys": task.resource_keys})
            self._agent_usage[task.agent_id] -= 1

    async def _handle_failure(self, task: AgentTask, error: BaseException, error_code: str) -> None:
        task.error = str(error)
        task.error_code = error_code
        transition_task(task, TaskStatus.FAILED)
        retryable = error_code in task.retry_policy.retry_on
        if retryable and task.attempts < task.retry_policy.max_attempts:
            delay = task.retry_policy.delay_for_attempt(task.attempts)
            transition_task(task, TaskStatus.READY)
            self._not_before[task.id] = asyncio.get_running_loop().time() + delay
            await self._save(task)
            await self._emit(
                task,
                "task.retrying",
                {"attempt": task.attempts, "error_code": error_code, "delay": delay},
            )
            return
        await self._save(task)
        await self._emit(
            task,
            "task.failed",
            {"attempts": task.attempts, "error_code": error_code},
        )

    async def _progress(self, task: AgentTask, progress: float, details: dict[str, Any]) -> None:
        task.progress = progress
        try:
            await self._save(task)
        except RunOwnershipError:
            # A worker that swallows this error must still be stopped: cancel every worker, the
            # caller's included. `run` then reports the ownership refusal.
            for future in list(self._active.values()):
                future.cancel()
            raise
        await self._emit(task, "task.progress", {"progress": progress, **details})

    async def _persist_all(self) -> None:
        for task in self.graph.tasks():
            await self._save(task)

    async def _emit(
        self, task: AgentTask, event_type: EventType, payload: dict[str, object]
    ) -> None:
        await self.events.append(
            AgentEvent(
                type=event_type,
                trace_id=task.trace_id,
                session_id=task.session_id,
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                payload={"task_id": task.id, **payload},
            )
        )

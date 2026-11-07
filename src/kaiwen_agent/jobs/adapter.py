from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from kaiwen_agent.control import RunControl
from kaiwen_agent.events import AgentEvent, EventSink, EventType
from kaiwen_agent.jobs.models import AgentJob
from kaiwen_agent.resources import ResourceManager
from kaiwen_agent.tasks import (
    AgentTask,
    InMemoryTaskStore,
    TaskGraph,
    TaskScheduler,
    TaskStore,
)
from kaiwen_agent.workers import WorkerRegistry

TaskEventCallback = Callable[[EventType, dict[str, object]], Awaitable[None]]


class TaskRuntimeAdapter(Protocol):
    async def execute(self, job: AgentJob, *, on_event: TaskEventCallback) -> AgentTask: ...

    async def cancel(self, task_id: str, *, created_by: str) -> bool: ...


class _CallbackEventSink(EventSink):
    def __init__(self, callback: TaskEventCallback) -> None:
        self.callback = callback

    async def append(self, event: AgentEvent) -> None:
        await self.callback(event.type, event.payload)


class SchedulerTaskRuntimeAdapter:
    """Runs each background job through the existing durable TaskScheduler."""

    def __init__(
        self,
        *,
        workers: WorkerRegistry,
        task_store: TaskStore | None = None,
        resources: ResourceManager | None = None,
        control: RunControl | None = None,
        max_concurrency: int = 4,
        agent_concurrency: dict[str, int] | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if any(limit < 1 for limit in (agent_concurrency or {}).values()):
            raise ValueError("Agent concurrency limits must be at least 1")
        self.workers = workers
        self.task_store = task_store or InMemoryTaskStore()
        self.resources = resources or ResourceManager()
        self.control = control or RunControl()
        self._global_limit = asyncio.Semaphore(max_concurrency)
        self._agent_limits = {
            agent_id: asyncio.Semaphore(limit)
            for agent_id, limit in (agent_concurrency or {}).items()
        }
        self._default_agent_limit = max_concurrency
        self._active: dict[str, TaskScheduler] = {}

    async def execute(self, job: AgentJob, *, on_event: TaskEventCallback) -> AgentTask:
        agent_limit = self._agent_limits.setdefault(
            job.worker_agent_id, asyncio.Semaphore(self._default_agent_limit)
        )
        async with self._global_limit, agent_limit:
            return await self._execute_with_slot(job, on_event=on_event)

    async def _execute_with_slot(self, job: AgentJob, *, on_event: TaskEventCallback) -> AgentTask:
        task = AgentTask(
            id=job.task_id,
            name=job.objective,
            description=job.objective,
            agent_id=job.worker_agent_id,
            session_id=job.session_id,
            tenant_id=job.tenant_id,
            user_id=job.user_id,
            conversation_id=job.conversation_id,
            trace_id=job.trace_id,
            parent_id=None,
            input=job.input,
            resource_keys=job.resource_keys,
            retry_policy=job.retry_policy,
            timeout_seconds=job.timeout_seconds,
            capabilities=job.capabilities,
            delegation_depth=job.delegation_depth,
            metadata={"job_id": job.id, "context_ref": job.context_ref},
        )
        scheduler = TaskScheduler(
            graph=TaskGraph([task]),
            workers=self.workers,
            store=self.task_store,
            events=_CallbackEventSink(on_event),
            resources=self.resources,
            control=self.control,
            max_concurrency=1,
        )
        self._active[task.id] = scheduler
        try:
            tasks = await scheduler.run()
            return tasks[0]
        finally:
            self._active.pop(task.id, None)

    async def cancel(self, task_id: str, *, created_by: str) -> bool:
        scheduler = self._active.get(task_id)
        if scheduler is None:
            return False
        await scheduler.cancel(task_id, created_by=created_by)
        return True

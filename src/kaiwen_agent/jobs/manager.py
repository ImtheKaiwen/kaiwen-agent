from __future__ import annotations

import asyncio
from contextlib import suppress
from functools import partial
from uuid import uuid4

from kaiwen_agent.dead_letter import (
    DeadLetterContext,
    DeadLetterKind,
    DeadLetterManager,
)
from kaiwen_agent.events import EventSink, InMemoryEventSink
from kaiwen_agent.jobs.adapter import TaskRuntimeAdapter
from kaiwen_agent.jobs.models import (
    AgentJob,
    AgentJobRef,
    JobContext,
    JobStatus,
    JobSubmitRequest,
)
from kaiwen_agent.jobs.store import InMemoryJobStore, JobStore
from kaiwen_agent.jobs.supervisor import JobCompletionNotifier, JobSupervisor
from kaiwen_agent.types import utc_now


class JobManager:
    def __init__(
        self,
        *,
        runtime: TaskRuntimeAdapter,
        allowed_worker_ids: frozenset[str] | set[str],
        store: JobStore | None = None,
        events: EventSink | None = None,
        completion_notifier: JobCompletionNotifier | None = None,
        dead_letters: DeadLetterManager | None = None,
    ) -> None:
        if not allowed_worker_ids:
            raise ValueError("allowed_worker_ids cannot be empty")
        self.runtime = runtime
        self.allowed_worker_ids = frozenset(allowed_worker_ids)
        self.store = store or InMemoryJobStore()
        self.events = events or InMemoryEventSink()
        self.supervisor = JobSupervisor(
            store=self.store,
            events=self.events,
            completion_notifier=completion_notifier,
        )
        self.dead_letters = dead_letters
        self._background: dict[str, asyncio.Task[None]] = {}

    async def submit(self, context: JobContext, request: JobSubmitRequest) -> AgentJobRef:
        return await self._submit(
            context,
            request,
            capabilities=frozenset(),
            delegation_depth=None,
        )

    async def submit_delegated(
        self,
        context: JobContext,
        request: JobSubmitRequest,
        *,
        capabilities: frozenset[str],
        delegation_depth: int | None,
    ) -> AgentJobRef:
        if not capabilities <= context.capabilities:
            raise PermissionError("Delegated capabilities exceed caller capabilities")
        if delegation_depth != context.delegation_depth + 1:
            raise ValueError("Delegation depth must advance exactly one level")
        return await self._submit(
            context,
            request,
            capabilities=capabilities,
            delegation_depth=delegation_depth,
        )

    async def _submit(
        self,
        context: JobContext,
        request: JobSubmitRequest,
        *,
        capabilities: frozenset[str],
        delegation_depth: int | None,
    ) -> AgentJobRef:
        if request.worker_agent_id not in self.allowed_worker_ids:
            raise PermissionError("Worker agent is not allowlisted")
        parent: AgentJob | None = None
        if request.parent_job_id is not None:
            parent = await self._owned(context, request.parent_job_id)
            if parent.terminal:
                raise ValueError("Cannot add a child to a terminal job")
            if not capabilities <= parent.capabilities:
                raise PermissionError("Child capabilities exceed parent job capabilities")
            if delegation_depth is not None and delegation_depth != parent.delegation_depth + 1:
                raise ValueError("Child delegation depth must advance exactly one level")
        effective_depth = (
            delegation_depth
            if delegation_depth is not None
            else (parent.delegation_depth + 1 if parent else 0)
        )
        job = AgentJob(
            trace_id=context.trace_id or (parent.trace_id if parent else f"trace_{uuid4().hex}"),
            parent_job_id=parent.id if parent else None,
            parent_run_id=context.run_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            conversation_id=context.conversation_id,
            session_id=context.session_id,
            requested_by_agent_id=context.agent_id,
            worker_agent_id=request.worker_agent_id,
            capabilities=capabilities,
            delegation_depth=effective_depth,
            objective=request.objective,
            input=request.input,
            context_ref=request.context_ref,
            priority=request.priority,
            retry_policy=request.retry_policy,
            timeout_seconds=request.timeout_seconds,
            resource_keys=request.resource_keys,
        )
        await self.store.save_job(job)
        await self.supervisor.emit(job, "job.created", {})
        await self.supervisor.emit(job, "job.queued", {})
        self._start(job)
        return AgentJobRef.from_job(job)

    async def get(self, context: JobContext, job_id: str) -> AgentJob:
        return await self._owned(context, job_id)

    async def list(
        self,
        context: JobContext,
        *,
        status: JobStatus | None = None,
        conversation_only: bool = False,
        limit: int = 100,
    ) -> list[AgentJob]:
        if conversation_only and context.conversation_id is None:
            return []
        return await self.store.list_jobs(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            conversation_id=context.conversation_id if conversation_only else None,
            status=status,
            limit=limit,
        )

    async def cancel(self, context: JobContext, job_id: str, *, cascade: bool = True) -> AgentJob:
        job = await self._owned(context, job_id)
        if job.terminal:
            return job
        now = utc_now()
        requested = job.model_copy(update={"status": JobStatus.CANCEL_REQUESTED, "updated_at": now})
        await self.store.save_job(requested)
        await self.supervisor.emit(requested, "job.cancel_requested", {})
        if cascade:
            children = await self.store.list_jobs(
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                parent_job_id=job.id,
                limit=1000,
            )
            for child in children:
                if not child.terminal:
                    await self.cancel(context, child.id, cascade=True)
        active = await self.runtime.cancel(job.task_id, created_by=context.user_id)
        background = self._background.get(job.id)
        if not active and background is not None and not background.done():
            background.cancel()
        elif not active:
            requested = requested.model_copy(
                update={
                    "status": JobStatus.CANCELLED,
                    "completed_at": now,
                    "updated_at": now,
                }
            )
            await self.store.save_job(requested)
            await self.supervisor.emit(requested, "job.cancelled", {})
        return requested

    async def recover(self, *, limit: int = 100) -> int:
        cancelled = await self.store.finalize_cancel_requested_jobs(limit=limit)
        for job in cancelled:
            await self.supervisor.emit(job, "job.cancelled", {"recovered": True})
        requeued = await self.store.requeue_recoverable_jobs(limit=limit)
        for job in requeued:
            await self.supervisor.emit(job, "job.queued", {"recovered": True})
            self._start(job)
        return len(cancelled) + len(requeued)

    async def wait(self, context: JobContext, job_id: str) -> AgentJob:
        await self._owned(context, job_id)
        background = self._background.get(job_id)
        if background is not None:
            with suppress(asyncio.CancelledError):
                await background
        return await self._owned(context, job_id)

    def _start(self, job: AgentJob) -> None:
        if job.id in self._background and not self._background[job.id].done():
            return
        task = asyncio.create_task(self._run_job(job))
        self._background[job.id] = task
        task.add_done_callback(partial(self._done, job.id))

    def _done(self, job_id: str, task: asyncio.Task[None]) -> None:
        self._background.pop(job_id, None)
        if not task.cancelled():
            task.exception()

    async def _run_job(self, job: AgentJob) -> None:
        claimed = await self.store.claim_job(job.id)
        if claimed is None:
            return
        try:
            task = await self.runtime.execute(
                claimed,
                on_event=lambda event_type, payload: self.supervisor.task_event(
                    claimed, event_type, payload
                ),
            )
        except asyncio.CancelledError:
            current = await self.store.get_job(job.id) or claimed
            now = utc_now()
            cancelled = current.model_copy(
                update={
                    "status": JobStatus.CANCELLED,
                    "completed_at": now,
                    "updated_at": now,
                }
            )
            await self.store.save_job(cancelled)
            await self.supervisor.emit(cancelled, "job.cancelled", {})
            raise
        except Exception as error:  # noqa: BLE001 - isolates background runtime failures
            current = await self.store.get_job(job.id) or claimed
            failed = await self.supervisor.fail(
                current, code="job_runtime_error", message=str(error)
            )
            await self._send_to_dead_letter(failed, attempts=failed.retry_policy.max_attempts)
        else:
            finalized = await self.supervisor.finalize(claimed, task)
            if finalized.status == JobStatus.FAILED:
                await self._send_to_dead_letter(finalized, attempts=task.attempts)

    async def _send_to_dead_letter(self, job: AgentJob, *, attempts: int) -> None:
        if self.dead_letters is None:
            return
        await self.dead_letters.enqueue(
            DeadLetterContext(
                tenant_id=job.tenant_id,
                user_id=job.user_id,
                trace_id=job.trace_id,
            ),
            kind=DeadLetterKind.JOB,
            reference_id=job.id,
            reason_code=job.error_code or "job_failed",
            reason=job.error_message or "Background job failed",
            payload={
                "task_id": job.task_id,
                "worker_agent_id": job.worker_agent_id,
                "parent_job_id": job.parent_job_id,
            },
            attempts=attempts,
        )

    async def _owned(self, context: JobContext, job_id: str) -> AgentJob:
        job = await self.store.get_job(job_id)
        if job is None or job.tenant_id != context.tenant_id or job.user_id != context.user_id:
            raise KeyError("Job not found in trusted owner scope")
        return job

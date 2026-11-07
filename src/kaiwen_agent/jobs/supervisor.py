from __future__ import annotations

from typing import Protocol

from kaiwen_agent.events import AgentEvent, EventSink, EventType
from kaiwen_agent.jobs.models import AgentJob, JobStatus
from kaiwen_agent.jobs.store import JobStore
from kaiwen_agent.tasks import AgentTask, TaskStatus
from kaiwen_agent.types import utc_now


class JobCompletionNotifier(Protocol):
    async def notify(self, job: AgentJob) -> None: ...


class JobSupervisor:
    """Deterministically projects Task Runtime state into durable job state."""

    def __init__(
        self,
        *,
        store: JobStore,
        events: EventSink,
        completion_notifier: JobCompletionNotifier | None = None,
    ) -> None:
        self.store = store
        self.events = events
        self.completion_notifier = completion_notifier

    async def task_event(
        self,
        job: AgentJob,
        event_type: EventType,
        payload: dict[str, object],
    ) -> None:
        current = await self.store.get_job(job.id)
        if current is None or current.terminal:
            return
        if event_type == "task.created":
            await self.emit(current, "job.assigned", {"worker": current.worker_agent_id})
        elif event_type == "task.started":
            now = utc_now()
            current = current.model_copy(
                update={
                    "status": JobStatus.RUNNING,
                    "started_at": now,
                    "updated_at": now,
                }
            )
            await self.store.save_job(current)
            await self.emit(current, "job.started", {})
        elif event_type == "task.progress":
            progress_value = payload.get("progress", current.progress)
            if isinstance(progress_value, bool) or not isinstance(progress_value, (int, float)):
                raise ValueError("Task progress event must contain a number")
            progress = float(progress_value)
            current = current.model_copy(update={"progress": progress, "updated_at": utc_now()})
            await self.store.save_job(current)
            await self.emit(current, "job.progress", {"progress": progress})
            await self.update_parent_progress(current)

    async def finalize(self, job: AgentJob, task: AgentTask) -> AgentJob:
        current = await self.store.get_job(job.id) or job
        now = utc_now()
        if task.status == TaskStatus.COMPLETED:
            status = JobStatus.COMPLETED
            event_type: EventType = "job.completed"
            progress = 1.0
        elif task.status == TaskStatus.CANCELLED:
            status = JobStatus.CANCELLED
            event_type = "job.cancelled"
            progress = current.progress
        else:
            status = JobStatus.FAILED
            event_type = "job.failed"
            progress = current.progress
        updated = current.model_copy(
            update={
                "status": status,
                "progress": progress,
                "result": task.output,
                "error_code": task.error_code,
                "error_message": task.error,
                "completed_at": now,
                "updated_at": now,
            }
        )
        await self.store.save_job(updated)
        await self.emit(
            updated,
            event_type,
            {"error_code": task.error_code} if task.error_code else {},
        )
        if self.completion_notifier is not None and status in {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
        }:
            await self.completion_notifier.notify(updated)
        await self.update_parent_progress(updated)
        return updated

    async def fail(self, job: AgentJob, *, code: str, message: str) -> AgentJob:
        now = utc_now()
        updated = job.model_copy(
            update={
                "status": JobStatus.FAILED,
                "error_code": code,
                "error_message": message,
                "completed_at": now,
                "updated_at": now,
            }
        )
        await self.store.save_job(updated)
        await self.emit(updated, "job.failed", {"error_code": code})
        if self.completion_notifier is not None:
            await self.completion_notifier.notify(updated)
        await self.update_parent_progress(updated)
        return updated

    async def update_parent_progress(self, child: AgentJob) -> None:
        if child.parent_job_id is None:
            return
        parent = await self.store.get_job(child.parent_job_id)
        if parent is None or parent.terminal:
            return
        children = await self.store.list_jobs(
            tenant_id=child.tenant_id,
            user_id=child.user_id,
            parent_job_id=parent.id,
            limit=1000,
        )
        if not children:
            return
        progress = sum(1.0 if item.terminal else item.progress for item in children) / len(children)
        updated = parent.model_copy(update={"progress": progress, "updated_at": utc_now()})
        await self.store.save_job(updated)
        await self.emit(updated, "job.progress", {"progress": progress, "from_children": True})

    async def emit(self, job: AgentJob, event_type: EventType, payload: dict[str, object]) -> None:
        await self.events.append(
            AgentEvent(
                type=event_type,
                trace_id=job.trace_id,
                session_id=job.session_id,
                tenant_id=job.tenant_id,
                user_id=job.user_id,
                payload={
                    "job_id": job.id,
                    "task_id": job.task_id,
                    "parent_job_id": job.parent_job_id,
                    **payload,
                },
            )
        )

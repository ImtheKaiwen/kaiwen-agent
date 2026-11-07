from __future__ import annotations

import asyncio
from typing import Protocol

from kaiwen_agent.jobs.models import TERMINAL_JOB_STATUSES, AgentJob, JobStatus
from kaiwen_agent.types import utc_now


class JobStore(Protocol):
    async def save_job(self, job: AgentJob) -> None: ...

    async def get_job(self, job_id: str) -> AgentJob | None: ...

    async def list_jobs(
        self,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
        conversation_id: str | None = None,
        parent_job_id: str | None = None,
        status: JobStatus | None = None,
        limit: int = 100,
    ) -> list[AgentJob]: ...

    async def count_jobs(
        self,
        *,
        tenant_id: str,
        user_id: str,
        requested_by_agent_id: str | None = None,
        parent_run_id: str | None = None,
        active_only: bool = False,
    ) -> int: ...

    async def claim_job(self, job_id: str) -> AgentJob | None: ...

    async def requeue_recoverable_jobs(self, *, limit: int = 100) -> list[AgentJob]: ...

    async def finalize_cancel_requested_jobs(self, *, limit: int = 100) -> list[AgentJob]: ...


class InMemoryJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, AgentJob] = {}
        self._lock = asyncio.Lock()

    async def save_job(self, job: AgentJob) -> None:
        async with self._lock:
            self._jobs[job.id] = job.model_copy(deep=True)

    async def get_job(self, job_id: str) -> AgentJob | None:
        async with self._lock:
            job = self._jobs.get(job_id)
            return job.model_copy(deep=True) if job else None

    async def list_jobs(
        self,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
        conversation_id: str | None = None,
        parent_job_id: str | None = None,
        status: JobStatus | None = None,
        limit: int = 100,
    ) -> list[AgentJob]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        async with self._lock:
            jobs = [
                job
                for job in self._jobs.values()
                if (tenant_id is None or job.tenant_id == tenant_id)
                and (user_id is None or job.user_id == user_id)
                and (conversation_id is None or job.conversation_id == conversation_id)
                and (parent_job_id is None or job.parent_job_id == parent_job_id)
                and (status is None or job.status == status)
            ]
            jobs.sort(key=lambda job: (job.priority, job.created_at), reverse=True)
            return [job.model_copy(deep=True) for job in jobs[:limit]]

    async def count_jobs(
        self,
        *,
        tenant_id: str,
        user_id: str,
        requested_by_agent_id: str | None = None,
        parent_run_id: str | None = None,
        active_only: bool = False,
    ) -> int:
        async with self._lock:
            return sum(
                1
                for job in self._jobs.values()
                if job.tenant_id == tenant_id
                and job.user_id == user_id
                and (
                    requested_by_agent_id is None
                    or job.requested_by_agent_id == requested_by_agent_id
                )
                and (parent_run_id is None or job.parent_run_id == parent_run_id)
                and (not active_only or not job.terminal)
            )

    async def claim_job(self, job_id: str) -> AgentJob | None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != JobStatus.QUEUED:
                return None
            now = utc_now()
            claimed = job.model_copy(
                update={
                    "status": JobStatus.RUNNING,
                    "started_at": job.started_at or now,
                    "updated_at": now,
                }
            )
            self._jobs[job_id] = claimed
            return claimed.model_copy(deep=True)

    async def requeue_recoverable_jobs(self, *, limit: int = 100) -> list[AgentJob]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        async with self._lock:
            recoverable = [
                job
                for job in self._jobs.values()
                if job.status not in TERMINAL_JOB_STATUSES
                and job.status != JobStatus.CANCEL_REQUESTED
            ][:limit]
            now = utc_now()
            requeued: list[AgentJob] = []
            for job in recoverable:
                updated = job.model_copy(
                    update={
                        "status": JobStatus.QUEUED,
                        "queued_at": now,
                        "started_at": None,
                        "updated_at": now,
                    }
                )
                self._jobs[job.id] = updated
                requeued.append(updated.model_copy(deep=True))
            return requeued

    async def finalize_cancel_requested_jobs(self, *, limit: int = 100) -> list[AgentJob]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        async with self._lock:
            pending = [
                job for job in self._jobs.values() if job.status == JobStatus.CANCEL_REQUESTED
            ][:limit]
            now = utc_now()
            cancelled: list[AgentJob] = []
            for job in pending:
                updated = job.model_copy(
                    update={
                        "status": JobStatus.CANCELLED,
                        "completed_at": now,
                        "updated_at": now,
                    }
                )
                self._jobs[job.id] = updated
                cancelled.append(updated.model_copy(deep=True))
            return cancelled

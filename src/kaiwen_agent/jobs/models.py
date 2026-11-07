from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kaiwen_agent.tasks.models import TaskRetryPolicy
from kaiwen_agent.types import utc_now


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    WAITING_APPROVAL = "waiting_approval"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


TERMINAL_JOB_STATUSES = {
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
}


class AgentJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"job_{uuid4().hex}")
    display_id: str = Field(default_factory=lambda: f"JOB-{uuid4().hex[:8].upper()}")
    task_id: str = Field(default_factory=lambda: f"task_{uuid4().hex}")
    trace_id: str = Field(default_factory=lambda: f"trace_{uuid4().hex}")
    parent_job_id: str | None = None
    parent_run_id: str | None = None
    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    conversation_id: str | None = None
    session_id: str = Field(min_length=1)
    requested_by_agent_id: str = Field(min_length=1)
    worker_agent_id: str = Field(min_length=1)
    capabilities: frozenset[str] = Field(default_factory=frozenset)
    delegation_depth: int = Field(default=0, ge=0)
    objective: str = Field(min_length=1, max_length=2_000)
    input: dict[str, Any] = Field(default_factory=dict)
    context_ref: dict[str, str] = Field(default_factory=dict)
    status: JobStatus = JobStatus.QUEUED
    progress: float = Field(default=0, ge=0, le=1)
    priority: int = Field(default=0, ge=-1000, le=1000)
    retry_policy: TaskRetryPolicy = Field(default_factory=TaskRetryPolicy)
    timeout_seconds: float | None = Field(default=None, gt=0)
    resource_keys: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    queued_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)
    result: dict[str, Any] | None = None
    result_ref: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    @field_validator("created_at", "queued_at", "started_at", "completed_at", "updated_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Job timestamps must be timezone-aware")
        return value

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_JOB_STATUSES


class AgentJobRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    display_id: str
    task_id: str
    trace_id: str
    status: JobStatus

    @classmethod
    def from_job(cls, job: AgentJob) -> AgentJobRef:
        return cls(
            id=job.id,
            display_id=job.display_id,
            task_id=job.task_id,
            trace_id=job.trace_id,
            status=job.status,
        )


class JobSubmitRequest(BaseModel):
    """Job data without caller identity; identity comes from trusted context."""

    model_config = ConfigDict(extra="forbid")

    worker_agent_id: str = Field(min_length=1)
    objective: str = Field(min_length=1, max_length=2_000)
    input: dict[str, Any] = Field(default_factory=dict)
    context_ref: dict[str, str] = Field(default_factory=dict)
    parent_job_id: str | None = None
    priority: int = Field(default=0, ge=-1000, le=1000)
    retry_policy: TaskRetryPolicy = Field(default_factory=TaskRetryPolicy)
    timeout_seconds: float | None = Field(default=None, gt=0)
    resource_keys: list[str] = Field(default_factory=list)


class JobContext(BaseModel):
    """Trusted host identity used for job visibility and ownership."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    conversation_id: str | None = None
    agent_id: str = Field(min_length=1)
    run_id: str | None = None
    trace_id: str | None = None
    capabilities: frozenset[str] = Field(default_factory=frozenset)
    delegation_depth: int = Field(default=0, ge=0)

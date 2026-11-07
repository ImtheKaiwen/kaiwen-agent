from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.types import utc_now


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_DEPENDENCY = "waiting_dependency"
    WAITING_RESOURCE = "waiting_resource"
    WAITING_USER = "waiting_user"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_TASK_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
}


class TaskRetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=1, ge=1)
    backoff_seconds: float = Field(default=0, ge=0)
    backoff_multiplier: float = Field(default=1, ge=1)
    retry_on: frozenset[str] = Field(default_factory=lambda: frozenset({"timeout"}))

    def delay_for_attempt(self, attempt: int) -> float:
        return self.backoff_seconds * (self.backoff_multiplier ** max(0, attempt - 1))


class AgentTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"task_{uuid4().hex}")
    name: str = Field(min_length=1)
    description: str = ""
    agent_id: str = Field(min_length=1)
    session_id: str = "default"
    tenant_id: str | None = None
    user_id: str | None = None
    conversation_id: str | None = None
    trace_id: str = Field(default_factory=lambda: f"trace_{uuid4().hex}")
    status: TaskStatus = TaskStatus.PENDING
    parent_id: str | None = None
    dependencies: list[str] = Field(default_factory=list)
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] | None = None
    resource_keys: list[str] = Field(default_factory=list)
    retry_policy: TaskRetryPolicy = Field(default_factory=TaskRetryPolicy)
    timeout_seconds: float | None = Field(default=None, gt=0)
    attempts: int = Field(default=0, ge=0)
    progress: float | None = Field(default=None, ge=0, le=1)
    error: str | None = None
    error_code: str | None = None
    capabilities: frozenset[str] = Field(default_factory=frozenset)
    delegation_depth: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_TASK_STATUSES

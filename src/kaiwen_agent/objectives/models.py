from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.types import utc_now


class ObjectiveStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    WAITING = "waiting"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_OBJECTIVE_STATUSES = {
    ObjectiveStatus.COMPLETED,
    ObjectiveStatus.FAILED,
    ObjectiveStatus.CANCELLED,
}


class ObjectiveContext(BaseModel):
    """Trusted host identity; never populated from model arguments."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    conversation_id: str | None = None
    trace_id: str = Field(min_length=1)


class ObjectiveCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=4_000)
    success_condition: str = Field(min_length=1, max_length=2_000)
    priority: int = Field(default=0, ge=-100, le=100)
    parent_objective_id: str | None = None


class Objective(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"obj_{uuid4().hex}")
    tenant_id: str
    user_id: str
    conversation_id: str | None = None
    agent_id: str
    title: str
    description: str
    success_condition: str
    status: ObjectiveStatus = ObjectiveStatus.PENDING
    priority: int = 0
    parent_objective_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_OBJECTIVE_STATUSES

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.types import utc_now


class DeadLetterKind(StrEnum):
    EVENT = "event"
    JOB = "job"
    TASK = "task"
    WATCH = "watch"
    WEBHOOK = "webhook"


class DeadLetterStatus(StrEnum):
    PENDING = "pending"
    REQUEUED = "requeued"
    RESOLVED = "resolved"


class DeadLetterContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1)
    user_id: str | None = None
    trace_id: str = Field(min_length=1)


class DeadLetterRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"dlq_{uuid4().hex}")
    tenant_id: str = Field(min_length=1)
    owner_user_id: str | None = None
    trace_id: str = Field(min_length=1)
    kind: DeadLetterKind
    reference_id: str = Field(min_length=1)
    reason_code: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=2_000)
    payload: dict[str, Any] = Field(default_factory=dict)
    attempts: int = Field(default=0, ge=0)
    requeue_count: int = Field(default=0, ge=0)
    status: DeadLetterStatus = DeadLetterStatus.PENDING
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

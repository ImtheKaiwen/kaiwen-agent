from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kaiwen_agent.types import utc_now

TriggerScalar = str | int | float | bool | None


class TriggerEvent(BaseModel):
    """Untrusted event data plus host-supplied routing scope."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"trgevt_{uuid4().hex}")
    type: str = Field(min_length=1, max_length=200)
    tenant_id: str | None = None
    user_id: str | None = None
    conversation_id: str | None = None
    resource: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=utc_now)

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value


class TriggerRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    event_type: str = Field(min_length=1, max_length=200)
    agent_id: str = Field(min_length=1)
    tenant_id: str | None = None
    payload_equals: dict[str, TriggerScalar] = Field(default_factory=dict)
    debounce_seconds: float = Field(default=0, ge=0, le=86_400)
    coalesce_seconds: float = Field(default=0, ge=0, le=86_400)
    dedupe_ttl_seconds: float = Field(default=86_400, gt=0, le=604_800)
    priority: int = Field(default=0, ge=-100, le=100)


class TriggerBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    rule_id: str
    agent_id: str
    tenant_id: str | None = None
    user_id: str | None = None
    conversation_id: str | None = None
    event_ids: list[str]
    event_types: list[str]
    first_event_at: datetime
    last_event_at: datetime
    due_at: datetime
    window_ends_at: datetime
    priority: int


class TriggerRouteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    duplicate: bool = False
    matched_rule_ids: list[str] = Field(default_factory=list)
    pending_batch_keys: list[str] = Field(default_factory=list)

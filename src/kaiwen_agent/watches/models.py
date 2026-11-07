from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kaiwen_agent.types import utc_now


class WatchStrategy(StrEnum):
    EVENT = "event"
    WEBHOOK = "webhook"
    POLLING = "polling"


class WatchStatus(StrEnum):
    ACTIVE = "active"
    MATCHED = "matched"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class WatchOperator(StrEnum):
    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    IN = "in"
    CONTAINS = "contains"
    GREATER_THAN_OR_EQUAL = "greater_than_or_equal"
    LESS_THAN_OR_EQUAL = "less_than_or_equal"


class WatchCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1, max_length=300)
    operator: WatchOperator = WatchOperator.EQUALS
    value: Any


class WatchContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    conversation_id: str | None = None
    trace_id: str = Field(min_length=1)


class WatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource: str = Field(min_length=1, max_length=500)
    condition: WatchCondition
    strategy: WatchStrategy
    objective_id: str | None = None
    job_id: str | None = None
    expires_at: datetime
    polling_interval_seconds: float | None = Field(default=None, gt=0, le=86_400)

    @model_validator(mode="after")
    def validate_polling(self) -> WatchCreate:
        if self.expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        if self.strategy == WatchStrategy.POLLING and self.polling_interval_seconds is None:
            raise ValueError("Polling watches require polling_interval_seconds")
        if self.strategy != WatchStrategy.POLLING and self.polling_interval_seconds is not None:
            raise ValueError("Only polling watches accept polling_interval_seconds")
        return self


class Watch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"watch_{uuid4().hex}")
    tenant_id: str
    user_id: str
    conversation_id: str | None = None
    agent_id: str
    objective_id: str | None = None
    job_id: str | None = None
    resource: str
    condition: WatchCondition
    strategy: WatchStrategy
    status: WatchStatus = WatchStatus.ACTIVE
    polling_interval_seconds: float | None = None
    next_check_at: datetime | None = None
    expires_at: datetime
    created_at: datetime = Field(default_factory=utc_now)
    matched_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)

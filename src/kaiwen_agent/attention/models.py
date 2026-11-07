from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.types import utc_now


class AttentionSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ACTION_REQUIRED = "action_required"
    CRITICAL = "critical"


class AttentionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    conversation_id: str | None = None
    trace_id: str = Field(min_length=1)


class AttentionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: AttentionSeverity
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=2_000)
    source: str = Field(min_length=1, max_length=200)
    resource: str | None = Field(default=None, max_length=500)


class AttentionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"attn_{uuid4().hex}")
    tenant_id: str
    user_id: str
    conversation_id: str | None = None
    agent_id: str
    severity: AttentionSeverity
    title: str
    summary: str
    source: str
    resource: str | None = None
    include_next_cycle: bool = False
    wakeup_id: str | None = None
    notification_requested: bool = False
    created_at: datetime = Field(default_factory=utc_now)

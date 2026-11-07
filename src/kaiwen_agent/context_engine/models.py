from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContextKind(StrEnum):
    SYSTEM = "system"
    POLICY = "policy"
    CURRENT_INPUT = "current_input"
    RECENT_CONVERSATION = "recent_conversation"
    SESSION_SUMMARY = "session_summary"
    MEMORY = "memory"
    KNOWLEDGE = "knowledge"
    TASK = "task"
    JOB = "job"
    OBJECTIVE = "objective"
    SYSTEM_STATE = "system_state"


class ContextBuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1)
    tenant_id: str | None = None
    user_id: str | None = None
    session_id: str = Field(min_length=1)
    conversation_id: str | None = None
    query: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    trace_id: str = Field(default_factory=lambda: f"trace_{uuid4().hex}")
    capabilities: frozenset[str] = Field(default_factory=frozenset)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"ctx_{uuid4().hex}")
    source: str = Field(min_length=1)
    kind: ContextKind
    content: str = Field(min_length=1)
    priority: int = Field(default=0, ge=-1000, le=1000)
    relevance: float = Field(default=1, ge=0, le=1)
    required: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_total_tokens: int = Field(default=30_000, ge=1)
    system: int = Field(default=4_000, ge=0)
    policy: int = Field(default=2_000, ge=0)
    current_input: int = Field(default=4_000, ge=1)
    recent_conversation: int = Field(default=6_000, ge=0)
    session_summary: int = Field(default=1_500, ge=0)
    memory: int = Field(default=4_000, ge=0)
    knowledge: int = Field(default=8_000, ge=0)
    task: int = Field(default=1_500, ge=0)
    job: int = Field(default=1_500, ge=0)
    objective: int = Field(default=1_000, ge=0)
    system_state: int = Field(default=1_000, ge=0)

    def limit_for(self, kind: ContextKind) -> int:
        return int(getattr(self, kind.value))


class ContextWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_context_tokens: int = Field(ge=2)
    reserved_output_tokens: int = Field(default=0, ge=0)
    reserved_request_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def require_input_capacity(self) -> ContextWindow:
        if self.reserved_output_tokens + self.reserved_request_tokens >= self.max_context_tokens:
            raise ValueError("Context window reservations leave no input capacity")
        return self

    @property
    def available_input_tokens(self) -> int:
        return self.max_context_tokens - self.reserved_output_tokens - self.reserved_request_tokens


class BuiltContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ContextItem]
    estimated_tokens: int = Field(ge=0)
    truncated: bool = False
    omitted_sources: list[str] = Field(default_factory=list)

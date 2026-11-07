from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kaiwen_agent.types import utc_now


class SessionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"sum_{uuid4().hex}")
    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    summary: str = ""
    goal: str | None = None
    decisions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    active_projects: list[str] = Field(default_factory=list)
    completed: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    known_constraints: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    source_message_ids: list[str] = Field(default_factory=list)
    source_message_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None

    @field_validator("created_at", "updated_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Session summary timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def require_content(self) -> SessionSummary:
        if not (
            self.summary.strip()
            or (self.goal is not None and self.goal.strip())
            or self.decisions
            or self.open_questions
            or self.active_projects
            or self.completed
            or self.next_steps
            or self.known_constraints
            or self.artifact_refs
        ):
            raise ValueError("Session summary must contain at least one value")
        return self

    def render(self) -> str:
        sections: list[str] = []
        if self.goal:
            sections.append(f"Goal: {self.goal}")
        if self.summary:
            sections.append(f"Summary: {self.summary}")
        if self.decisions:
            sections.append("Decisions: " + "; ".join(self.decisions))
        if self.open_questions:
            sections.append("Open questions: " + "; ".join(self.open_questions))
        if self.active_projects:
            sections.append("Active projects: " + "; ".join(self.active_projects))
        if self.completed:
            sections.append("Completed: " + "; ".join(self.completed))
        if self.next_steps:
            sections.append("Next steps: " + "; ".join(self.next_steps))
        if self.known_constraints:
            sections.append("Known constraints: " + "; ".join(self.known_constraints))
        if self.artifact_refs:
            sections.append("Artifact references: " + "; ".join(self.artifact_refs))
        return "\n".join(sections)


class MemoryType(StrEnum):
    EPISODIC = "episodic"
    USER = "user"
    PROJECT = "project"
    SEMANTIC = "semantic"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    PENDING_REVIEW = "pending_review"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    DISPUTED = "disputed"
    DELETED = "deleted"


class MemorySensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"
    SECRET = "secret"


class MemoryAdmissionDecision(StrEnum):
    AUTO_SAVE = "auto_save"
    REVIEW = "review"
    DISCARD = "discard"


class MemoryOperationContext(BaseModel):
    """Trusted identity supplied by the host, never by model-generated arguments."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    conversation_id: str | None = None
    run_id: str | None = None
    environment: str = "development"
    capabilities: frozenset[str] = Field(default_factory=frozenset)


class MemoryCandidate(BaseModel):
    """Untrusted proposed memory content without authority-bearing identity fields."""

    model_config = ConfigDict(extra="forbid")

    namespace: str = Field(min_length=3, max_length=200)
    type: MemoryType
    subject: str = Field(min_length=1, max_length=200)
    key: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=20_000)
    structured_data: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.5, ge=0, le=1)
    importance: float = Field(default=0.5, ge=0, le=1)
    sensitivity: MemorySensitivity = MemorySensitivity.INTERNAL
    future_utility: float = Field(default=0.5, ge=0, le=1)
    stability: float = Field(default=0.5, ge=0, le=1)
    explicitness: float = Field(default=0.5, ge=0, le=1)
    scope_relevance: float = Field(default=0.5, ge=0, le=1)
    source_type: str = Field(default="conversation", min_length=1, max_length=100)
    source_conversation_id: str | None = None
    source_message_id: str | None = None
    source_run_id: str | None = None
    source_job_id: str | None = None
    expires_at: datetime | None = None

    @field_validator("namespace")
    @classmethod
    def validate_namespace(cls, value: str) -> str:
        prefix, separator, identifier = value.partition(":")
        if separator != ":" or prefix not in {"user", "project", "tenant", "system"}:
            raise ValueError("namespace must use user:, project:, tenant:, or system:")
        if not identifier.strip():
            raise ValueError("namespace identifier cannot be empty")
        return value

    @field_validator("expires_at")
    @classmethod
    def require_expiry_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Memory expiry must be timezone-aware")
        return value


class MemoryAdmissionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: MemoryAdmissionDecision
    score: float = Field(ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)


class MemoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"mem_{uuid4().hex}")
    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    namespace: str = Field(min_length=3, max_length=200)
    type: MemoryType
    subject: str = Field(min_length=1, max_length=200)
    key: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=20_000)
    structured_data: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(ge=0, le=1)
    importance: float = Field(ge=0, le=1)
    sensitivity: MemorySensitivity
    status: MemoryStatus = MemoryStatus.ACTIVE
    supersedes_id: str | None = None
    source_type: str = Field(min_length=1, max_length=100)
    source_conversation_id: str | None = None
    source_message_id: str | None = None
    source_run_id: str | None = None
    source_job_id: str | None = None
    embedding: list[float] | None = Field(default=None, exclude=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_accessed_at: datetime | None = None
    expires_at: datetime | None = None

    @field_validator("created_at", "updated_at", "last_accessed_at", "expires_at")
    @classmethod
    def require_item_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Memory timestamps must be timezone-aware")
        return value


class MemorySearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    namespaces: frozenset[str] = Field(min_length=1)
    text: str = Field(min_length=1, max_length=8_000)
    top_k: int = Field(default=8, ge=1, le=50)


class MemorySearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item: MemoryItem
    score: float = Field(ge=0, le=1)
    lexical_score: float = Field(default=0, ge=0, le=1)
    semantic_score: float = Field(default=0, ge=0, le=1)

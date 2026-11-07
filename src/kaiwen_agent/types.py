from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class AgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class ModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    provider_response_id: str | None = None
    provider_state: Any | None = None
    structured_output: Any | None = None
    usage: ModelUsage | None = None
    model: str | None = None


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str
    name: str
    output: Any


class AgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    session_id: str
    text: str
    status: Literal["completed", "failed"]
    tool_results: list[ToolResult] = Field(default_factory=list)
    structured_output: Any | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)


class AgentRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"run_{uuid4().hex}")
    session_id: str
    status: Literal["created", "running", "completed", "failed"] = "created"
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None

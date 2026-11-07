from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from kaiwen_agent.context import AgentContext

ToolHandler = Callable[[AgentContext, BaseModel], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 1
    backoff_seconds: float = 0.0
    retry_on: tuple[type[Exception], ...] = (TimeoutError,)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.backoff_seconds < 0:
            raise ValueError("backoff_seconds cannot be negative")


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: ToolHandler
    permissions: frozenset[str]
    side_effect: Literal["none", "reversible", "destructive"] = "none"
    requires_approval: bool = False
    idempotent: bool = True
    timeout_seconds: float = 30.0
    retry_policy: RetryPolicy = RetryPolicy()

    @property
    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

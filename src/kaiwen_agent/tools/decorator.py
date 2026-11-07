from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, TypeVar, cast

from pydantic import BaseModel

from kaiwen_agent.tools.definition import RetryPolicy, ToolDefinition, ToolHandler

F = TypeVar("F", bound=Callable[..., Any])


def tool(
    *,
    name: str,
    description: str,
    input_model: type[BaseModel],
    permissions: set[str] | frozenset[str] | None = None,
    side_effect: Literal["none", "reversible", "destructive"] = "none",
    requires_approval: bool = False,
    idempotent: bool = True,
    timeout_seconds: float = 30.0,
    retry_policy: RetryPolicy | None = None,
) -> Callable[[F], ToolDefinition]:
    def decorate(function: F) -> ToolDefinition:
        return ToolDefinition(
            name=name,
            description=description,
            input_model=input_model,
            handler=cast(ToolHandler, function),
            permissions=frozenset(permissions or set()),
            side_effect=side_effect,
            requires_approval=requires_approval,
            idempotent=idempotent,
            timeout_seconds=timeout_seconds,
            retry_policy=retry_policy or RetryPolicy(),
        )

    return decorate

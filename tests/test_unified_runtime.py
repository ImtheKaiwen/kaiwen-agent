from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import BaseModel

from kaiwen_agent import AgentContext, tool
from kaiwen_agent.exceptions import PermissionDeniedError, ToolNotFoundError
from kaiwen_agent.runtime import (
    AgentRuntime,
    AutonomyLevel,
    RunMode,
    RuntimeIdentity,
    RuntimeModeUnavailableError,
)
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.types import AgentInput, ModelResponse, ToolCall, ToolResult


class CapturingProvider:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def respond(
        self,
        agent_input: AgentInput,
        *,
        context: AgentContext,
        tools: Sequence[ToolDefinition],
        tool_results: Sequence[ToolResult],
        provider_state: Any | None,
    ) -> ModelResponse:
        self.calls.append(
            {
                "input": agent_input,
                "context": context,
                "tools": tuple(tools),
                "tool_results": tuple(tool_results),
                "provider_state": provider_state,
            }
        )
        return self.responses.pop(0)


class EchoInput(BaseModel):
    value: str


@tool(
    name="echo.read",
    description="Echo an allowed value",
    input_model=EchoInput,
    permissions={"echo.read"},
)
async def echo(context: AgentContext, arguments: EchoInput) -> dict[str, str]:
    return {
        "value": arguments.value,
        "tenant_id": context.tenant_id or "",
        "service": str(context.services.get("service", "")),
    }


def identity(*, permissions: frozenset[str] = frozenset()) -> RuntimeIdentity:
    return RuntimeIdentity(
        session_id="session_1",
        tenant_id="tenant_1",
        user_id="user_1",
        conversation_id="conversation_1",
        trace_id="trace_1",
        environment="production",
        permissions=permissions,
    )


def test_chat_fast_path_exposes_no_tools_and_calls_provider_once() -> None:
    async def scenario() -> None:
        provider = CapturingProvider([ModelResponse(text="Hello")])
        runtime = AgentRuntime(
            name="assistant",
            model=provider,
            tools=[echo],
            permissions={"echo.read"},
        )
        result = await runtime.chat("Hi", identity=identity(permissions=frozenset({"echo.read"})))
        assert result.text == "Hello"
        assert len(provider.calls) == 1
        assert provider.calls[0]["tools"] == ()
        context = provider.calls[0]["context"]
        assert context.metadata["runtime_mode"] == "chat"
        assert context.metadata["autonomy_level"] == "supervised"

    asyncio.run(scenario())


def test_action_exposes_only_explicit_tools_and_preserves_trusted_scope() -> None:
    async def scenario() -> None:
        provider = CapturingProvider(
            [
                ModelResponse(
                    tool_calls=[ToolCall(id="call_1", name="echo.read", arguments={"value": "ok"})]
                ),
                ModelResponse(text="Done"),
            ]
        )
        runtime = AgentRuntime(
            name="assistant",
            model=provider,
            tools=[echo],
            permissions={"echo.read"},
            services={"service": "default"},
        )
        result = await runtime.action(
            "Read",
            identity=identity(permissions=frozenset({"echo.read"})),
            tool_names=("echo.read",),
            autonomy=AutonomyLevel.MANUAL,
            services={"service": "request"},
        )
        assert result.tool_results[0].output == {
            "value": "ok",
            "tenant_id": "tenant_1",
            "service": "request",
        }
        assert [tool.name for tool in provider.calls[0]["tools"]] == ["echo.read"]
        assert provider.calls[0]["context"].metadata["autonomy_level"] == "manual"

    asyncio.run(scenario())


def test_action_fails_closed_for_unknown_or_unpermitted_tools() -> None:
    runtime = AgentRuntime(
        name="assistant",
        model=CapturingProvider([]),
        tools=[echo],
        permissions={"echo.read"},
    )
    with pytest.raises(ToolNotFoundError):
        asyncio.run(
            runtime.action(
                "Read",
                identity=identity(permissions=frozenset({"echo.read"})),
                tool_names=("unknown.read",),
            )
        )

    denied_provider = CapturingProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="call_1", name="echo.read", arguments={"value": "no"})]
            )
        ]
    )
    denied = AgentRuntime(
        name="assistant",
        model=denied_provider,
        tools=[echo],
        permissions={"echo.read"},
    )
    with pytest.raises(PermissionDeniedError):
        asyncio.run(
            denied.action(
                "Read",
                identity=identity(),
                tool_names=("echo.read",),
            )
        )


def test_chat_rejects_tool_exposure_and_workflow_is_not_implicitly_enabled() -> None:
    runtime = AgentRuntime(name="assistant", model=CapturingProvider([]), tools=[echo])
    with pytest.raises(ValueError, match="does not expose tools"):
        asyncio.run(
            runtime.run(
                "Hi",
                identity=identity(),
                mode=RunMode.CHAT,
                tool_names=("echo.read",),
            )
        )
    with pytest.raises(RuntimeModeUnavailableError, match="durable workflow controller"):
        asyncio.run(
            runtime.run(
                "Build",
                identity=identity(),
                mode=RunMode.WORKFLOW,
                autonomy=AutonomyLevel.AUTONOMOUS,
            )
        )

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import BaseModel

from kaiwen_agent import Agent, AgentContext, tool
from kaiwen_agent.events import InMemoryEventSink
from kaiwen_agent.exceptions import PermissionDeniedError
from kaiwen_agent.models.fake import FakeModelProvider
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.types import (
    AgentInput,
    ModelResponse,
    ModelUsage,
    ToolCall,
    ToolResult,
)


class GreetingInput(BaseModel):
    name: str


@tool(
    name="greeting.create",
    description="Create a greeting",
    input_model=GreetingInput,
    permissions={"greeting.read"},
)
async def create_greeting(context: AgentContext, arguments: GreetingInput) -> dict[str, str]:
    del context
    return {"message": f"Merhaba {arguments.name}"}


def test_agent_executes_tool_and_completes() -> None:
    events = InMemoryEventSink()
    provider = FakeModelProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="call_1", name="greeting.create", arguments={"name": "Kaiwen"})
                ]
            ),
            ModelResponse(text="Selamlama hazır."),
        ]
    )
    agent = Agent(
        name="example",
        model=provider,
        tools=[create_greeting],
        permissions={"greeting.read"},
        event_sink=events,
    )

    result = asyncio.run(
        agent.run(
            AgentInput(text="Beni selamla"),
            context=AgentContext(session_id="session_1", permissions=frozenset({"greeting.read"})),
        )
    )

    assert result.status == "completed"
    assert result.text == "Selamlama hazır."
    assert result.tool_results[0].output == {"message": "Merhaba Kaiwen"}
    assert [event.type for event in events.events] == [
        "run.started",
        "policy.allowed",
        "policy.allowed",
        "tool.started",
        "tool.completed",
        "run.completed",
    ]


def test_permissions_are_intersection_of_agent_and_context() -> None:
    provider = FakeModelProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="call_1", name="greeting.create", arguments={"name": "Kaiwen"})
                ]
            )
        ]
    )
    agent = Agent(
        name="example",
        model=provider,
        tools=[create_greeting],
        permissions={"greeting.read"},
    )

    with pytest.raises(PermissionDeniedError):
        asyncio.run(
            agent.run(
                AgentInput(text="Beni selamla"),
                context=AgentContext(session_id="session_1"),
            )
        )


def test_model_usage_is_accumulated_and_emitted() -> None:
    events = InMemoryEventSink()
    agent = Agent(
        name="usage-test",
        model=FakeModelProvider(
            [
                ModelResponse(
                    text="Tamam.",
                    model="test-model",
                    usage=ModelUsage(input_tokens=8, output_tokens=3, total_tokens=11),
                )
            ]
        ),
        event_sink=events,
    )

    result = asyncio.run(
        agent.run(
            AgentInput(text="Test"),
            context=AgentContext(session_id="session_1"),
        )
    )

    assert result.usage.total_tokens == 11
    usage_event = next(event for event in events.events if event.type == "run.status")
    assert usage_event.payload["kind"] == "model_usage"
    assert usage_event.payload["model"] == "test-model"


def test_agent_preserves_trusted_identity_in_effective_context() -> None:
    captured: AgentContext | None = None
    events = InMemoryEventSink()

    @tool(
        name="context.inspect",
        description="Inspect context",
        input_model=GreetingInput,
        permissions={"context.read"},
    )
    async def inspect_context(context: AgentContext, arguments: GreetingInput) -> dict[str, str]:
        nonlocal captured
        del arguments
        captured = context
        return {"ok": "true"}

    agent = Agent(
        name="admin-agent",
        model=FakeModelProvider(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="call_1",
                            name="context.inspect",
                            arguments={"name": "ignored"},
                        )
                    ]
                ),
                ModelResponse(text="Done"),
            ]
        ),
        tools=[inspect_context],
        permissions={"context.read"},
        event_sink=events,
    )
    asyncio.run(
        agent.run(
            AgentInput(text="Inspect"),
            context=AgentContext(
                session_id="session_1",
                tenant_id="tenant_1",
                user_id="user_1",
                conversation_id="conversation_1",
                client_id="client_1",
                environment="production",
                permissions=frozenset({"context.read"}),
            ),
        )
    )
    assert captured is not None
    assert captured.agent_id == "admin-agent"
    assert captured.tenant_id == "tenant_1"
    assert captured.user_id == "user_1"
    assert captured.conversation_id == "conversation_1"
    assert captured.client_id == "client_1"
    assert captured.environment == "production"
    assert all(event.tenant_id == "tenant_1" for event in events.events)
    assert all(event.user_id == "user_1" for event in events.events)


def test_provider_continuation_state_is_isolated_across_concurrent_runs() -> None:
    class ConcurrentProvider:
        def __init__(self) -> None:
            self.started: set[str] = set()
            self.ready = asyncio.Event()
            self.seen_continuations: dict[str, object | None] = {}

        async def respond(
            self,
            agent_input: AgentInput,
            *,
            context: AgentContext,
            tools: Sequence[ToolDefinition],
            tool_results: Sequence[ToolResult],
            provider_state: Any | None,
        ) -> ModelResponse:
            del agent_input, tools
            session_id = context.session_id
            if not tool_results:
                self.started.add(session_id)
                if len(self.started) == 2:
                    self.ready.set()
                await asyncio.wait_for(self.ready.wait(), timeout=1)
                return ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id=f"call-{session_id}",
                            name="greeting.create",
                            arguments={"name": session_id},
                        )
                    ],
                    provider_state={"session_id": session_id},
                )
            self.seen_continuations[session_id] = provider_state
            if provider_state != {"session_id": session_id}:
                raise RuntimeError("Provider state crossed run boundaries")
            return ModelResponse(text=f"completed:{session_id}")

    async def scenario() -> None:
        provider = ConcurrentProvider()
        agent = Agent(
            name="concurrent-agent",
            model=provider,
            tools=[create_greeting],
            permissions={"greeting.read"},
        )
        first, second = await asyncio.gather(
            agent.run(
                AgentInput(text="first"),
                context=AgentContext(
                    session_id="session_1",
                    permissions=frozenset({"greeting.read"}),
                ),
            ),
            agent.run(
                AgentInput(text="second"),
                context=AgentContext(
                    session_id="session_2",
                    permissions=frozenset({"greeting.read"}),
                ),
            ),
        )
        assert {first.text, second.text} == {"completed:session_1", "completed:session_2"}
        assert provider.seen_continuations == {
            "session_1": {"session_id": "session_1"},
            "session_2": {"session_id": "session_2"},
        }

    asyncio.run(scenario())

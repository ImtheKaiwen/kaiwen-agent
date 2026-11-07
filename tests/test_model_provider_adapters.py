from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from kaiwen_agent.context import AgentContext
from kaiwen_agent.model_routing import (
    DataLocality,
    ModelComplexity,
    ModelRoute,
    ModelRouter,
    ModelRouteUnavailableError,
    ModelTier,
)
from kaiwen_agent.models import (
    GeminiGenerateContentProvider,
    ModelCapabilities,
    ModelCapabilitiesRegistry,
    ModelToolStrategy,
    OllamaChatProvider,
)
from kaiwen_agent.policy import (
    PolicyContext,
    PolicyDecision,
    PolicyDecisionType,
    PolicyEngine,
    PolicyRegistry,
)
from kaiwen_agent.types import AgentInput, ToolResult

from .test_agent import create_greeting


class AllowModels:
    policy_id = "allow-models"

    async def evaluate(self, context):
        if context.action != "model.resolve":
            return None
        return PolicyDecision(
            decision=PolicyDecisionType.ALLOW,
            policy_id=self.policy_id,
            rule_id="allow",
            reason="test",
        )


def _policy_context() -> PolicyContext:
    return PolicyContext(
        tenant_id="tenant-1",
        user_id="user-1",
        agent_id="agent-1",
        session_id="session-1",
        trace_id="trace-1",
        action="test",
    )


def test_model_capabilities_are_technical_and_versioned_by_provider_model() -> None:
    registry = ModelCapabilitiesRegistry()
    entry = ModelCapabilities(
        provider="ollama",
        model="qwen3",
        context_window=32_768,
        tool_strategy=ModelToolStrategy.NATIVE,
        data_locality=DataLocality.LOCAL_ONLY,
    )
    registry.register(entry)
    assert registry.get("ollama", "qwen3") == entry
    with pytest.raises(ValueError, match="already registered"):
        registry.register(entry)


def test_model_router_never_falls_back_to_cloud_for_local_only_data() -> None:
    async def scenario() -> None:
        engine = PolicyEngine(registry=PolicyRegistry([AllowModels()]))
        cloud = ModelRoute(
            id="cloud",
            provider="gemini",
            model="cloud-model",
            tier=ModelTier.CHEAP,
            capabilities=frozenset({"reasoning"}),
            complexities=frozenset({ModelComplexity.LOW}),
            data_locality=DataLocality.CLOUD_ALLOWED,
            priority=100,
        )
        local = ModelRoute(
            id="local",
            provider="ollama",
            model="local-model",
            tier=ModelTier.CHEAP,
            capabilities=frozenset({"reasoning"}),
            complexities=frozenset({ModelComplexity.LOW}),
            data_locality=DataLocality.LOCAL_ONLY,
        )
        selected = await ModelRouter(routes=[cloud, local], policy_engine=engine).resolve(
            capability="reasoning",
            complexity=ModelComplexity.LOW,
            policy_context=_policy_context(),
            required_locality=DataLocality.LOCAL_ONLY,
        )
        assert selected.route_id == "local"
        with pytest.raises(ModelRouteUnavailableError):
            await ModelRouter(routes=[cloud], policy_engine=engine).resolve(
                capability="reasoning",
                complexity=ModelComplexity.LOW,
                policy_context=_policy_context(),
                required_locality=DataLocality.LOCAL_ONLY,
            )

    asyncio.run(scenario())


def test_model_router_returns_only_policy_approved_candidates_in_priority_order() -> None:
    class DenyBlockedRoute:
        policy_id = "deny-blocked-route"

        async def evaluate(self, context):
            if context.action == "model.resolve" and context.resource == "blocked":
                return PolicyDecision(
                    decision=PolicyDecisionType.DENY,
                    policy_id=self.policy_id,
                    rule_id="deny",
                    reason="test",
                )
            return None

    async def scenario() -> None:
        engine = PolicyEngine(registry=PolicyRegistry([DenyBlockedRoute(), AllowModels()]))

        def route(route_id: str, priority: int) -> ModelRoute:
            return ModelRoute(
                id=route_id,
                provider=f"provider-{route_id}",
                model=f"model-{route_id}",
                tier=ModelTier.CHEAP,
                capabilities=frozenset({"reasoning"}),
                complexities=frozenset({ModelComplexity.LOW}),
                priority=priority,
            )

        selections = await ModelRouter(
            routes=[route("secondary", 10), route("blocked", 100), route("primary", 20)],
            policy_engine=engine,
        ).resolve_candidates(
            capability="reasoning",
            complexity=ModelComplexity.LOW,
            policy_context=_policy_context(),
        )

        assert [selection.route_id for selection in selections] == ["primary", "secondary"]

    asyncio.run(scenario())


class FakeOllamaClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    async def chat(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        return self.responses.pop(0)


def test_ollama_native_tools_are_proposals_and_continuation_is_explicit() -> None:
    async def scenario() -> None:
        client = FakeOllamaClient(
            [
                {
                    "model": "qwen3",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "greeting__create",
                                    "arguments": {"name": "Kaiwen"},
                                }
                            }
                        ],
                    },
                    "prompt_eval_count": 5,
                    "eval_count": 2,
                },
                {
                    "model": "qwen3",
                    "message": {"role": "assistant", "content": "Hazır."},
                },
            ]
        )
        provider = OllamaChatProvider(model="qwen3", client=client)
        first = await provider.respond(
            AgentInput(text="Selamla"),
            context=AgentContext(session_id="session-1"),
            tools=[create_greeting],
            tool_results=[],
            provider_state=None,
        )
        assert first.tool_calls[0].name == "greeting.create"
        assert client.requests[0]["tools"][0]["function"]["name"] == "greeting__create"
        second = await provider.respond(
            AgentInput(text="ignored"),
            context=AgentContext(session_id="session-1"),
            tools=[create_greeting],
            tool_results=[
                ToolResult(
                    call_id=first.tool_calls[0].id,
                    name="greeting.create",
                    output={"message": "Merhaba"},
                )
            ],
            provider_state=first.provider_state,
        )
        assert second.text == "Hazır."
        assert client.requests[1]["messages"][-1]["role"] == "tool"

    asyncio.run(scenario())


def test_text_only_ollama_never_exposes_or_accepts_tool_calls() -> None:
    async def scenario() -> None:
        client = FakeOllamaClient(
            [
                {
                    "model": "text-model",
                    "message": {
                        "role": "assistant",
                        "content": "Only text",
                        "tool_calls": [{"function": {"name": "greeting__create", "arguments": {}}}],
                    },
                }
            ]
        )
        response = await OllamaChatProvider(
            model="text-model",
            client=client,
            tool_strategy=ModelToolStrategy.TEXT_ONLY,
        ).respond(
            AgentInput(text="Try a tool"),
            context=AgentContext(session_id="session-1"),
            tools=[create_greeting],
            tool_results=[],
            provider_state=None,
        )
        assert "tools" not in client.requests[0]
        assert response.tool_calls == []

    asyncio.run(scenario())


class FakeGeminiModels:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []

    async def generate_content(self, **request: Any) -> Any:
        self.requests.append(request)
        return self.response


def test_gemini_disables_sdk_tool_execution_and_returns_proposals() -> None:
    async def scenario() -> None:
        models = FakeGeminiModels(
            SimpleNamespace(
                text=None,
                function_calls=[
                    SimpleNamespace(id="call-1", name="greeting__create", args={"name": "Kaiwen"})
                ],
                usage_metadata=SimpleNamespace(
                    prompt_token_count=7,
                    candidates_token_count=3,
                    total_token_count=10,
                ),
            )
        )
        client = SimpleNamespace(aio=SimpleNamespace(models=models))
        response = await GeminiGenerateContentProvider(model="gemini-test", client=client).respond(
            AgentInput(text="Selamla"),
            context=AgentContext(session_id="session-1"),
            tools=[create_greeting],
            tool_results=[],
            provider_state=None,
        )
        request = models.requests[0]
        assert request["config"]["automatic_function_calling"] == {"disable": True}
        assert request["config"]["tools"][0]["function_declarations"][0]["name"] == (
            "greeting__create"
        )
        assert response.tool_calls[0].name == "greeting.create"
        assert response.usage is not None and response.usage.total_tokens == 10

    asyncio.run(scenario())

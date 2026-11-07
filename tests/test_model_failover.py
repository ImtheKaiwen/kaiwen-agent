from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest

from kaiwen_agent.context import AgentContext
from kaiwen_agent.model_routing import (
    DataLocality,
    ModelComplexity,
    ModelSelection,
    ModelTier,
)
from kaiwen_agent.models import (
    FailoverCandidate,
    FailoverModelProvider,
    FailoverProviderState,
    ModelContinuationError,
    ModelFailoverConfigurationError,
    RetryableModelProviderError,
)
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.types import AgentInput, ModelResponse, ToolCall, ToolResult


class RecordingProvider:
    def __init__(self, outcomes: list[ModelResponse | Exception]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[Sequence[ToolDefinition], Sequence[ToolResult], Any | None]] = []

    async def respond(
        self,
        agent_input: AgentInput,
        *,
        context: AgentContext,
        tools: Sequence[ToolDefinition],
        tool_results: Sequence[ToolResult],
        provider_state: Any | None,
    ) -> ModelResponse:
        del agent_input, context
        self.calls.append((tools, tool_results, provider_state))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _selection(
    route_id: str,
    *,
    locality: DataLocality = DataLocality.CLOUD_ALLOWED,
    regions: frozenset[str] = frozenset(),
) -> ModelSelection:
    return ModelSelection(
        route_id=route_id,
        provider=f"provider-{route_id}",
        model=f"model-{route_id}",
        tier=ModelTier.CHEAP,
        capability="reasoning",
        complexity=ModelComplexity.LOW,
        data_locality=locality,
        regions=regions,
    )


def _provider(*candidates: tuple[ModelSelection, RecordingProvider]) -> FailoverModelProvider:
    return FailoverModelProvider(
        candidates=[
            FailoverCandidate(selection=selection, provider=provider)
            for selection, provider in candidates
        ]
    )


def _respond(
    provider: FailoverModelProvider,
    *,
    tool_results: Sequence[ToolResult] = (),
    provider_state: Any | None = None,
) -> ModelResponse:
    return asyncio.run(
        provider.respond(
            AgentInput(text="hello"),
            context=AgentContext(session_id="session-1"),
            tools=(),
            tool_results=tool_results,
            provider_state=provider_state,
        )
    )


def test_retryable_initial_failure_uses_next_approved_candidate() -> None:
    primary = RecordingProvider([RetryableModelProviderError("temporary")])
    fallback = RecordingProvider([ModelResponse(text="fallback", provider_state={"id": "b"})])

    response = _respond(
        _provider((_selection("primary"), primary), (_selection("fallback"), fallback))
    )

    assert response.text == "fallback"
    assert response.provider_state == FailoverProviderState(route_id="fallback", state={"id": "b"})
    assert len(primary.calls) == len(fallback.calls) == 1


def test_non_retryable_failure_never_falls_back() -> None:
    primary = RecordingProvider([ValueError("bad request")])
    fallback = RecordingProvider([ModelResponse(text="must not run")])
    provider = _provider(
        (_selection("primary"), primary),
        (_selection("fallback"), fallback),
    )

    with pytest.raises(ValueError, match="bad request"):
        _respond(provider)
    assert fallback.calls == []


def test_continuation_is_pinned_and_never_replayed_on_another_provider() -> None:
    primary = RecordingProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="call-1", name="safe.read")],
                provider_state={"opaque": "primary"},
            ),
            RetryableModelProviderError("lost after tool result"),
        ]
    )
    fallback = RecordingProvider([ModelResponse(text="must not run")])
    provider = _provider(
        (_selection("primary"), primary),
        (_selection("fallback"), fallback),
    )
    first = _respond(provider)

    with pytest.raises(RetryableModelProviderError, match="lost after tool result"):
        _respond(
            provider,
            tool_results=[ToolResult(call_id="call-1", name="safe.read", output="ok")],
            provider_state=first.provider_state,
        )

    assert primary.calls[1][2] == {"opaque": "primary"}
    assert fallback.calls == []


def test_untrusted_or_unknown_continuation_state_fails_closed() -> None:
    provider = _provider((_selection("primary"), RecordingProvider([])))
    with pytest.raises(ModelContinuationError, match="requires provider state"):
        _respond(
            provider,
            tool_results=[ToolResult(call_id="call-1", name="safe.read", output="ok")],
            provider_state={"opaque": "not tagged"},
        )
    with pytest.raises(ModelContinuationError, match="unknown failover route"):
        _respond(
            provider,
            provider_state=FailoverProviderState(route_id="forged", state=None),
        )


@pytest.mark.parametrize(
    ("required_locality", "required_region", "candidate"),
    [
        (
            DataLocality.LOCAL_ONLY,
            None,
            _selection("cloud", locality=DataLocality.CLOUD_ALLOWED),
        ),
        (
            DataLocality.REGION_RESTRICTED,
            "eu-central",
            _selection(
                "wrong-region",
                locality=DataLocality.REGION_RESTRICTED,
                regions=frozenset({"us-east"}),
            ),
        ),
    ],
)
def test_incompatible_locality_candidate_is_rejected_at_composition(
    required_locality: DataLocality,
    required_region: str | None,
    candidate: ModelSelection,
) -> None:
    with pytest.raises(ModelFailoverConfigurationError, match="data-locality"):
        FailoverModelProvider(
            candidates=[FailoverCandidate(candidate, RecordingProvider([]))],
            required_locality=required_locality,
            required_region=required_region,
        )


def test_failover_returns_tool_proposals_without_executing_them() -> None:
    fallback = RecordingProvider(
        [ModelResponse(tool_calls=[ToolCall(id="call-1", name="safe.read")])]
    )
    response = _respond(
        _provider(
            (_selection("primary"), RecordingProvider([RetryableModelProviderError()])),
            (_selection("fallback"), fallback),
        )
    )
    assert [call.name for call in response.tool_calls] == ["safe.read"]


def test_catch_all_exception_class_is_rejected() -> None:
    with pytest.raises(ModelFailoverConfigurationError, match="specific Exception"):
        FailoverModelProvider(
            candidates=[FailoverCandidate(_selection("primary"), RecordingProvider([]))],
            retryable_exceptions=(Exception,),
        )

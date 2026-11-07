from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from kaiwen_agent.context import AgentContext
from kaiwen_agent.model_routing.models import ModelSelection
from kaiwen_agent.model_routing.router import locality_satisfies
from kaiwen_agent.models.base import ModelProvider
from kaiwen_agent.models.capabilities import DataLocality
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.types import AgentInput, ModelResponse, ToolResult


class RetryableModelProviderError(RuntimeError):
    """An adapter-classified transient failure that may permit initial-call failover."""


class ModelFailoverConfigurationError(ValueError):
    pass


class ModelContinuationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FailoverCandidate:
    """A policy-approved route paired with its server-side provider adapter."""

    selection: ModelSelection
    provider: ModelProvider


@dataclass(frozen=True, slots=True)
class FailoverProviderState:
    """Pins opaque continuation state to the provider that created it."""

    route_id: str
    state: Any | None


class FailoverModelProvider:
    """Bounded initial-call failover over already policy-approved model routes.

    This adapter does not route, execute tools, or infer retryability. It may move to
    the next candidate only before a provider continuation has started. Once a model
    response is accepted, all opaque state and tool-result continuation remain pinned
    to that exact route.
    """

    def __init__(
        self,
        *,
        candidates: Sequence[FailoverCandidate],
        required_locality: DataLocality = DataLocality.CLOUD_ALLOWED,
        required_region: str | None = None,
        retryable_exceptions: tuple[type[Exception], ...] = (RetryableModelProviderError,),
    ) -> None:
        if not candidates:
            raise ModelFailoverConfigurationError("At least one failover candidate is required")
        route_ids = [candidate.selection.route_id for candidate in candidates]
        if len(route_ids) != len(set(route_ids)):
            raise ModelFailoverConfigurationError("Failover route ids must be unique")
        if not retryable_exceptions:
            raise ModelFailoverConfigurationError("retryable_exceptions must not be empty")
        for error_type in retryable_exceptions:
            if error_type is Exception or not issubclass(error_type, Exception):
                raise ModelFailoverConfigurationError(
                    "retryable_exceptions must contain specific Exception subclasses"
                )
        for candidate in candidates:
            selection = candidate.selection
            if not locality_satisfies(
                selection.data_locality,
                selection.regions,
                required=required_locality,
                region=required_region,
            ):
                raise ModelFailoverConfigurationError(
                    f"Failover route violates data-locality requirement: {selection.route_id}"
                )
        self._candidates = tuple(candidates)
        self._by_route_id = {
            candidate.selection.route_id: candidate for candidate in self._candidates
        }
        self._retryable_exceptions = retryable_exceptions

    async def respond(
        self,
        agent_input: AgentInput,
        *,
        context: AgentContext,
        tools: Sequence[ToolDefinition],
        tool_results: Sequence[ToolResult],
        provider_state: Any | None,
    ) -> ModelResponse:
        if provider_state is not None or tool_results:
            return await self._continue(
                agent_input,
                context=context,
                tools=tools,
                tool_results=tool_results,
                provider_state=provider_state,
            )

        for candidate in self._candidates:
            try:
                response = await candidate.provider.respond(
                    agent_input,
                    context=context,
                    tools=tools,
                    tool_results=(),
                    provider_state=None,
                )
            except self._retryable_exceptions:
                if candidate is self._candidates[-1]:
                    raise
                continue
            return self._pin_response(candidate, response)
        raise AssertionError("Failover candidates unexpectedly exhausted")

    async def _continue(
        self,
        agent_input: AgentInput,
        *,
        context: AgentContext,
        tools: Sequence[ToolDefinition],
        tool_results: Sequence[ToolResult],
        provider_state: Any | None,
    ) -> ModelResponse:
        if not isinstance(provider_state, FailoverProviderState):
            raise ModelContinuationError(
                "Failover continuation requires provider state created by this adapter"
            )
        candidate = self._by_route_id.get(provider_state.route_id)
        if candidate is None:
            raise ModelContinuationError("Continuation references an unknown failover route")
        response = await candidate.provider.respond(
            agent_input,
            context=context,
            tools=tools,
            tool_results=tool_results,
            provider_state=provider_state.state,
        )
        return self._pin_response(candidate, response)

    @staticmethod
    def _pin_response(
        candidate: FailoverCandidate,
        response: ModelResponse,
    ) -> ModelResponse:
        return response.model_copy(
            update={
                "provider_state": FailoverProviderState(
                    route_id=candidate.selection.route_id,
                    state=response.provider_state,
                )
            }
        )

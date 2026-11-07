from collections import deque
from collections.abc import Iterable, Sequence
from typing import Any

from kaiwen_agent.context import AgentContext
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.types import AgentInput, ModelResponse, ToolResult


class FakeModelProvider:
    """Deterministic provider for tests and provider-free examples."""

    def __init__(self, responses: Iterable[ModelResponse]) -> None:
        self._responses = deque(responses)

    async def respond(
        self,
        agent_input: AgentInput,
        *,
        context: AgentContext,
        tools: Sequence[ToolDefinition],
        tool_results: Sequence[ToolResult],
        provider_state: Any | None,
    ) -> ModelResponse:
        del agent_input, context, tools, tool_results, provider_state
        if not self._responses:
            raise RuntimeError("FakeModelProvider has no queued response")
        return self._responses.popleft()

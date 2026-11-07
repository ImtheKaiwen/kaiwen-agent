from collections.abc import Sequence
from typing import Any, Protocol

from kaiwen_agent.context import AgentContext
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.types import AgentInput, ModelResponse, ToolResult


class ModelProvider(Protocol):
    async def respond(
        self,
        agent_input: AgentInput,
        *,
        context: AgentContext,
        tools: Sequence[ToolDefinition],
        tool_results: Sequence[ToolResult],
        provider_state: Any | None,
    ) -> ModelResponse: ...

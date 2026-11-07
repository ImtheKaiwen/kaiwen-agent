from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from kaiwen_agent.context import AgentContext
from kaiwen_agent.models.openai_tools import (
    openai_tool_name,
    serialize_openai_tool,
    strict_openai_schema,
)
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.types import (
    AgentInput,
    ModelResponse,
    ModelUsage,
    ToolCall,
    ToolResult,
)


@dataclass(frozen=True, slots=True)
class OpenAIProviderState:
    previous_response_id: str | None = None
    input_items: tuple[dict[str, Any], ...] = ()


class OpenAIResponsesProvider:
    """OpenAI Responses API adapter with no shared conversation state."""

    def __init__(
        self,
        *,
        model: str,
        instructions: str | None = None,
        client: Any | None = None,
        store: bool = False,
        parallel_tool_calls: bool = True,
        output_schema: Mapping[str, Any] | None = None,
        output_name: str = "agent_output",
    ) -> None:
        if client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as error:
                raise RuntimeError(
                    "Install the OpenAI adapter with `pip install kaiwen-agent[openai]`."
                ) from error
            client = AsyncOpenAI()

        self._client = client
        self.model = model
        self.instructions = instructions
        self.store = store
        self.parallel_tool_calls = parallel_tool_calls
        self.output_schema = dict(output_schema) if output_schema is not None else None
        self.output_name = output_name

    async def respond(
        self,
        agent_input: AgentInput,
        *,
        context: AgentContext,
        tools: Sequence[ToolDefinition],
        tool_results: Sequence[ToolResult],
        provider_state: Any | None,
    ) -> ModelResponse:
        provider_names = {
            openai_tool_name(definition.name): definition.name for definition in tools
        }
        if len(provider_names) != len(tools):
            raise ValueError("Tool names collide after OpenAI-compatible normalization")

        state = provider_state if isinstance(provider_state, OpenAIProviderState) else None
        request_input = self._build_input(agent_input, tool_results, state)
        request: dict[str, Any] = {
            "model": self.model,
            "input": request_input,
            "tools": [serialize_openai_tool(definition) for definition in tools],
            "store": self.store,
            "parallel_tool_calls": self.parallel_tool_calls,
        }
        if self.instructions:
            request["instructions"] = self.instructions
        if self.store and state and state.previous_response_id:
            request["previous_response_id"] = state.previous_response_id
        if not self.store:
            request["include"] = ["reasoning.encrypted_content"]
        if self.output_schema is not None:
            request["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": self.output_name,
                    "schema": strict_openai_schema(self.output_schema),
                    "strict": True,
                }
            }
        safety_identifier = context.metadata.get("safety_identifier")
        if isinstance(safety_identifier, str) and safety_identifier:
            request["safety_identifier"] = safety_identifier

        response = await self._client.responses.create(**request)
        output_text = getattr(response, "output_text", None)
        structured_output: Any | None = None
        if self.output_schema is not None and output_text:
            structured_output = json.loads(output_text)

        calls: list[ToolCall] = []
        for item in getattr(response, "output", []):
            if getattr(item, "type", None) != "function_call":
                continue
            calls.append(
                ToolCall(
                    id=item.call_id,
                    name=provider_names.get(item.name, item.name),
                    arguments=json.loads(getattr(item, "arguments", "{}")),
                )
            )

        return ModelResponse(
            text=output_text,
            tool_calls=calls,
            provider_response_id=getattr(response, "id", None),
            provider_state=self._next_state(response, request_input),
            structured_output=structured_output,
            usage=self._parse_usage(getattr(response, "usage", None)),
            model=getattr(response, "model", self.model),
        )

    def _build_input(
        self,
        agent_input: AgentInput,
        tool_results: Sequence[ToolResult],
        state: OpenAIProviderState | None,
    ) -> str | list[dict[str, Any]]:
        tool_outputs = [
            {
                "type": "function_call_output",
                "call_id": result.call_id,
                "output": json.dumps(result.output, ensure_ascii=False, default=str),
            }
            for result in tool_results
        ]
        if self.store and state and state.previous_response_id:
            return tool_outputs
        if not self.store and state:
            return [*deepcopy(list(state.input_items)), *tool_outputs]
        return agent_input.text

    def _next_state(
        self,
        response: Any,
        request_input: str | list[dict[str, Any]],
    ) -> OpenAIProviderState:
        response_id = getattr(response, "id", None)
        if self.store:
            return OpenAIProviderState(previous_response_id=response_id)

        if isinstance(request_input, str):
            input_items: list[dict[str, Any]] = [
                {
                    "role": "user",
                    "content": request_input,
                }
            ]
        else:
            input_items = deepcopy(request_input)
        input_items.extend(self._serialize_output_item(item) for item in response.output)
        return OpenAIProviderState(input_items=tuple(input_items))

    @staticmethod
    def _serialize_output_item(item: Any) -> dict[str, Any]:
        model_dump = getattr(item, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump(mode="json", exclude_none=True)
            if isinstance(dumped, dict):
                return dumped
        if isinstance(item, Mapping):
            return deepcopy(dict(item))

        fields = (
            "type",
            "id",
            "call_id",
            "name",
            "arguments",
            "status",
            "role",
            "content",
            "summary",
            "encrypted_content",
            "phase",
        )
        return {
            field: deepcopy(getattr(item, field))
            for field in fields
            if getattr(item, field, None) is not None
        }

    @staticmethod
    def _parse_usage(raw_usage: Any | None) -> ModelUsage | None:
        if raw_usage is None:
            return None
        return ModelUsage(
            input_tokens=getattr(raw_usage, "input_tokens", 0),
            output_tokens=getattr(raw_usage, "output_tokens", 0),
            total_tokens=getattr(raw_usage, "total_tokens", 0),
        )

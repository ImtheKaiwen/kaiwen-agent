from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from kaiwen_agent.context import AgentContext
from kaiwen_agent.models.capabilities import ModelToolStrategy
from kaiwen_agent.models.provider_tools import provider_tool_catalog
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.types import AgentInput, ModelResponse, ModelUsage, ToolCall, ToolResult


@dataclass(frozen=True, slots=True)
class OllamaProviderState:
    messages: tuple[dict[str, Any], ...] = ()


class OllamaChatProvider:
    """Ollama chat adapter; it proposes tools but never executes them."""

    def __init__(
        self,
        *,
        model: str,
        instructions: str | None = None,
        client: Any | None = None,
        host: str | None = None,
        tool_strategy: ModelToolStrategy = ModelToolStrategy.NATIVE,
        think: bool | str | None = None,
    ) -> None:
        if tool_strategy not in {ModelToolStrategy.NATIVE, ModelToolStrategy.TEXT_ONLY}:
            raise ValueError("Ollama adapter currently supports native or text-only tools")
        if client is None:
            try:
                from ollama import AsyncClient  # type: ignore[import-not-found]
            except ImportError as error:
                raise RuntimeError(
                    "Install the Ollama adapter with `pip install kaiwen-agent[ollama]`."
                ) from error
            client = AsyncClient(host=host)
        self._client = client
        self.model = model
        self.instructions = instructions
        self.tool_strategy = tool_strategy
        self.think = think

    async def respond(
        self,
        agent_input: AgentInput,
        *,
        context: AgentContext,
        tools: Sequence[ToolDefinition],
        tool_results: Sequence[ToolResult],
        provider_state: Any | None,
    ) -> ModelResponse:
        del context
        state = provider_state if isinstance(provider_state, OllamaProviderState) else None
        messages = deepcopy(list(state.messages)) if state else []
        if not messages:
            if self.instructions:
                messages.append({"role": "system", "content": self.instructions})
            messages.append({"role": "user", "content": agent_input.text})
        for result in tool_results:
            messages.append(
                {
                    "role": "tool",
                    "tool_name": _provider_name(result.name),
                    "content": json.dumps(result.output, ensure_ascii=False, default=str),
                }
            )
        request: dict[str, Any] = {
            "model": self.model,
            "messages": deepcopy(messages),
            "stream": False,
        }
        provider_names: dict[str, str] = {}
        if self.tool_strategy == ModelToolStrategy.NATIVE and tools:
            catalog, provider_names = provider_tool_catalog(tools)
            request["tools"] = catalog
        if self.think is not None:
            request["think"] = self.think
        response = await self._client.chat(**request)
        raw = _mapping(response)
        message = _mapping(raw.get("message", {}))
        calls: list[ToolCall] = []
        serialized_calls: list[dict[str, Any]] = []
        raw_calls = (
            message.get("tool_calls") or []
            if self.tool_strategy == ModelToolStrategy.NATIVE
            else []
        )
        for index, item in enumerate(raw_calls):
            function = _mapping(_mapping(item).get("function", {}))
            name = str(function.get("name", ""))
            arguments = _mapping(function.get("arguments", {}))
            call_id = str(_mapping(item).get("id") or _call_id(name, arguments, index))
            calls.append(
                ToolCall(
                    id=call_id,
                    name=provider_names.get(name, name),
                    arguments=dict(arguments),
                )
            )
            serialized_calls.append(
                {"type": "function", "function": {"name": name, "arguments": arguments}}
            )
        assistant: dict[str, Any] = {
            "role": "assistant",
            "content": str(message.get("content") or ""),
        }
        if serialized_calls:
            assistant["tool_calls"] = serialized_calls
        messages.append(assistant)
        input_tokens = _nonnegative_int(raw.get("prompt_eval_count"))
        output_tokens = _nonnegative_int(raw.get("eval_count"))
        return ModelResponse(
            text=assistant["content"] or None,
            tool_calls=calls,
            provider_state=OllamaProviderState(messages=tuple(messages)),
            usage=ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
            model=str(raw.get("model") or self.model),
        )


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json", exclude_none=True)
        if isinstance(dumped, Mapping):
            return dumped
    return {}


def _provider_name(name: str) -> str:
    return name.replace(".", "__")


def _call_id(name: str, arguments: Mapping[str, Any], index: int) -> str:
    material = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return f"ollama_{sha256(f'{name}:{index}:{material}'.encode()).hexdigest()[:24]}"


def _nonnegative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

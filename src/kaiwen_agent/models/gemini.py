from __future__ import annotations

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
class GeminiProviderState:
    contents: tuple[dict[str, Any], ...] = ()


class GeminiGenerateContentProvider:
    """Gemini adapter with automatic SDK function execution explicitly disabled."""

    def __init__(
        self,
        *,
        model: str,
        instructions: str | None = None,
        client: Any | None = None,
        tool_strategy: ModelToolStrategy = ModelToolStrategy.NATIVE,
    ) -> None:
        if tool_strategy not in {ModelToolStrategy.NATIVE, ModelToolStrategy.TEXT_ONLY}:
            raise ValueError("Gemini adapter currently supports native or text-only tools")
        if client is None:
            try:
                from google import genai  # type: ignore[import-not-found]
            except ImportError as error:
                raise RuntimeError(
                    "Install the Gemini adapter with `pip install kaiwen-agent[gemini]`."
                ) from error
            client = genai.Client()
        self._client = client
        self.model = model
        self.instructions = instructions
        self.tool_strategy = tool_strategy

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
        state = provider_state if isinstance(provider_state, GeminiProviderState) else None
        contents = deepcopy(list(state.contents)) if state else []
        if not contents:
            contents.append({"role": "user", "parts": [{"text": agent_input.text}]})
        for result in tool_results:
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "function_response": {
                                "name": _provider_name(result.name),
                                "response": {"result": result.output},
                                "id": result.call_id,
                            }
                        }
                    ],
                }
            )
        config: dict[str, Any] = {
            "automatic_function_calling": {"disable": True},
        }
        if self.instructions:
            config["system_instruction"] = self.instructions
        provider_names: dict[str, str] = {}
        if self.tool_strategy == ModelToolStrategy.NATIVE and tools:
            catalog, provider_names = provider_tool_catalog(tools)
            config["tools"] = [{"function_declarations": [item["function"] for item in catalog]}]
        response = await self._client.aio.models.generate_content(
            model=self.model,
            contents=contents,
            config=config,
        )
        calls: list[ToolCall] = []
        model_parts: list[dict[str, Any]] = []
        raw_calls = (
            getattr(response, "function_calls", None) or []
            if self.tool_strategy == ModelToolStrategy.NATIVE
            else []
        )
        for index, raw_call in enumerate(raw_calls):
            name = str(getattr(raw_call, "name", ""))
            arguments = _mapping(getattr(raw_call, "args", {}))
            call_id = str(getattr(raw_call, "id", None) or _call_id(name, arguments, index))
            calls.append(
                ToolCall(
                    id=call_id,
                    name=provider_names.get(name, name),
                    arguments=dict(arguments),
                )
            )
            model_parts.append(
                {
                    "function_call": {
                        "name": name,
                        "args": dict(arguments),
                        "id": call_id,
                    }
                }
            )
        text = getattr(response, "text", None)
        if isinstance(text, str) and text:
            model_parts.insert(0, {"text": text})
        if model_parts:
            contents.append({"role": "model", "parts": model_parts})
        usage = getattr(response, "usage_metadata", None)
        input_tokens = _usage_value(usage, "prompt_token_count")
        output_tokens = _usage_value(usage, "candidates_token_count")
        total_tokens = _usage_value(usage, "total_token_count") or input_tokens + output_tokens
        return ModelResponse(
            text=text if isinstance(text, str) and text else None,
            tool_calls=calls,
            provider_state=GeminiProviderState(contents=tuple(contents)),
            usage=ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
            ),
            model=self.model,
        )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _provider_name(name: str) -> str:
    return name.replace(".", "__")


def _call_id(name: str, arguments: Mapping[str, Any], index: int) -> str:
    material = repr(sorted(arguments.items()))
    return f"gemini_{sha256(f'{name}:{index}:{material}'.encode()).hexdigest()[:24]}"


def _usage_value(usage: Any, field: str) -> int:
    value = getattr(usage, field, 0) if usage is not None else 0
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

"""Provider adapter contracts against recorded OpenAI SDK shapes and real SDK errors (M5).

The responses below follow the shapes returned by the installed `openai` SDK (they are
validated into the real `Response` model, not hand-built stand-ins), so adapter parsing
is checked against the actual attribute surface instead of a permissive fake.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

import httpx
import openai
import pytest
from openai.types.responses import Response
from pydantic import BaseModel

from kaiwen_agent.context import AgentContext
from kaiwen_agent.exceptions import ModelResponseError
from kaiwen_agent.model_routing import (
    DataLocality,
    ModelComplexity,
    ModelSelection,
    ModelTier,
)
from kaiwen_agent.models import (
    FailoverCandidate,
    FailoverModelProvider,
    is_transient_provider_error,
)
from kaiwen_agent.models.openai import OpenAIResponsesProvider
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.types import AgentInput, ModelResponse, ToolResult

_USAGE = {
    "input_tokens": 10,
    "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
    "output_tokens": 5,
    "output_tokens_details": {"reasoning_tokens": 0},
    "total_tokens": 15,
}
_MESSAGE = {
    "type": "message",
    "id": "msg_1",
    "status": "completed",
    "role": "assistant",
    "content": [{"type": "output_text", "text": "Hello there", "annotations": []}],
}
_FUNCTION_CALL = {
    "type": "function_call",
    "id": "fc_1",
    "call_id": "call_1",
    "name": "projects__read",
    "arguments": '{"category": "games"}',
    "status": "completed",
}


def _response(*output: dict[str, Any]) -> Response:
    return Response.model_validate(
        {
            "id": "resp_1",
            "object": "response",
            "created_at": 1.0,
            "model": "gpt-test",
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "status": "completed",
            "usage": _USAGE,
            "output": list(output),
        }
    )


class _Query(BaseModel):
    category: str | None = None


async def _read(context: AgentContext, arguments: BaseModel) -> dict[str, Any]:
    return {}


_TOOL = ToolDefinition(
    name="projects.read",
    description="List projects",
    input_model=_Query,
    handler=_read,
    permissions=frozenset(),
)


class _Client:
    def __init__(self, *outcomes: Response | Exception) -> None:
        self.requests: list[dict[str, Any]] = []
        self._outcomes = list(outcomes)
        self.responses = self

    async def create(self, **request: Any) -> Response:
        self.requests.append(request)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _respond(
    provider: Any,
    *,
    tool_results: Sequence[ToolResult] = (),
    provider_state: Any = None,
    tools: Sequence[ToolDefinition] = (_TOOL,),
) -> ModelResponse:
    return asyncio.run(
        provider.respond(
            AgentInput(text="Show the games"),
            context=AgentContext(session_id="s"),
            tools=tools,
            tool_results=tool_results,
            provider_state=provider_state,
        )
    )


def test_recorded_text_response_is_parsed_with_usage() -> None:
    provider = OpenAIResponsesProvider(model="gpt-test", client=_Client(_response(_MESSAGE)))

    result = _respond(provider)

    assert result.text == "Hello there"
    assert result.tool_calls == []
    assert result.model == "gpt-test"
    assert result.usage is not None and result.usage.total_tokens == 15


def test_recorded_function_call_maps_to_the_registered_tool_name() -> None:
    client = _Client(_response(_FUNCTION_CALL))
    provider = OpenAIResponsesProvider(model="gpt-test", client=client)

    result = _respond(provider)

    call = result.tool_calls[0]
    assert (call.id, call.name, call.arguments) == (
        "call_1",
        "projects.read",
        {"category": "games"},
    )
    assert client.requests[0]["tools"][0]["name"] == "projects__read"
    assert client.requests[0]["store"] is False


def test_stateless_continuation_replays_provider_items_and_tool_output() -> None:
    client = _Client(_response(_FUNCTION_CALL), _response(_MESSAGE))
    provider = OpenAIResponsesProvider(model="gpt-test", client=client)
    first = _respond(provider)

    _respond(
        provider,
        tool_results=[ToolResult(call_id="call_1", name="projects.read", output={"items": []})],
        provider_state=first.provider_state,
    )

    items = client.requests[1]["input"]
    assert items[0] == {"role": "user", "content": "Show the games"}
    assert items[1]["type"] == "function_call" and items[1]["call_id"] == "call_1"
    assert items[2]["type"] == "function_call_output"
    assert json.loads(items[2]["output"]) == {"items": []}
    assert "reasoning.encrypted_content" in client.requests[1]["include"]


@pytest.mark.parametrize(
    "arguments",
    [
        "{not json sk-secretvalue1234567890",
        '["sk-secretvalue1234567890"]',
        '"sk-secretvalue1234567890"',
    ],
)
def test_malformed_tool_arguments_raise_a_typed_error_without_echoing_them(
    arguments: str,
) -> None:
    malformed = {**_FUNCTION_CALL, "arguments": arguments}
    provider = OpenAIResponsesProvider(model="gpt-test", client=_Client(_response(malformed)))

    with pytest.raises(ModelResponseError) as error:
        _respond(provider)

    assert "secretvalue" not in str(error.value)


def test_timeout_and_retry_configuration_reach_the_sdk_client(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    provider = OpenAIResponsesProvider(model="gpt-test", timeout=7.5, max_retries=0)

    assert provider._client.max_retries == 0
    assert provider._client.timeout == 7.5

    default = OpenAIResponsesProvider(model="gpt-test")
    assert default._client.max_retries == 2


def test_timeout_options_are_rejected_with_an_injected_client() -> None:
    with pytest.raises(ValueError):
        OpenAIResponsesProvider(model="gpt-test", client=_Client(), timeout=5)
    with pytest.raises(ValueError):
        OpenAIResponsesProvider(model="gpt-test", client=_Client(), max_retries=1)


_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/responses")


def _status_error(cls: type[openai.APIStatusError], status: int, body: Any = None) -> Exception:
    return cls("failure", response=httpx.Response(status, request=_REQUEST), body=body)


@pytest.mark.parametrize(
    ("error", "transient"),
    [
        (openai.APIConnectionError(request=_REQUEST), True),
        (openai.APITimeoutError(request=_REQUEST), True),
        (_status_error(openai.InternalServerError, 500), True),
        (_status_error(openai.InternalServerError, 503), True),
        (_status_error(openai.RateLimitError, 429, {"code": "rate_limit_exceeded"}), True),
        (_status_error(openai.RateLimitError, 429, {"code": "insufficient_quota"}), False),
        (_status_error(openai.BadRequestError, 400), False),
        (_status_error(openai.AuthenticationError, 401), False),
        (_status_error(openai.PermissionDeniedError, 403), False),
        (ValueError("bad input"), False),
        (TimeoutError(), True),
        (ConnectionResetError(), True),
    ],
)
def test_transient_error_classification_uses_real_sdk_exceptions(
    error: Exception, transient: bool
) -> None:
    assert is_transient_provider_error(error) is transient


class _GenericStatusError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__("status")
        self.status_code = status


class ConnectError(Exception):  # name-compatible stand-in for the httpx and ollama errors
    pass


class _GenaiStyleError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__("genai")
        self.code = code


def test_classification_covers_gemini_and_ollama_style_errors() -> None:
    assert is_transient_provider_error(_GenericStatusError(503))
    assert is_transient_provider_error(_GenaiStyleError(500))
    assert is_transient_provider_error(ConnectError())
    assert not is_transient_provider_error(_GenericStatusError(404))
    assert not is_transient_provider_error(_GenaiStyleError(400))


def _selection(
    route_id: str, locality: DataLocality = DataLocality.CLOUD_ALLOWED
) -> ModelSelection:
    return ModelSelection(
        route_id=route_id,
        provider=f"provider-{route_id}",
        model=f"model-{route_id}",
        tier=ModelTier.CHEAP,
        capability="reasoning",
        complexity=ModelComplexity.LOW,
        data_locality=locality,
        regions=frozenset(),
    )


def _failover(first: Exception, *, second_locality: DataLocality = DataLocality.CLOUD_ALLOWED):
    primary = OpenAIResponsesProvider(model="m1", client=_Client(first))
    secondary_client = _Client(_response(_MESSAGE))
    secondary = OpenAIResponsesProvider(model="m2", client=secondary_client)
    provider = FailoverModelProvider(
        candidates=[
            FailoverCandidate(selection=_selection("primary"), provider=primary),
            FailoverCandidate(
                selection=_selection("secondary", second_locality), provider=secondary
            ),
        ]
    )
    return provider, secondary_client


def test_failover_uses_the_next_route_for_a_genuinely_transient_sdk_error() -> None:
    provider, secondary_client = _failover(openai.APIConnectionError(request=_REQUEST))

    result = _respond(provider)

    assert result.text == "Hello there"
    assert len(secondary_client.requests) == 1


@pytest.mark.parametrize(
    "error",
    [
        _status_error(openai.BadRequestError, 400),
        _status_error(openai.AuthenticationError, 401),
        _status_error(openai.RateLimitError, 429, {"code": "insufficient_quota"}),
    ],
)
def test_failover_does_not_hide_non_transient_errors(error: Exception) -> None:
    provider, secondary_client = _failover(error)

    with pytest.raises(type(error)):
        _respond(provider)

    assert secondary_client.requests == []


def test_failover_still_enforces_data_locality_at_configuration() -> None:
    from kaiwen_agent.models import ModelFailoverConfigurationError

    with pytest.raises(ModelFailoverConfigurationError):
        FailoverModelProvider(
            candidates=[
                FailoverCandidate(
                    selection=_selection("cloud"),
                    provider=OpenAIResponsesProvider(model="m", client=_Client()),
                )
            ],
            required_locality=DataLocality.LOCAL_ONLY,
        )

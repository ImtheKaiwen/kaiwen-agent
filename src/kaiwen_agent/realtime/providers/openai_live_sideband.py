from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from pydantic import TypeAdapter

from kaiwen_agent.context import AgentContext
from kaiwen_agent.events import EventType
from kaiwen_agent.exceptions import (
    ApprovalDeniedError,
    ApprovalRequiredError,
    PermissionDeniedError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolTimeoutError,
    ToolValidationError,
)
from kaiwen_agent.models.openai_tools import openai_tool_name
from kaiwen_agent.realtime.events import RealtimeProviderEvent
from kaiwen_agent.realtime.providers.openai_live import (
    OpenAILiveProtocolError,
    OpenAILiveSocket,
    OpenAILiveSocketConnector,
    WebsocketsOpenAILiveConnector,
    _decode_event,
    _event_id,
    normalize_openai_live_event,
)
from kaiwen_agent.realtime.session import (
    RealtimeSessionRecord,
    RealtimeSessionStatus,
)
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.tools.execution import ToolExecutor
from kaiwen_agent.tools.registry import ToolRegistry
from kaiwen_agent.types import ToolCall

ProviderEventHandler = Callable[[RealtimeProviderEvent], Awaitable[None]]
ToolEventHandler = Callable[[EventType, dict[str, object]], Awaitable[None]]


async def _ignore_provider_event(event: RealtimeProviderEvent) -> None:
    del event


async def _ignore_tool_event(event_type: EventType, payload: dict[str, object]) -> None:
    del event_type, payload


class OpenAILiveSidebandConnection:
    """Trusted server connection attached to one already-running Live session."""

    def __init__(
        self,
        socket: OpenAILiveSocket,
        *,
        provider_session_id: str,
        realtime_session_id: str,
        conversation_id: str,
        tenant_id: str,
        user_id: str,
    ) -> None:
        self._socket = socket
        self.provider_session_id = provider_session_id
        self.realtime_session_id = realtime_session_id
        self.conversation_id = conversation_id
        self.tenant_id = tenant_id
        self.user_id = user_id
        self._closed = False

    async def raw_events(self) -> AsyncIterator[dict[str, Any]]:
        while not self._closed:
            raw = await self._socket.recv()
            event = _decode_event(raw)
            yield event
            if event.get("type") == "session.closed":
                self._closed = True
                return

    async def send_function_result(self, call_id: str, output: Any) -> None:
        if not call_id:
            raise ValueError("call_id must not be empty")
        await self._send(
            {
                "type": "response.item.create",
                "event_id": _event_id(),
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": _json_output(output),
                },
            }
        )

    async def continue_response(self) -> None:
        await self._send({"type": "response.create", "event_id": _event_id()})

    async def append_thinking(self, content: str, *, delegation_id: str | None) -> None:
        await self._append_context("session.thinking.append", content, delegation_id)

    async def append_commentary(self, content: str, *, delegation_id: str | None) -> None:
        await self._append_context("session.commentary.append", content, delegation_id)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Closing a sideband must not close the primary WebRTC/SIP media session.
        await self._socket.close()

    async def _append_context(
        self,
        event_type: str,
        content: str,
        delegation_id: str | None,
    ) -> None:
        if not content.strip():
            raise ValueError("content must not be empty")
        await self._send(
            {
                "type": event_type,
                "event_id": _event_id(),
                "delegation_id": delegation_id,
                "content": content,
            }
        )

    async def _send(self, event: dict[str, Any]) -> None:
        if self._closed:
            raise RuntimeError("OpenAI Live sideband connection is closed")
        await self._socket.send(json.dumps(event, separators=(",", ":")))


class OpenAILiveSidebandService:
    """Attaches trusted backend control to an application-owned realtime session."""

    def __init__(
        self,
        *,
        api_key: str,
        connector: OpenAILiveSocketConnector | None = None,
        base_url: str = "wss://api.openai.com/v1/live/sessions",
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._api_key = api_key
        self._connector = connector or WebsocketsOpenAILiveConnector()
        self._base_url = base_url.rstrip("/")

    async def attach(
        self,
        session: RealtimeSessionRecord,
    ) -> OpenAILiveSidebandConnection:
        if session.provider != "openai-live":
            raise ValueError("session provider must be openai-live")
        if session.status not in {
            RealtimeSessionStatus.CONNECTING,
            RealtimeSessionStatus.ACTIVE,
        }:
            raise ValueError("session must be connecting or active")
        provider_session_id = session.provider_session_id
        if not provider_session_id:
            raise ValueError("session has no provider session id")
        encoded_session_id = quote(provider_session_id, safe="")
        socket = await self._connector.connect(
            f"{self._base_url}/{encoded_session_id}/attach",
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        return OpenAILiveSidebandConnection(
            socket,
            provider_session_id=provider_session_id,
            realtime_session_id=session.id,
            conversation_id=session.conversation_id,
            tenant_id=session.tenant_id,
            user_id=session.user_id,
        )


@dataclass(slots=True)
class _PendingResponse:
    calls: list[ToolCall] = field(default_factory=list)


class OpenAILiveSidebandToolBridge:
    """Executes delegated Live function calls through the normal safe tool pipeline."""

    def __init__(
        self,
        connection: OpenAILiveSidebandConnection,
        *,
        tools: Sequence[ToolDefinition],
        tool_executor: ToolExecutor,
        context: AgentContext,
        execution_scope: str | None = None,
        on_provider_event: ProviderEventHandler = _ignore_provider_event,
        emit: ToolEventHandler = _ignore_tool_event,
    ) -> None:
        if not tools:
            raise ValueError("at least one tool is required")
        if context.session_id != connection.realtime_session_id:
            raise ValueError("context session does not own the sideband connection")
        if context.tenant_id != connection.tenant_id:
            raise ValueError("context tenant does not own the sideband connection")
        if context.user_id != connection.user_id:
            raise ValueError("context user does not own the sideband connection")
        self.connection = connection
        self.registry = ToolRegistry(tools)
        self.tool_executor = tool_executor
        self.context = context
        self.execution_scope = execution_scope or f"realtime:{context.session_id}"
        self.on_provider_event = on_provider_event
        self.emit = emit
        self._provider_names = {
            openai_tool_name(definition.name): definition.name for definition in tools
        }
        if len(self._provider_names) != len(tools):
            raise ValueError("Tool names collide after OpenAI-compatible normalization")
        self._response_ids: dict[str, str] = {}
        self._pending: dict[tuple[str, str], _PendingResponse] = {}
        self._completed: set[tuple[str, str]] = set()

    async def run(self) -> None:
        async for event in self.connection.raw_events():
            normalized = normalize_openai_live_event(event)
            if normalized is not None:
                await self.on_provider_event(normalized)
            await self._handle_response_event(event)

    async def _handle_response_event(self, envelope: Mapping[str, Any]) -> None:
        if envelope.get("type") != "response.event":
            return
        delegation_id = envelope.get("delegation_id")
        nested = envelope.get("event")
        if not isinstance(delegation_id, str) or not isinstance(nested, Mapping):
            return
        nested_type = nested.get("type")
        if nested_type == "response.created":
            response_id = _response_id(nested)
            if response_id:
                self._response_ids[delegation_id] = response_id
            return
        response_id = _response_id(nested) or self._response_ids.get(delegation_id)
        if not response_id:
            return
        key = (delegation_id, response_id)
        if nested_type == "response.output_item.done":
            call = self._parse_call(nested)
            if call is not None:
                self._pending.setdefault(key, _PendingResponse()).calls.append(call)
            return
        if nested_type == "response.completed":
            if key in self._completed:
                return
            self._completed.add(key)
            pending = self._pending.pop(key, None)
            if pending is not None and pending.calls:
                await self._execute_calls(pending.calls, delegation_id)

    def _parse_call(self, event: Mapping[str, Any]) -> ToolCall | None:
        item = event.get("item")
        if not isinstance(item, Mapping) or item.get("type") != "function_call":
            return None
        call_id = item.get("call_id")
        provider_name = item.get("name")
        raw_arguments = item.get("arguments", "{}")
        if not isinstance(call_id, str) or not isinstance(provider_name, str):
            raise OpenAILiveProtocolError("Completed function call is missing call_id or name")
        try:
            arguments = (
                json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            )
        except json.JSONDecodeError as exc:
            raise OpenAILiveProtocolError("Completed function call has invalid arguments") from exc
        if not isinstance(arguments, dict):
            raise OpenAILiveProtocolError("Completed function call arguments must be an object")
        return ToolCall(
            id=call_id,
            name=self._provider_names.get(provider_name, provider_name),
            arguments=arguments,
        )

    async def _execute_calls(self, calls: Sequence[ToolCall], delegation_id: str) -> None:
        for call in calls:
            try:
                definition = self.registry.get(call.name)
                result = await self.tool_executor.execute(
                    definition,
                    call,
                    self.context,
                    execution_scope=f"{self.execution_scope}:{delegation_id}",
                    emit=self.emit,
                )
                output = result.output
            except Exception as exc:
                output = {"ok": False, "error": {"code": _safe_error_code(exc)}}
            await self.connection.send_function_result(call.id, output)
        await self.connection.continue_response()


def _response_id(event: Mapping[str, Any]) -> str | None:
    direct = event.get("response_id")
    if isinstance(direct, str):
        return direct
    response = event.get("response")
    if isinstance(response, Mapping) and isinstance(response.get("id"), str):
        response_id = response["id"]
        return str(response_id)
    return None


def _safe_error_code(error: Exception) -> str:
    if isinstance(error, ToolNotFoundError):
        return "tool_not_found"
    if isinstance(error, PermissionDeniedError):
        return "permission_denied"
    if isinstance(error, ApprovalRequiredError):
        return "approval_required"
    if isinstance(error, ApprovalDeniedError):
        return "approval_denied"
    if isinstance(error, ToolValidationError):
        return "invalid_arguments"
    if isinstance(error, ToolTimeoutError):
        return "tool_timeout"
    if isinstance(error, ToolExecutionError):
        return "tool_failed"
    return "tool_failed"


def _json_output(output: Any) -> str:
    return TypeAdapter(Any).dump_json(output).decode("utf-8")

from __future__ import annotations

import asyncio
import base64
import importlib
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.models.openai_tools import serialize_openai_tool
from kaiwen_agent.realtime.base import RealtimeConnection
from kaiwen_agent.realtime.config import RealtimeSessionConfig
from kaiwen_agent.realtime.events import (
    RealtimeEventDelivery,
    RealtimeEventType,
    RealtimeProviderEvent,
)
from kaiwen_agent.tools.definition import ToolDefinition


class OpenAILiveError(RuntimeError):
    pass


class OpenAILiveProtocolError(OpenAILiveError):
    pass


class OpenAILiveTransportUnavailableError(OpenAILiveError):
    pass


class OpenAILiveSocket(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


class OpenAILiveSocketConnector(Protocol):
    async def connect(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
    ) -> OpenAILiveSocket: ...


class WebsocketsOpenAILiveConnector:
    """Optional default transport loaded only when an actual connection is opened."""

    async def connect(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
    ) -> OpenAILiveSocket:
        try:
            module = importlib.import_module("websockets.asyncio.client")
        except ImportError as exc:
            raise OpenAILiveTransportUnavailableError(
                "Install kaiwen-agent[openai-live] to use the OpenAI Live WebSocket transport"
            ) from exc
        connect = cast(Any, module.connect)
        socket = await connect(url, additional_headers=dict(headers))
        return cast(OpenAILiveSocket, socket)


class OpenAILiveProviderOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instructions: str | None = None
    voice: str | None = None
    store: bool = False
    response_model: str | None = None
    response_instructions: str | None = None
    parallel_tool_calls: bool = False
    connect_timeout_seconds: float = Field(default=15, gt=0, le=120)


class OpenAILiveWebRTCSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    sdp_answer: str


class OpenAILiveHTTPTransport(Protocol):
    async def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]: ...


class UrllibOpenAILiveHTTPTransport:
    async def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._post_json,
            url,
            dict(headers),
            body,
            timeout_seconds,
        )

    @staticmethod
    def _post_json(
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        request = Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
                decoded = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise OpenAILiveError(f"OpenAI Live request failed with HTTP {exc.code}") from exc
        except URLError as exc:
            raise OpenAILiveError("OpenAI Live request could not connect") from exc
        if not isinstance(decoded, dict):
            raise OpenAILiveProtocolError("OpenAI Live returned a non-object response")
        return cast(dict[str, Any], decoded)


class OpenAILiveWebRTCService:
    """Creates browser WebRTC sessions without exposing the server API key."""

    def __init__(
        self,
        *,
        api_key: str,
        transport: OpenAILiveHTTPTransport | None = None,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 15,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._api_key = api_key
        self._transport = transport or UrllibOpenAILiveHTTPTransport()
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def create_session(
        self,
        *,
        sdp_offer: str,
        config: RealtimeSessionConfig,
        options: OpenAILiveProviderOptions | None = None,
        tools: Sequence[ToolDefinition] = (),
    ) -> OpenAILiveWebRTCSession:
        if not sdp_offer.strip():
            raise ValueError("sdp_offer must not be empty")
        resolved_options = options or OpenAILiveProviderOptions()
        payload = {
            "session": _build_session_config(
                config,
                resolved_options,
                include_audio_format=False,
                tools=tools,
            ),
            "transport": {"type": "webrtc", "sdp": sdp_offer},
        }
        response = await self._transport.post_json(
            f"{self._base_url}/live/sessions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            body=payload,
            timeout_seconds=self._timeout_seconds,
        )
        session = response.get("session")
        transport = response.get("transport")
        if not isinstance(session, dict) or not isinstance(transport, dict):
            raise OpenAILiveProtocolError("OpenAI Live response is missing session or transport")
        session_id = session.get("id")
        sdp_answer = transport.get("sdp")
        if not isinstance(session_id, str) or not isinstance(sdp_answer, str):
            raise OpenAILiveProtocolError("OpenAI Live response has invalid session or SDP data")
        return OpenAILiveWebRTCSession(session_id=session_id, sdp_answer=sdp_answer)


class OpenAILiveConnection:
    def __init__(
        self,
        socket: OpenAILiveSocket,
        *,
        provider_session_id: str,
        responses_enabled: bool,
    ) -> None:
        self._socket = socket
        self._provider_session_id = provider_session_id
        self._responses_enabled = responses_enabled
        self._closed = False

    @property
    def provider_session_id(self) -> str:
        return self._provider_session_id

    async def send_text(self, text: str) -> None:
        self._ensure_open()
        if not text.strip():
            raise ValueError("text must not be empty")
        if not self._responses_enabled:
            raise OpenAILiveProtocolError(
                "Text input requires a Responses delegation model in OpenAILiveProviderOptions"
            )
        await self._send(
            {
                "type": "response.item.create",
                "event_id": _event_id(),
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        await self._send({"type": "response.create", "event_id": _event_id()})

    async def send_audio(self, audio: bytes) -> None:
        self._ensure_open()
        if not audio:
            raise ValueError("audio must not be empty")
        await self._send(
            {
                "type": "session.input_audio.append",
                "event_id": _event_id(),
                "audio": base64.b64encode(audio).decode("ascii"),
            }
        )

    async def interrupt(self) -> None:
        self._ensure_open()
        # Live handles barge-in when new input audio arrives. Ensure input is active so
        # the next frame can interrupt generated speech on the provider timeline.
        await self._send({"type": "session.input_audio.unmute", "event_id": _event_id()})

    async def close(self) -> None:
        if self._closed:
            return
        try:
            await self._send({"type": "session.close", "event_id": _event_id()})
        finally:
            self._closed = True
            await self._socket.close()

    async def events(self) -> AsyncIterator[RealtimeProviderEvent]:
        while not self._closed:
            raw = await self._socket.recv()
            event = _decode_event(raw)
            normalized = normalize_openai_live_event(event)
            if normalized is not None:
                yield normalized
            if event.get("type") == "session.closed":
                self._closed = True
                return

    async def _send(self, event: dict[str, Any]) -> None:
        await self._socket.send(json.dumps(event, separators=(",", ":")))

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("OpenAI Live connection is closed")


class OpenAILiveProvider:
    name = "openai-live"

    def __init__(
        self,
        *,
        api_key: str,
        connector: OpenAILiveSocketConnector | None = None,
        options: OpenAILiveProviderOptions | None = None,
        tools: Sequence[ToolDefinition] = (),
        websocket_url: str = "wss://api.openai.com/v1/live",
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._api_key = api_key
        self._connector = connector or WebsocketsOpenAILiveConnector()
        self._options = options or OpenAILiveProviderOptions()
        self._tools = tuple(tools)
        self._websocket_url = websocket_url

    async def create_session(self, config: RealtimeSessionConfig) -> RealtimeConnection:
        socket = await self._connector.connect(
            self._websocket_url,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        try:
            await socket.send(
                json.dumps(
                    {
                        "type": "session.start",
                        "event_id": _event_id(),
                        "session": _build_session_config(
                            config,
                            self._options,
                            include_audio_format=True,
                            tools=self._tools,
                        ),
                    },
                    separators=(",", ":"),
                )
            )
            raw = await asyncio.wait_for(
                socket.recv(),
                timeout=self._options.connect_timeout_seconds,
            )
            event = _decode_event(raw)
            if event.get("type") == "error":
                raise OpenAILiveProtocolError("OpenAI Live rejected session.start")
            if event.get("type") != "session.started":
                raise OpenAILiveProtocolError("Expected session.started from OpenAI Live")
            session = event.get("session")
            if not isinstance(session, dict) or not isinstance(session.get("id"), str):
                raise OpenAILiveProtocolError("session.started is missing the session id")
            return OpenAILiveConnection(
                socket,
                provider_session_id=session["id"],
                responses_enabled=self._options.response_model is not None,
            )
        except BaseException:
            await socket.close()
            raise


def normalize_openai_live_event(
    event: Mapping[str, Any],
) -> RealtimeProviderEvent | None:
    event_type = event.get("type")
    provider_event_id = event.get("event_id")
    event_id = provider_event_id if isinstance(provider_event_id, str) else None
    if event_type == "session.input_transcript.delta":
        return _provider_event(
            RealtimeEventType.TRANSCRIPT_DELTA,
            event_id,
            event,
            role="user",
        )
    if event_type == "session.output_transcript.delta":
        return _provider_event(
            RealtimeEventType.TRANSCRIPT_DELTA,
            event_id,
            event,
            role="assistant",
        )
    if event_type == "session.output_audio.delta":
        return _provider_event(RealtimeEventType.AUDIO_OUTPUT, event_id, event)
    if event_type == "transport.dtmf.received":
        digit = event.get("event")
        if isinstance(digit, str) and digit in "0123456789*#ABCD":
            return RealtimeProviderEvent(
                type=RealtimeEventType.DTMF_RECEIVED,
                provider_event_id=event_id,
                payload={"digit": digit},
            )
        return None
    if event_type == "session.usage.updated":
        return _provider_event(
            RealtimeEventType.USAGE_UPDATED,
            event_id,
            event,
            delivery=RealtimeEventDelivery.DURABLE,
        )
    if event_type == "session.closed":
        return _provider_event(
            RealtimeEventType.SESSION_DISCONNECTED,
            event_id,
            event,
            delivery=RealtimeEventDelivery.DURABLE,
        )
    if event_type == "error":
        return _provider_event(
            RealtimeEventType.ERROR,
            event_id,
            event,
            delivery=RealtimeEventDelivery.DURABLE,
        )
    if event_type == "response.event":
        nested = event.get("event")
        if isinstance(nested, dict):
            nested_type = nested.get("type")
            if nested_type in {
                "response.output_text.delta",
                "response.audio_transcript.delta",
            }:
                delta = nested.get("delta")
                if isinstance(delta, str):
                    return RealtimeProviderEvent(
                        type=RealtimeEventType.TRANSCRIPT_DELTA,
                        provider_event_id=event_id,
                        payload={"role": "assistant", "delta": delta},
                    )
            if nested_type == "response.completed":
                return _provider_event(
                    RealtimeEventType.TURN_COMPLETED,
                    event_id,
                    event,
                    delivery=RealtimeEventDelivery.DURABLE,
                )
    return None


def _provider_event(
    event_type: RealtimeEventType,
    provider_event_id: str | None,
    event: Mapping[str, Any],
    *,
    role: str | None = None,
    delivery: RealtimeEventDelivery = RealtimeEventDelivery.EPHEMERAL,
) -> RealtimeProviderEvent:
    payload = {
        key: value for key, value in event.items() if key not in {"type", "event_id", "error"}
    }
    if role is not None:
        payload["role"] = role
    if event_type == RealtimeEventType.ERROR:
        error = event.get("error")
        if isinstance(error, dict):
            payload["code"] = error.get("code")
            payload["message"] = error.get("message")
    return RealtimeProviderEvent(
        type=event_type,
        delivery=delivery,
        provider_event_id=provider_event_id,
        payload=payload,
    )


def _build_session_config(
    config: RealtimeSessionConfig,
    options: OpenAILiveProviderOptions,
    *,
    include_audio_format: bool,
    tools: Sequence[ToolDefinition] = (),
) -> dict[str, Any]:
    session: dict[str, Any] = {"model": config.model, "store": options.store}
    if options.instructions:
        session["instructions"] = options.instructions
    audio: dict[str, Any] = {}
    if include_audio_format:
        audio["format"] = {"type": "audio/pcm", "rate": 24_000}
    if options.voice:
        audio["output"] = {"voice": options.voice}
    if audio:
        session["audio"] = audio
    if options.response_model:
        responses: dict[str, Any] = {
            "model": options.response_model,
            "parallel_tool_calls": options.parallel_tool_calls,
        }
        if options.response_instructions:
            responses["instructions"] = options.response_instructions
        if tools:
            responses["tools"] = [serialize_openai_tool(definition) for definition in tools]
            responses["tool_choice"] = "auto"
        session["delegation"] = {"type": "responses", "responses": responses}
    elif config.task_delegation:
        session["delegation"] = {"type": "client"}
    return session


def _decode_event(raw: str | bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OpenAILiveProtocolError("OpenAI Live sent invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise OpenAILiveProtocolError("OpenAI Live sent a non-object event")
    return cast(dict[str, Any], decoded)


def _event_id() -> str:
    return f"evt_{uuid4().hex}"

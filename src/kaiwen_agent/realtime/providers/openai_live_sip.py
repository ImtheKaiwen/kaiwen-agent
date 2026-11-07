from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from kaiwen_agent.realtime.config import RealtimeChannel, RealtimeSessionConfig
from kaiwen_agent.realtime.providers.openai_live import (
    OpenAILiveError,
    OpenAILiveProviderOptions,
    _build_session_config,
)
from kaiwen_agent.realtime.session import RealtimeSessionRecord, RealtimeSessionStatus
from kaiwen_agent.telephony.base import IncomingPhoneCall
from kaiwen_agent.tools.definition import ToolDefinition


class OpenAILiveSIPHTTPTransport(Protocol):
    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> None: ...


class UrllibOpenAILiveSIPHTTPTransport:
    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> None:
        await asyncio.to_thread(
            self._post_sync,
            url,
            dict(headers),
            body,
            timeout_seconds,
        )

    @staticmethod
    def _post_sync(
        url: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> None:
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request_headers = dict(headers)
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request = Request(url, data=payload, headers=request_headers, method="POST")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                response.read()
        except HTTPError as exc:
            raise OpenAILiveError(f"OpenAI Live SIP request failed with HTTP {exc.code}") from exc
        except URLError as exc:
            raise OpenAILiveError("OpenAI Live SIP request could not connect") from exc


class OpenAILiveSIPService:
    """Controls verified inbound GPT-Live SIP sessions from a trusted backend."""

    def __init__(
        self,
        *,
        api_key: str,
        options: OpenAILiveProviderOptions | None = None,
        tools: Sequence[ToolDefinition] = (),
        transport: OpenAILiveSIPHTTPTransport | None = None,
        base_url: str = "https://api.openai.com/v1/live/sessions",
        timeout_seconds: float = 15,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._api_key = api_key
        self._options = options or OpenAILiveProviderOptions()
        self._tools = tuple(tools)
        self._transport = transport or UrllibOpenAILiveSIPHTTPTransport()
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def accept(
        self,
        call: IncomingPhoneCall,
        *,
        config: RealtimeSessionConfig,
        conversation_id: str,
        tenant_id: str,
        user_id: str,
    ) -> RealtimeSessionRecord:
        self._validate_call(call)
        if config.provider != "openai-live":
            raise ValueError("config provider must be openai-live")
        phone_config = config.model_copy(update={"channel": RealtimeChannel.PHONE})
        session = _build_session_config(
            phone_config,
            self._options,
            include_audio_format=False,
            tools=self._tools,
        )
        session["type"] = "live"
        await self._post(call, "accept", {"session": session})
        return RealtimeSessionRecord(
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            channel=RealtimeChannel.PHONE,
            provider="openai-live",
            provider_session_id=call.provider_call_id,
            status=RealtimeSessionStatus.ACTIVE,
            config=phone_config,
            metadata={
                "telephony_event_id": call.event_id,
                "caller": call.caller,
                "callee": call.callee,
            },
        )

    async def reject(self, call: IncomingPhoneCall, *, status_code: int = 603) -> None:
        self._validate_call(call)
        if not 300 <= status_code <= 699:
            raise ValueError("status_code must be between 300 and 699")
        await self._post(call, "reject", {"status_code": status_code})

    async def refer(self, call: IncomingPhoneCall, *, target_uri: str) -> None:
        self._validate_call(call)
        if not target_uri.startswith(("sip:", "tel:")):
            raise ValueError("target_uri must use sip: or tel:")
        await self._post(call, "refer", {"target_uri": target_uri})

    async def hangup(self, call: IncomingPhoneCall) -> None:
        self._validate_call(call)
        await self._post(call, "hangup", None)

    async def _post(
        self,
        call: IncomingPhoneCall,
        action: str,
        body: dict[str, Any] | None,
    ) -> None:
        session_id = quote(call.provider_call_id, safe="")
        await self._transport.post(
            f"{self._base_url}/{session_id}/{action}",
            headers={"Authorization": f"Bearer {self._api_key}"},
            body=body,
            timeout_seconds=self._timeout_seconds,
        )

    @staticmethod
    def _validate_call(call: IncomingPhoneCall) -> None:
        if call.provider != "openai-live":
            raise ValueError("call provider must be openai-live")


def parse_verified_openai_live_sip_event(event: Mapping[str, Any]) -> IncomingPhoneCall:
    """Parse an event only after the consumer verifies the OpenAI webhook signature."""

    event_type = event.get("type")
    if event_type not in {"live.transport.incoming", "live.call.incoming"}:
        raise ValueError("event is not an OpenAI Live incoming transport event")
    event_id = event.get("id")
    data = event.get("data")
    if not isinstance(event_id, str) or not isinstance(data, Mapping):
        raise ValueError("incoming event is missing id or data")
    if event_type == "live.transport.incoming" and data.get("type") != "sip":
        raise ValueError("incoming Live transport is not SIP")
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("incoming SIP event is missing session_id")

    raw_headers = data.get("sip_headers", [])
    headers: list[dict[str, str]] = []
    if isinstance(raw_headers, list):
        for item in raw_headers:
            if not isinstance(item, Mapping):
                continue
            name = item.get("name")
            value = item.get("value")
            if isinstance(name, str) and isinstance(value, str):
                headers.append({"name": name, "value": value})
    header_map = {item["name"].lower(): item["value"] for item in headers}
    return IncomingPhoneCall(
        event_id=event_id,
        provider="openai-live",
        provider_call_id=session_id,
        caller=header_map.get("from"),
        callee=header_map.get("to"),
        metadata={"sip_headers": headers},
    )


__all__ = [
    "OpenAILiveSIPHTTPTransport",
    "OpenAILiveSIPService",
    "UrllibOpenAILiveSIPHTTPTransport",
    "parse_verified_openai_live_sip_event",
]

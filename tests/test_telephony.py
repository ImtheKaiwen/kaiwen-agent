import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import BaseModel

from kaiwen_agent import AgentContext, tool
from kaiwen_agent.realtime import (
    OpenAILiveProviderOptions,
    OpenAILiveSIPService,
    RealtimeChannel,
    RealtimeEventType,
    RealtimeSessionConfig,
    RealtimeSessionStatus,
    normalize_openai_live_event,
    parse_verified_openai_live_sip_event,
)
from kaiwen_agent.telephony import (
    IncomingPhoneCall,
    InMemoryIncomingCallDecisionStore,
    OutboundPhoneCall,
)
from kaiwen_agent.telephony.providers import TwilioSIPOutboundService


class FakeSIPTransport:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> None:
        self.requests.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )


class FakeTwilioTransport:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def post_form(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        form: Mapping[str, str],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        self.requests.append(
            {
                "url": url,
                "headers": dict(headers),
                "form": dict(form),
                "timeout_seconds": timeout_seconds,
            }
        )
        return {"sid": "CA123456789", "status": "queued"}


class EmptyInput(BaseModel):
    pass


@tool(
    name="projects.read",
    description="Read projects",
    input_model=EmptyInput,
    permissions={"projects.read"},
)
async def read_projects(context: AgentContext, values: EmptyInput) -> dict[str, bool]:
    del context, values
    return {"ok": True}


def incoming_call() -> IncomingPhoneCall:
    return IncomingPhoneCall(
        event_id="evt_phone_1",
        provider="openai-live",
        provider_call_id="live/phone 1",
        caller="sip:+905551112233@example.com",
        callee="sip:+902121112233@example.com",
    )


def test_parse_verified_live_sip_webhook_keeps_headers_untrusted_metadata() -> None:
    call = parse_verified_openai_live_sip_event(
        {
            "id": "evt_1",
            "type": "live.transport.incoming",
            "data": {
                "type": "sip",
                "session_id": "live_123",
                "sip_headers": [
                    {"name": "From", "value": "sip:+90555@example.com"},
                    {"name": "To", "value": "sip:+90212@example.com"},
                ],
            },
        }
    )
    assert call.provider_call_id == "live_123"
    assert call.caller == "sip:+90555@example.com"
    assert call.metadata["sip_headers"][0]["name"] == "From"


def test_parse_verified_live_sip_webhook_rejects_wrong_transport() -> None:
    with pytest.raises(ValueError, match="not SIP"):
        parse_verified_openai_live_sip_event(
            {
                "id": "evt_1",
                "type": "live.transport.incoming",
                "data": {"type": "webrtc", "session_id": "live_123"},
            }
        )


def test_openai_live_sip_accept_returns_phone_session_and_configures_tools() -> None:
    async def scenario() -> None:
        transport = FakeSIPTransport()
        service = OpenAILiveSIPService(
            api_key="server-secret",
            transport=transport,
            options=OpenAILiveProviderOptions(
                instructions="Answer briefly.",
                voice="marin",
                response_model="gpt-5.6-luna",
            ),
            tools=[read_projects],
        )
        record = await service.accept(
            incoming_call(),
            config=RealtimeSessionConfig(provider="openai-live", model="gpt-live-1"),
            conversation_id="conversation_1",
            tenant_id="tenant_1",
            user_id="user_1",
        )

        assert record.channel == RealtimeChannel.PHONE
        assert record.status == RealtimeSessionStatus.ACTIVE
        assert record.provider_session_id == "live/phone 1"
        request = transport.requests[0]
        assert request["url"].endswith("/live%2Fphone%201/accept")
        assert request["headers"] == {"Authorization": "Bearer server-secret"}
        session = request["body"]["session"]
        assert session["type"] == "live"
        assert "format" not in session["audio"]
        assert session["delegation"]["responses"]["tools"][0]["name"] == "projects__read"

    asyncio.run(scenario())


def test_openai_live_sip_controls_validate_and_use_expected_endpoints() -> None:
    async def scenario() -> None:
        transport = FakeSIPTransport()
        service = OpenAILiveSIPService(api_key="secret", transport=transport)
        call = incoming_call()
        await service.reject(call, status_code=486)
        await service.refer(call, target_uri="tel:+905551112233")
        await service.hangup(call)
        assert [request["url"].rsplit("/", 1)[-1] for request in transport.requests] == [
            "reject",
            "refer",
            "hangup",
        ]
        assert transport.requests[2]["body"] is None
        with pytest.raises(ValueError, match="between 300 and 699"):
            await service.reject(call, status_code=200)
        with pytest.raises(ValueError, match="sip: or tel:"):
            await service.refer(call, target_uri="https://example.com")

    asyncio.run(scenario())


def test_incoming_call_decision_store_claims_once_under_concurrency() -> None:
    async def scenario() -> None:
        store = InMemoryIncomingCallDecisionStore()
        results = await asyncio.gather(*(store.claim("evt_1") for _ in range(10)))
        assert results.count(True) == 1
        assert results.count(False) == 9
        await store.release("evt_1")
        assert await store.claim("evt_1") is True
        await store.complete("evt_1")
        assert await store.claim("evt_1") is False

    asyncio.run(scenario())


def test_openai_live_normalizes_phone_dtmf() -> None:
    event = normalize_openai_live_event(
        {"type": "transport.dtmf.received", "event_id": "evt_dtmf", "event": "#"}
    )
    assert event is not None
    assert event.type == RealtimeEventType.DTMF_RECEIVED
    assert event.payload == {"digit": "#"}


def test_twilio_outbound_call_bridges_pstn_to_sip_without_exposing_credentials() -> None:
    async def scenario() -> None:
        transport = FakeTwilioTransport()
        service = TwilioSIPOutboundService(
            account_sid="AC123456789",
            auth_token="twilio-secret",
            from_number="+12025550123",
            sip_target_uri=("sip:project_123@sip.api.openai.com;transport=tls;secure=true"),
            transport=transport,
        )
        receipt = await service.place_call(
            OutboundPhoneCall(
                to_number="+905551112233",
                status_callback_url="https://api.example.com/call-status",
                sip_headers={"x-kaiwen-binding": "opaque-token"},
            )
        )

        assert receipt.provider_call_id == "CA123456789"
        request = transport.requests[0]
        assert request["url"].endswith("/Accounts/AC123456789/Calls.json")
        assert request["headers"]["Authorization"].startswith("Basic ")
        assert "twilio-secret" not in request["headers"]["Authorization"]
        assert request["form"]["To"] == "+905551112233"
        assert "transport=tls;secure=true" in request["form"]["Twiml"]
        assert "x-kaiwen-binding=opaque-token" in request["form"]["Twiml"]
        assert request["form"]["StatusCallbackEvent"] == ("initiated ringing answered completed")

    asyncio.run(scenario())


def test_twilio_outbound_call_rejects_unsafe_callback_and_sip_headers() -> None:
    async def scenario() -> None:
        service = TwilioSIPOutboundService(
            account_sid="AC123456789",
            auth_token="secret",
            from_number="+12025550123",
            sip_target_uri="sip:project_123@sip.api.openai.com",
            transport=FakeTwilioTransport(),
        )
        with pytest.raises(ValueError, match="https"):
            await service.place_call(
                OutboundPhoneCall(
                    to_number="+905551112233",
                    status_callback_url="http://api.example.com/call-status",
                )
            )
        with pytest.raises(ValueError, match="safe x- headers"):
            await service.place_call(
                OutboundPhoneCall(
                    to_number="+905551112233",
                    sip_headers={"Authorization": "forged"},
                )
            )

    asyncio.run(scenario())

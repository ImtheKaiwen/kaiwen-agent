import asyncio
import json
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import BaseModel

from kaiwen_agent import AgentContext, ToolExecutor, tool
from kaiwen_agent.realtime import (
    OpenAILiveProtocolError,
    OpenAILiveProvider,
    OpenAILiveProviderOptions,
    OpenAILiveSidebandService,
    OpenAILiveSidebandToolBridge,
    OpenAILiveWebRTCService,
    RealtimeEventDelivery,
    RealtimeEventType,
    RealtimeSessionConfig,
    RealtimeSessionRecord,
    RealtimeSessionStatus,
    normalize_openai_live_event,
)


class FakeLiveSocket:
    def __init__(self, received: list[dict[str, Any]]) -> None:
        self.received: asyncio.Queue[str] = asyncio.Queue()
        for event in received:
            self.received.put_nowait(json.dumps(event))
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        return await self.received.get()

    async def close(self) -> None:
        self.closed = True


class FakeLiveConnector:
    def __init__(self, socket: FakeLiveSocket) -> None:
        self.socket = socket
        self.url: str | None = None
        self.headers: dict[str, str] = {}

    async def connect(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
    ) -> FakeLiveSocket:
        self.url = url
        self.headers = dict(headers)
        return self.socket


class FakeLiveHTTPTransport:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.url: str | None = None
        self.headers: dict[str, str] = {}
        self.body: dict[str, Any] = {}

    async def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        del timeout_seconds
        self.url = url
        self.headers = dict(headers)
        self.body = body
        return self.response


def live_config() -> RealtimeSessionConfig:
    return RealtimeSessionConfig(provider="openai-live", model="gpt-live-1")


def test_openai_live_primary_websocket_lifecycle() -> None:
    async def scenario() -> None:
        socket = FakeLiveSocket([{"type": "session.started", "session": {"id": "live_123"}}])
        connector = FakeLiveConnector(socket)
        provider = OpenAILiveProvider(
            api_key="server-secret",
            connector=connector,
            options=OpenAILiveProviderOptions(
                instructions="Be concise",
                voice="marin",
                response_model="gpt-5-mini",
            ),
        )
        connection = await provider.create_session(live_config())
        assert connection.provider_session_id == "live_123"
        assert connector.url == "wss://api.openai.com/v1/live"
        assert connector.headers == {"Authorization": "Bearer server-secret"}

        started = socket.sent[0]
        assert started["type"] == "session.start"
        assert started["session"]["model"] == "gpt-live-1"
        assert started["session"]["audio"] == {
            "format": {"type": "audio/pcm", "rate": 24_000},
            "output": {"voice": "marin"},
        }
        assert started["session"]["delegation"] == {
            "type": "responses",
            "responses": {
                "model": "gpt-5-mini",
                "parallel_tool_calls": False,
            },
        }

        await connection.send_text("Summarize today")
        await connection.send_audio(b"\x00\x01")
        await connection.interrupt()
        assert socket.sent[1]["type"] == "response.item.create"
        assert socket.sent[2]["type"] == "response.create"
        assert socket.sent[3]["type"] == "session.input_audio.append"
        assert socket.sent[3]["audio"] == "AAE="
        assert socket.sent[4]["type"] == "session.input_audio.unmute"

        await connection.close()
        assert socket.sent[-1]["type"] == "session.close"
        assert socket.closed

    asyncio.run(scenario())


class ProjectQuery(BaseModel):
    slug: str


@tool(
    name="projects.admin.get",
    description="Read one project",
    input_model=ProjectQuery,
    permissions={"projects.read"},
)
async def read_project(context: AgentContext, query: ProjectQuery) -> dict[str, str]:
    del context
    return {"slug": query.slug, "status": "draft"}


@tool(
    name="projects.admin.publish",
    description="Publish one project",
    input_model=ProjectQuery,
    permissions={"projects.publish"},
    side_effect="reversible",
    requires_approval=True,
)
async def publish_project(context: AgentContext, query: ProjectQuery) -> dict[str, str]:
    context.services["writes"].append(query.slug)
    return {"slug": query.slug, "status": "published"}


def active_live_session() -> RealtimeSessionRecord:
    return RealtimeSessionRecord(
        conversation_id="conversation_1",
        tenant_id="tenant_1",
        user_id="user_1",
        channel=live_config().channel,
        provider="openai-live",
        provider_session_id="live_browser_1",
        status=RealtimeSessionStatus.ACTIVE,
        config=live_config(),
    )


def test_webrtc_session_registers_backend_tools_with_strict_schemas() -> None:
    async def scenario() -> None:
        transport = FakeLiveHTTPTransport(
            {
                "session": {"id": "live_browser"},
                "transport": {"type": "webrtc", "sdp": "answer-sdp"},
            }
        )
        service = OpenAILiveWebRTCService(api_key="server-secret", transport=transport)
        await service.create_session(
            sdp_offer="offer-sdp",
            config=live_config(),
            options=OpenAILiveProviderOptions(
                response_model="gpt-5.6-luna",
                response_instructions="Use verified project records.",
            ),
            tools=[read_project],
        )
        responses = transport.body["session"]["delegation"]["responses"]
        assert responses["model"] == "gpt-5.6-luna"
        assert responses["instructions"] == "Use verified project records."
        assert responses["parallel_tool_calls"] is False
        assert responses["tool_choice"] == "auto"
        assert responses["tools"][0]["name"] == "projects__admin__get"
        assert responses["tools"][0]["parameters"]["additionalProperties"] is False

    asyncio.run(scenario())


def test_sideband_attaches_without_restarting_or_closing_primary_session() -> None:
    async def scenario() -> None:
        socket = FakeLiveSocket([])
        connector = FakeLiveConnector(socket)
        service = OpenAILiveSidebandService(
            api_key="server-secret",
            connector=connector,
        )
        connection = await service.attach(active_live_session())
        assert connector.url == ("wss://api.openai.com/v1/live/sessions/live_browser_1/attach")
        assert connector.headers == {"Authorization": "Bearer server-secret"}
        assert socket.sent == []
        await connection.close()
        assert socket.sent == []
        assert socket.closed

    asyncio.run(scenario())


def test_sideband_tool_bridge_uses_safe_executor_and_continues_once() -> None:
    async def scenario() -> None:
        socket = FakeLiveSocket(
            [
                {
                    "type": "response.event",
                    "delegation_id": "delegation_1",
                    "event": {"type": "response.created", "response": {"id": "resp_1"}},
                },
                {
                    "type": "response.event",
                    "delegation_id": "delegation_1",
                    "event": {
                        "type": "response.output_item.done",
                        "response_id": "resp_1",
                        "item": {
                            "type": "function_call",
                            "call_id": "call_1",
                            "name": "projects__admin__get",
                            "arguments": '{"slug":"cube-rivals"}',
                        },
                    },
                },
                {
                    "type": "response.event",
                    "delegation_id": "delegation_1",
                    "event": {
                        "type": "response.completed",
                        "response": {"id": "resp_1", "output": []},
                    },
                },
                {
                    "type": "response.event",
                    "delegation_id": "delegation_1",
                    "event": {
                        "type": "response.completed",
                        "response": {"id": "resp_1", "output": []},
                    },
                },
                {"type": "session.closed"},
            ]
        )
        session = active_live_session()
        connection = await OpenAILiveSidebandService(
            api_key="secret",
            connector=FakeLiveConnector(socket),
        ).attach(session)
        bridge = OpenAILiveSidebandToolBridge(
            connection,
            tools=[read_project],
            tool_executor=ToolExecutor(),
            context=AgentContext(
                session_id=session.id,
                tenant_id="tenant_1",
                user_id="user_1",
                permissions=frozenset({"projects.read"}),
            ),
        )
        await bridge.run()
        assert [event["type"] for event in socket.sent] == [
            "response.item.create",
            "response.create",
        ]
        output = json.loads(socket.sent[0]["item"]["output"])
        assert output == {"slug": "cube-rivals", "status": "draft"}

    asyncio.run(scenario())


def test_sideband_tool_bridge_never_writes_without_approval() -> None:
    async def scenario() -> None:
        socket = FakeLiveSocket(
            [
                {
                    "type": "response.event",
                    "delegation_id": "delegation_2",
                    "event": {"type": "response.created", "response": {"id": "resp_2"}},
                },
                {
                    "type": "response.event",
                    "delegation_id": "delegation_2",
                    "event": {
                        "type": "response.output_item.done",
                        "response_id": "resp_2",
                        "item": {
                            "type": "function_call",
                            "call_id": "call_2",
                            "name": "projects__admin__publish",
                            "arguments": '{"slug":"cube-rivals"}',
                        },
                    },
                },
                {
                    "type": "response.event",
                    "delegation_id": "delegation_2",
                    "event": {
                        "type": "response.completed",
                        "response": {"id": "resp_2", "output": []},
                    },
                },
                {"type": "session.closed"},
            ]
        )
        writes: list[str] = []
        session = active_live_session()
        connection = await OpenAILiveSidebandService(
            api_key="secret",
            connector=FakeLiveConnector(socket),
        ).attach(session)
        bridge = OpenAILiveSidebandToolBridge(
            connection,
            tools=[publish_project],
            tool_executor=ToolExecutor(),
            context=AgentContext(
                session_id=session.id,
                tenant_id="tenant_1",
                user_id="user_1",
                permissions=frozenset({"projects.publish"}),
                services={"writes": writes},
            ),
        )
        await bridge.run()
        assert writes == []
        output = json.loads(socket.sent[0]["item"]["output"])
        assert output == {"ok": False, "error": {"code": "approval_required"}}
        assert socket.sent[1]["type"] == "response.create"

    asyncio.run(scenario())


def test_sideband_tool_bridge_rejects_cross_user_context() -> None:
    async def scenario() -> None:
        session = active_live_session()
        connection = await OpenAILiveSidebandService(
            api_key="secret",
            connector=FakeLiveConnector(FakeLiveSocket([])),
        ).attach(session)
        with pytest.raises(ValueError, match="context user"):
            OpenAILiveSidebandToolBridge(
                connection,
                tools=[read_project],
                tool_executor=ToolExecutor(),
                context=AgentContext(
                    session_id=session.id,
                    tenant_id="tenant_1",
                    user_id="another_user",
                    permissions=frozenset({"projects.read"}),
                ),
            )

    asyncio.run(scenario())


def test_text_requires_responses_delegation() -> None:
    async def scenario() -> None:
        socket = FakeLiveSocket([{"type": "session.started", "session": {"id": "live_123"}}])
        connection = await OpenAILiveProvider(
            api_key="secret",
            connector=FakeLiveConnector(socket),
        ).create_session(live_config())
        with pytest.raises(OpenAILiveProtocolError, match="Responses delegation"):
            await connection.send_text("hello")

    asyncio.run(scenario())


def test_provider_rejects_unexpected_start_event_and_closes_socket() -> None:
    async def scenario() -> None:
        socket = FakeLiveSocket([{"type": "session.updated"}])
        provider = OpenAILiveProvider(
            api_key="secret",
            connector=FakeLiveConnector(socket),
        )
        with pytest.raises(OpenAILiveProtocolError, match="session.started"):
            await provider.create_session(live_config())
        assert socket.closed

    asyncio.run(scenario())


def test_live_event_normalization_keeps_ephemeral_and_durable_events_separate() -> None:
    transcript = normalize_openai_live_event(
        {
            "type": "session.output_transcript.delta",
            "event_id": "event_1",
            "delta": "Merhaba",
            "start_ms": 10,
            "end_ms": 20,
        }
    )
    assert transcript is not None
    assert transcript.type == RealtimeEventType.TRANSCRIPT_DELTA
    assert transcript.delivery == RealtimeEventDelivery.EPHEMERAL
    assert transcript.payload["role"] == "assistant"

    usage = normalize_openai_live_event(
        {
            "type": "session.usage.updated",
            "event_id": "event_2",
            "usage": {"seconds": 4.5},
        }
    )
    assert usage is not None
    assert usage.type == RealtimeEventType.USAGE_UPDATED
    assert usage.delivery == RealtimeEventDelivery.DURABLE
    assert usage.payload["usage"] == {"seconds": 4.5}

    response_delta = normalize_openai_live_event(
        {
            "type": "response.event",
            "event_id": "event_3",
            "event": {"type": "response.output_text.delta", "delta": "Plan hazır."},
        }
    )
    assert response_delta is not None
    assert response_delta.type == RealtimeEventType.TRANSCRIPT_DELTA
    assert response_delta.payload == {"role": "assistant", "delta": "Plan hazır."}

    assert normalize_openai_live_event({"type": "future.event"}) is None


def test_webrtc_service_builds_server_authorized_session_request() -> None:
    async def scenario() -> None:
        transport = FakeLiveHTTPTransport(
            {
                "session": {"id": "live_browser"},
                "transport": {"type": "webrtc", "sdp": "answer-sdp"},
            }
        )
        service = OpenAILiveWebRTCService(
            api_key="server-secret",
            transport=transport,
        )
        session = await service.create_session(
            sdp_offer="offer-sdp",
            config=live_config(),
            options=OpenAILiveProviderOptions(voice="marin"),
        )
        assert session.session_id == "live_browser"
        assert session.sdp_answer == "answer-sdp"
        assert transport.url == "https://api.openai.com/v1/live/sessions"
        assert transport.headers == {"Authorization": "Bearer server-secret"}
        assert transport.body["transport"] == {
            "type": "webrtc",
            "sdp": "offer-sdp",
        }
        assert "server-secret" not in json.dumps(transport.body)
        assert "format" not in transport.body["session"].get("audio", {})

    asyncio.run(scenario())

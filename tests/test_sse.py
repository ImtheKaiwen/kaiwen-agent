import asyncio
import json

from kaiwen_agent.events import AgentEvent, EventScope, InMemoryEventSink
from kaiwen_agent.transports.sse import SSEEventBroker, encode_sse


def test_encode_sse_uses_id_type_and_json_payload() -> None:
    event = AgentEvent(
        id="evt_1",
        type="run.started",
        trace_id="trace_1",
        session_id="session_1",
        payload={"agent": "test"},
    )
    encoded = encode_sse(event)
    assert encoded.startswith("id: evt_1\nevent: run.started\ndata: ")
    data = json.loads(encoded.split("data: ", 1)[1])
    assert data["payload"] == {"agent": "test"}


def test_sse_redacts_payload_mutated_after_event_validation() -> None:
    event = AgentEvent(
        id="evt_secret",
        type="run.failed",
        trace_id="trace_1",
        session_id="session_1",
    )
    event.payload["authorization"] = "Bearer private-token"

    assert "private-token" not in encode_sse(event)


def test_sse_broker_persists_and_fans_out_by_session() -> None:
    async def scenario() -> None:
        sink = InMemoryEventSink()
        broker = SSEEventBroker(sink)
        stream = broker.stream("session_1")
        next_event = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        await broker.append(
            AgentEvent(
                id="evt_1",
                type="run.started",
                trace_id="trace_1",
                session_id="session_1",
            )
        )
        encoded = await asyncio.wait_for(next_event, timeout=1)
        await stream.aclose()
        assert "event: run.started" in encoded
        assert [event.id for event in sink.events] == ["evt_1"]

    asyncio.run(scenario())


def test_in_memory_event_reads_require_exact_owner_scope() -> None:
    async def scenario() -> None:
        sink = InMemoryEventSink()
        for tenant_id, user_id in (("tenant_1", "user_1"), ("tenant_2", "user_2")):
            await sink.append(
                AgentEvent(
                    type="run.started",
                    trace_id=f"trace:{tenant_id}",
                    session_id="shared-session",
                    tenant_id=tenant_id,
                    user_id=user_id,
                )
            )
        owned = await sink.list_events_scoped(
            scope=EventScope(
                tenant_id="tenant_1",
                user_id="user_1",
                session_id="shared-session",
            )
        )
        assert [(event.tenant_id, event.user_id) for event in owned] == [("tenant_1", "user_1")]

    asyncio.run(scenario())

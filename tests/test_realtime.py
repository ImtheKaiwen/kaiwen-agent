import asyncio

import pytest
from pydantic import ValidationError

from kaiwen_agent.realtime import (
    FakeRealtimeProvider,
    InMemoryRealtimeEventSink,
    InMemoryRealtimeSessionStore,
    InvalidRealtimeSessionTransitionError,
    RealtimeEventType,
    RealtimeLimits,
    RealtimeMode,
    RealtimeProviderEvent,
    RealtimeRuntimeEvent,
    RealtimeSessionConfig,
    RealtimeSessionLimitError,
    RealtimeSessionManager,
    RealtimeSessionRecord,
    RealtimeSessionStatus,
    transition_realtime_session,
)


def test_durable_realtime_event_redacts_secrets_at_the_contract_boundary() -> None:
    event = RealtimeRuntimeEvent(
        type=RealtimeEventType.ERROR,
        session_id="session_1",
        conversation_id="conversation_1",
        tenant_id="tenant_1",
        user_id="user_1",
        payload={
            "provider_secret": "private-value",
            "message": "Bearer private-token",
        },
    )

    assert event.payload == {
        "provider_secret": "[REDACTED]",
        "message": "[REDACTED]",
    }

    event.payload["api_key"] = "mutated-secret"
    sink = InMemoryRealtimeEventSink()
    asyncio.run(sink.append(event))
    assert sink.events[0].payload["api_key"] == "[REDACTED]"


def make_config() -> RealtimeSessionConfig:
    return RealtimeSessionConfig(provider="fake", model="live-test")


def test_objective_requirement_needs_objective_mode() -> None:
    with pytest.raises(ValidationError, match="objective_driven"):
        RealtimeSessionConfig(
            provider="fake",
            model="live-test",
            require_objective=True,
        )
    config = RealtimeSessionConfig(
        provider="fake",
        model="live-test",
        mode=RealtimeMode.OBJECTIVE_DRIVEN,
        require_objective=True,
    )
    assert config.mode == RealtimeMode.OBJECTIVE_DRIVEN


def test_session_state_machine_rejects_invalid_transition() -> None:
    session = RealtimeSessionRecord(
        conversation_id="conversation",
        tenant_id="tenant",
        user_id="user",
        channel=make_config().channel,
        provider="fake",
        config=make_config(),
    )
    with pytest.raises(InvalidRealtimeSessionTransitionError):
        transition_realtime_session(session, RealtimeSessionStatus.ACTIVE)


def test_manager_session_lifecycle_and_transport_are_isolated() -> None:
    async def scenario() -> None:
        provider = FakeRealtimeProvider()
        events = InMemoryRealtimeEventSink()
        manager = RealtimeSessionManager([provider], events=events)

        first = await manager.create_session(
            make_config(),
            conversation_id="conversation_1",
            tenant_id="tenant",
            user_id="user_1",
        )
        second = await manager.create_session(
            make_config(),
            conversation_id="conversation_2",
            tenant_id="tenant",
            user_id="user_2",
        )
        assert first.status == RealtimeSessionStatus.ACTIVE
        assert second.status == RealtimeSessionStatus.ACTIVE
        assert first.provider_session_id != second.provider_session_id

        await manager.send_text(first.id, "hello")
        await manager.send_audio(first.id, b"audio")
        assert provider.connections[0].sent_text == ["hello"]
        assert provider.connections[0].sent_audio == [b"audio"]
        assert provider.connections[1].sent_text == []

        interrupted = await manager.interrupt(first.id)
        assert interrupted.status == RealtimeSessionStatus.INTERRUPTED
        assert provider.connections[0].interrupt_count == 1
        resumed = await manager.resume(first.id)
        assert resumed.status == RealtimeSessionStatus.ACTIVE
        closed = await manager.disconnect(first.id)
        assert closed.status == RealtimeSessionStatus.CLOSED
        assert provider.connections[0].closed

        event_types = [event.type for event in events.events if event.session_id == first.id]
        assert event_types == [
            RealtimeEventType.SESSION_CREATED,
            RealtimeEventType.SESSION_CONNECTING,
            RealtimeEventType.SESSION_CONNECTED,
            RealtimeEventType.SESSION_INTERRUPTED,
            RealtimeEventType.SESSION_CONNECTED,
            RealtimeEventType.SESSION_DISCONNECTED,
        ]

    asyncio.run(scenario())


def test_provider_event_stream_stops_when_connection_closes() -> None:
    async def scenario() -> None:
        provider = FakeRealtimeProvider()
        manager = RealtimeSessionManager([provider])
        session = await manager.create_session(
            make_config(),
            conversation_id="conversation",
            tenant_id="tenant",
            user_id="user",
        )
        stream = manager.provider_events(session.id)
        next_event = asyncio.create_task(anext(stream))
        event = RealtimeProviderEvent(
            type=RealtimeEventType.TRANSCRIPT_COMPLETED,
            payload={"text": "hello"},
        )
        await provider.connections[0].emit(event)
        received = await next_event
        assert received.payload == {"text": "hello"}
        await manager.disconnect(session.id)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    asyncio.run(scenario())


def test_reconnect_replaces_only_the_target_connection() -> None:
    async def scenario() -> None:
        provider = FakeRealtimeProvider()
        manager = RealtimeSessionManager([provider])
        first = await manager.create_session(
            make_config(),
            conversation_id="one",
            tenant_id="tenant",
            user_id="one",
        )
        second = await manager.create_session(
            make_config(),
            conversation_id="two",
            tenant_id="tenant",
            user_id="two",
        )
        old_provider_session_id = first.provider_session_id
        reconnected = await manager.reconnect(first.id)
        assert reconnected.provider_session_id != old_provider_session_id
        await manager.send_text(second.id, "still active")
        assert provider.connections[1].sent_text == ["still active"]
        assert provider.connections[0].closed

    asyncio.run(scenario())


def test_concurrent_session_creation_enforces_user_limit() -> None:
    async def scenario() -> None:
        provider = FakeRealtimeProvider()
        manager = RealtimeSessionManager(
            [provider],
            limits=RealtimeLimits(
                max_global_sessions=10,
                max_sessions_per_tenant=10,
                max_sessions_per_user=1,
            ),
        )

        async def create(conversation_id: str) -> RealtimeSessionRecord:
            return await manager.create_session(
                make_config(),
                conversation_id=conversation_id,
                tenant_id="tenant",
                user_id="user",
            )

        results = await asyncio.gather(
            create("one"),
            create("two"),
            return_exceptions=True,
        )
        assert sum(isinstance(item, RealtimeSessionRecord) for item in results) == 1
        assert sum(isinstance(item, RealtimeSessionLimitError) for item in results) == 1

    asyncio.run(scenario())


def test_provider_failure_is_recorded_without_provider_message() -> None:
    async def scenario() -> None:
        provider = FakeRealtimeProvider(fail_creation=True)
        store = InMemoryRealtimeSessionStore()
        events = InMemoryRealtimeEventSink()
        manager = RealtimeSessionManager([provider], store=store, events=events)
        with pytest.raises(RuntimeError, match="creation failed"):
            await manager.create_session(
                make_config(),
                conversation_id="conversation",
                tenant_id="tenant",
                user_id="user",
            )
        assert len(events.events) == 3
        failed_event = events.events[-1]
        assert failed_event.type == RealtimeEventType.SESSION_FAILED
        assert failed_event.payload["error_type"] == "RuntimeError"
        active = await store.list_active()
        assert active == []

    asyncio.run(scenario())

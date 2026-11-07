import asyncio

import pytest

from kaiwen_agent.realtime import (
    ConversationModality,
    ConversationRole,
    FakeRealtimeProvider,
    InMemoryConversationStore,
    InMemoryTaskSubmissionPort,
    RealtimeBridge,
    RealtimeEventType,
    RealtimeProviderEvent,
    RealtimeSessionConfig,
    RealtimeSessionManager,
    RealtimeTaskBridge,
    RealtimeTaskNotFoundError,
    RealtimeTaskPermissionError,
)


async def create_session(
    manager: RealtimeSessionManager,
    *,
    tenant_id: str = "tenant",
    user_id: str = "user",
    conversation_id: str = "conversation",
):
    return await manager.create_session(
        RealtimeSessionConfig(provider="fake", model="fake-live"),
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        user_id=user_id,
    )


def test_bridge_assembles_voice_transcripts_into_durable_messages() -> None:
    async def scenario() -> None:
        manager = RealtimeSessionManager([FakeRealtimeProvider()])
        store = InMemoryConversationStore()
        bridge = RealtimeBridge(manager, conversations=store)
        session = await create_session(manager)

        for event_id, delta in [("u1", "Mer"), ("u2", "haba")]:
            await bridge.handle_event(
                session,
                RealtimeProviderEvent(
                    type=RealtimeEventType.TRANSCRIPT_DELTA,
                    provider_event_id=event_id,
                    payload={"role": "user", "delta": delta},
                ),
            )
        await bridge.handle_event(
            session,
            RealtimeProviderEvent(
                type=RealtimeEventType.TRANSCRIPT_DELTA,
                provider_event_id="a1",
                payload={"role": "assistant", "delta": "Selam"},
            ),
        )
        await bridge.handle_event(
            session,
            RealtimeProviderEvent(type=RealtimeEventType.TURN_COMPLETED),
        )

        messages = await store.list_messages(tenant_id="tenant", conversation_id="conversation")
        assert [(item.role, item.text) for item in messages] == [
            (ConversationRole.USER, "Merhaba"),
            (ConversationRole.ASSISTANT, "Selam"),
        ]
        assert all(item.modality == ConversationModality.AUDIO for item in messages)
        assert messages[0].provider_event_ids == ["u1", "u2"]
        assert messages[0].metadata == {"partial": False}

    asyncio.run(scenario())


def test_bridge_persists_text_and_keeps_tenants_isolated() -> None:
    async def scenario() -> None:
        provider = FakeRealtimeProvider()
        manager = RealtimeSessionManager([provider])
        store = InMemoryConversationStore()
        bridge = RealtimeBridge(manager, conversations=store)
        session = await create_session(manager)
        message = await bridge.send_text(session.id, "Planı hazırla")
        assert message.modality == ConversationModality.TEXT
        assert provider.connections[0].sent_text == ["Planı hazırla"]
        assert await store.list_messages(tenant_id="other", conversation_id="conversation") == []

    asyncio.run(scenario())


def test_bridge_flushes_partial_transcript_without_audio_bytes() -> None:
    async def scenario() -> None:
        manager = RealtimeSessionManager([FakeRealtimeProvider()])
        store = InMemoryConversationStore()
        bridge = RealtimeBridge(manager, conversations=store)
        session = await create_session(manager)
        await bridge.handle_event(
            session,
            RealtimeProviderEvent(
                type=RealtimeEventType.TRANSCRIPT_DELTA,
                payload={"role": "user", "delta": "Yarım kalan"},
            ),
        )
        await bridge.flush(session, partial=True)
        messages = await store.list_messages(tenant_id="tenant", conversation_id="conversation")
        assert messages[0].metadata == {"partial": True}
        assert "audio" not in messages[0].model_dump()

    asyncio.run(scenario())


def test_task_bridge_is_allowlisted_and_session_scoped() -> None:
    async def scenario() -> None:
        manager = RealtimeSessionManager([FakeRealtimeProvider()])
        owner = await create_session(manager)
        other = await create_session(
            manager,
            tenant_id="other_tenant",
            user_id="other_user",
            conversation_id="other_conversation",
        )
        submissions = InMemoryTaskSubmissionPort()
        bridge = RealtimeTaskBridge(
            manager,
            submissions,
            allowed_agents={"planner", "analyst"},
            default_agent="planner",
        )
        reference = await bridge.delegate(owner.id, "Repository için plan hazırla")
        task = await submissions.get(reference.task_id)
        assert task is not None
        assert task.tenant_id == "tenant"
        assert task.user_id == "user"
        assert task.conversation_id == "conversation"
        assert task.input == {"request": "Repository için plan hazırla"}
        assert (await bridge.status(owner.id, reference.task_id)).task_id == reference.task_id
        with pytest.raises(RealtimeTaskNotFoundError):
            await bridge.status(other.id, reference.task_id)
        with pytest.raises(RealtimeTaskPermissionError):
            await bridge.delegate(owner.id, "Deploy", preferred_agent="deployer")

    asyncio.run(scenario())

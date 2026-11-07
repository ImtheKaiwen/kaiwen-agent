from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from kaiwen_agent.realtime.conversation import (
    ConversationMessage,
    ConversationModality,
    ConversationRole,
    ConversationStore,
    InMemoryConversationStore,
)
from kaiwen_agent.realtime.events import RealtimeEventType, RealtimeProviderEvent
from kaiwen_agent.realtime.manager import RealtimeSessionManager
from kaiwen_agent.realtime.session import RealtimeSessionRecord


@dataclass(slots=True)
class _TranscriptBuffer:
    parts: list[str] = field(default_factory=list)
    provider_event_ids: list[str] = field(default_factory=list)
    start_ms: int | None = None
    end_ms: int | None = None


class RealtimeBridge:
    """Converges provider events into durable, provider-neutral conversations."""

    def __init__(
        self,
        sessions: RealtimeSessionManager,
        *,
        conversations: ConversationStore | None = None,
    ) -> None:
        self.sessions = sessions
        self.conversations = conversations or InMemoryConversationStore()
        self._buffers: dict[tuple[str, ConversationRole], _TranscriptBuffer] = {}
        self._lock = asyncio.Lock()

    async def run(self, session_id: str) -> None:
        session = await self.sessions.get_session(session_id)
        try:
            async for event in self.sessions.provider_events(session_id):
                await self.handle_event(session, event)
        finally:
            await self.flush(session, partial=True)

    async def send_text(self, session_id: str, text: str) -> ConversationMessage:
        session = await self.sessions.get_session(session_id)
        message = ConversationMessage(
            conversation_id=session.conversation_id,
            tenant_id=session.tenant_id,
            user_id=session.user_id,
            realtime_session_id=session.id,
            role=ConversationRole.USER,
            modality=ConversationModality.TEXT,
            text=text,
        )
        await self.conversations.append_message(message)
        await self.sessions.send_text(session_id, text)
        return message.model_copy(deep=True)

    async def handle_event(
        self,
        session: RealtimeSessionRecord,
        event: RealtimeProviderEvent,
    ) -> None:
        if event.type == RealtimeEventType.TRANSCRIPT_DELTA:
            role = _event_role(event)
            if role is None:
                return
            if role == ConversationRole.ASSISTANT:
                await self._finalize(session, ConversationRole.USER, partial=False)
            await self._append_delta(session, role, event)
            return
        if event.type == RealtimeEventType.TRANSCRIPT_COMPLETED:
            role = _event_role(event)
            if role is not None:
                text = event.payload.get("text")
                if isinstance(text, str) and text.strip():
                    async with self._lock:
                        buffer = self._buffers.setdefault((session.id, role), _TranscriptBuffer())
                        buffer.parts = [text]
                await self._finalize(session, role, partial=False)
            return
        if event.type == RealtimeEventType.TURN_COMPLETED:
            await self._finalize(session, ConversationRole.ASSISTANT, partial=False)
            return
        if event.type in {
            RealtimeEventType.SESSION_DISCONNECTED,
            RealtimeEventType.SESSION_FAILED,
        }:
            await self.flush(session, partial=event.type == RealtimeEventType.SESSION_FAILED)

    async def flush(self, session: RealtimeSessionRecord, *, partial: bool) -> None:
        await self._finalize(session, ConversationRole.USER, partial=partial)
        await self._finalize(session, ConversationRole.ASSISTANT, partial=partial)

    async def _append_delta(
        self,
        session: RealtimeSessionRecord,
        role: ConversationRole,
        event: RealtimeProviderEvent,
    ) -> None:
        delta = event.payload.get("delta")
        if not isinstance(delta, str) or not delta:
            return
        async with self._lock:
            buffer = self._buffers.setdefault((session.id, role), _TranscriptBuffer())
            buffer.parts.append(delta)
            if event.provider_event_id:
                buffer.provider_event_ids.append(event.provider_event_id)
            start_ms = event.payload.get("start_ms")
            end_ms = event.payload.get("end_ms")
            if isinstance(start_ms, int) and buffer.start_ms is None:
                buffer.start_ms = start_ms
            if isinstance(end_ms, int):
                buffer.end_ms = end_ms

    async def _finalize(
        self,
        session: RealtimeSessionRecord,
        role: ConversationRole,
        *,
        partial: bool,
    ) -> ConversationMessage | None:
        async with self._lock:
            buffer = self._buffers.pop((session.id, role), None)
        if buffer is None:
            return None
        text = "".join(buffer.parts).strip()
        if not text:
            return None
        message = ConversationMessage(
            conversation_id=session.conversation_id,
            tenant_id=session.tenant_id,
            user_id=session.user_id,
            realtime_session_id=session.id,
            role=role,
            modality=ConversationModality.AUDIO,
            text=text,
            provider_event_ids=buffer.provider_event_ids,
            start_ms=buffer.start_ms,
            end_ms=buffer.end_ms,
            metadata={"partial": partial},
        )
        await self.conversations.append_message(message)
        return message.model_copy(deep=True)


def _event_role(event: RealtimeProviderEvent) -> ConversationRole | None:
    role = event.payload.get("role")
    if role == ConversationRole.USER.value:
        return ConversationRole.USER
    if role == ConversationRole.ASSISTANT.value:
        return ConversationRole.ASSISTANT
    return None

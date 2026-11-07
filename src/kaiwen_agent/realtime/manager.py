from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from typing import Any

from kaiwen_agent.realtime.base import (
    InMemoryRealtimeEventSink,
    RealtimeConnection,
    RealtimeEventSink,
    RealtimeProvider,
)
from kaiwen_agent.realtime.config import RealtimeLimits, RealtimeSessionConfig
from kaiwen_agent.realtime.events import (
    RealtimeEventType,
    RealtimeProviderEvent,
    RealtimeRuntimeEvent,
)
from kaiwen_agent.realtime.session import (
    InMemoryRealtimeSessionStore,
    RealtimeSessionRecord,
    RealtimeSessionStatus,
    RealtimeSessionStore,
    transition_realtime_session,
)


class RealtimeProviderNotFoundError(LookupError):
    pass


class RealtimeSessionNotFoundError(LookupError):
    pass


class RealtimeSessionLimitError(RuntimeError):
    pass


class RealtimeSessionManager:
    """Owns provider-neutral realtime session lifecycle and isolation."""

    def __init__(
        self,
        providers: Iterable[RealtimeProvider],
        *,
        store: RealtimeSessionStore | None = None,
        events: RealtimeEventSink | None = None,
        limits: RealtimeLimits | None = None,
    ) -> None:
        self._providers = {provider.name: provider for provider in providers}
        self._store = store or InMemoryRealtimeSessionStore()
        self._events = events or InMemoryRealtimeEventSink()
        self._limits = limits or RealtimeLimits()
        self._connections: dict[str, RealtimeConnection] = {}
        self._lock = asyncio.Lock()

    async def create_session(
        self,
        config: RealtimeSessionConfig,
        *,
        conversation_id: str,
        tenant_id: str,
        user_id: str,
        client_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RealtimeSessionRecord:
        provider = self._providers.get(config.provider)
        if provider is None:
            raise RealtimeProviderNotFoundError(
                f"Realtime provider is not registered: {config.provider}"
            )

        session = RealtimeSessionRecord(
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            client_id=client_id,
            channel=config.channel,
            provider=config.provider,
            config=config,
            metadata=metadata or {},
        )
        async with self._lock:
            await self._enforce_limits(tenant_id=tenant_id, user_id=user_id)
            await self._store.save(session)
        await self._emit(session, RealtimeEventType.SESSION_CREATED)

        transition_realtime_session(session, RealtimeSessionStatus.CONNECTING)
        await self._store.save(session)
        await self._emit(session, RealtimeEventType.SESSION_CONNECTING)
        try:
            connection = await provider.create_session(config.model_copy(deep=True))
        except Exception as exc:
            session.error = type(exc).__name__
            transition_realtime_session(session, RealtimeSessionStatus.FAILED)
            await self._store.save(session)
            await self._emit(
                session,
                RealtimeEventType.SESSION_FAILED,
                {"error_type": type(exc).__name__},
            )
            raise

        session.provider_session_id = connection.provider_session_id
        transition_realtime_session(session, RealtimeSessionStatus.ACTIVE)
        async with self._lock:
            self._connections[session.id] = connection
            await self._store.save(session)
        await self._emit(session, RealtimeEventType.SESSION_CONNECTED)
        return session.model_copy(deep=True)

    async def get_session(self, session_id: str) -> RealtimeSessionRecord:
        session = await self._store.get(session_id)
        if session is None:
            raise RealtimeSessionNotFoundError(f"Realtime session not found: {session_id}")
        return session

    async def list_active(self) -> list[RealtimeSessionRecord]:
        return await self._store.list_active()

    async def send_text(self, session_id: str, text: str) -> None:
        connection = self._get_connection(session_id)
        await connection.send_text(text)

    async def send_audio(self, session_id: str, audio: bytes) -> None:
        connection = self._get_connection(session_id)
        await connection.send_audio(audio)

    def provider_events(self, session_id: str) -> AsyncIterator[RealtimeProviderEvent]:
        return self._get_connection(session_id).events()

    async def interrupt(self, session_id: str) -> RealtimeSessionRecord:
        session = await self.get_session(session_id)
        connection = self._get_connection(session_id)
        await connection.interrupt()
        transition_realtime_session(session, RealtimeSessionStatus.INTERRUPTED)
        await self._store.save(session)
        await self._emit(session, RealtimeEventType.SESSION_INTERRUPTED)
        return session.model_copy(deep=True)

    async def resume(self, session_id: str) -> RealtimeSessionRecord:
        session = await self.get_session(session_id)
        transition_realtime_session(session, RealtimeSessionStatus.ACTIVE)
        await self._store.save(session)
        await self._emit(session, RealtimeEventType.SESSION_CONNECTED, {"resumed": True})
        return session.model_copy(deep=True)

    async def reconnect(self, session_id: str) -> RealtimeSessionRecord:
        session = await self.get_session(session_id)
        provider = self._providers.get(session.provider)
        if provider is None:
            raise RealtimeProviderNotFoundError(
                f"Realtime provider is not registered: {session.provider}"
            )
        old_connection = self._get_connection(session_id)
        transition_realtime_session(session, RealtimeSessionStatus.RECONNECTING)
        await self._store.save(session)
        await self._emit(session, RealtimeEventType.SESSION_RECONNECTING)
        await old_connection.close()
        try:
            connection = await provider.create_session(session.config.model_copy(deep=True))
        except Exception as exc:
            session.error = type(exc).__name__
            transition_realtime_session(session, RealtimeSessionStatus.FAILED)
            async with self._lock:
                self._connections.pop(session.id, None)
                await self._store.save(session)
            await self._emit(
                session,
                RealtimeEventType.SESSION_FAILED,
                {"error_type": type(exc).__name__},
            )
            raise
        session.provider_session_id = connection.provider_session_id
        session.error = None
        transition_realtime_session(session, RealtimeSessionStatus.ACTIVE)
        async with self._lock:
            self._connections[session.id] = connection
            await self._store.save(session)
        await self._emit(session, RealtimeEventType.SESSION_CONNECTED, {"reconnected": True})
        return session.model_copy(deep=True)

    async def disconnect(self, session_id: str) -> RealtimeSessionRecord:
        session = await self.get_session(session_id)
        transition_realtime_session(session, RealtimeSessionStatus.CLOSING)
        await self._store.save(session)
        async with self._lock:
            connection = self._connections.pop(session.id, None)
        if connection is not None:
            await connection.close()
        transition_realtime_session(session, RealtimeSessionStatus.CLOSED)
        await self._store.save(session)
        await self._emit(session, RealtimeEventType.SESSION_DISCONNECTED)
        return session.model_copy(deep=True)

    async def _enforce_limits(self, *, tenant_id: str, user_id: str) -> None:
        active = await self._store.list_active()
        if len(active) >= self._limits.max_global_sessions:
            raise RealtimeSessionLimitError("Global realtime session limit reached")
        tenant_sessions = [item for item in active if item.tenant_id == tenant_id]
        if len(tenant_sessions) >= self._limits.max_sessions_per_tenant:
            raise RealtimeSessionLimitError("Tenant realtime session limit reached")
        user_sessions = [item for item in tenant_sessions if item.user_id == user_id]
        if len(user_sessions) >= self._limits.max_sessions_per_user:
            raise RealtimeSessionLimitError("User realtime session limit reached")

    def _get_connection(self, session_id: str) -> RealtimeConnection:
        connection = self._connections.get(session_id)
        if connection is None:
            raise RealtimeSessionNotFoundError(
                f"Active realtime connection not found: {session_id}"
            )
        return connection

    async def _emit(
        self,
        session: RealtimeSessionRecord,
        event_type: RealtimeEventType,
        payload: dict[str, Any] | None = None,
    ) -> None:
        event_payload: dict[str, Any] = {"status": session.status.value}
        if payload:
            event_payload.update(payload)
        await self._events.append(
            RealtimeRuntimeEvent(
                type=event_type,
                session_id=session.id,
                conversation_id=session.conversation_id,
                tenant_id=session.tenant_id,
                user_id=session.user_id,
                payload=event_payload,
            )
        )

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from kaiwen_agent.realtime.config import RealtimeSessionConfig
from kaiwen_agent.realtime.events import (
    RealtimeProviderEvent,
    RealtimeRuntimeEvent,
    redact_realtime_event,
)


class RealtimeConnection(Protocol):
    @property
    def provider_session_id(self) -> str | None: ...

    async def send_text(self, text: str) -> None: ...

    async def send_audio(self, audio: bytes) -> None: ...

    async def interrupt(self) -> None: ...

    async def close(self) -> None: ...

    def events(self) -> AsyncIterator[RealtimeProviderEvent]: ...


class RealtimeProvider(Protocol):
    @property
    def name(self) -> str: ...

    async def create_session(
        self,
        config: RealtimeSessionConfig,
    ) -> RealtimeConnection: ...


class RealtimeEventSink(Protocol):
    async def append(self, event: RealtimeRuntimeEvent) -> None: ...


class InMemoryRealtimeEventSink:
    def __init__(self) -> None:
        self.events: list[RealtimeRuntimeEvent] = []

    async def append(self, event: RealtimeRuntimeEvent) -> None:
        self.events.append(redact_realtime_event(event))

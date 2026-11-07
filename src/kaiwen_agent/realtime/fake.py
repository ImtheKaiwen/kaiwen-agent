from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

from kaiwen_agent.realtime.base import RealtimeConnection
from kaiwen_agent.realtime.config import RealtimeSessionConfig
from kaiwen_agent.realtime.events import RealtimeProviderEvent

_CLOSED = object()


class FakeRealtimeConnection:
    def __init__(self) -> None:
        self._provider_session_id = f"fake_rt_{uuid4().hex}"
        self._events: asyncio.Queue[RealtimeProviderEvent | object] = asyncio.Queue()
        self.sent_text: list[str] = []
        self.sent_audio: list[bytes] = []
        self.interrupt_count = 0
        self.closed = False

    @property
    def provider_session_id(self) -> str:
        return self._provider_session_id

    async def send_text(self, text: str) -> None:
        self._ensure_open()
        self.sent_text.append(text)

    async def send_audio(self, audio: bytes) -> None:
        self._ensure_open()
        self.sent_audio.append(audio)

    async def interrupt(self) -> None:
        self._ensure_open()
        self.interrupt_count += 1

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self._events.put(_CLOSED)

    async def emit(self, event: RealtimeProviderEvent) -> None:
        self._ensure_open()
        await self._events.put(event.model_copy(deep=True))

    async def events(self) -> AsyncIterator[RealtimeProviderEvent]:
        while True:
            event = await self._events.get()
            if event is _CLOSED:
                return
            if isinstance(event, RealtimeProviderEvent):
                yield event

    def _ensure_open(self) -> None:
        if self.closed:
            raise RuntimeError("Realtime connection is closed")


class FakeRealtimeProvider:
    name = "fake"

    def __init__(self, *, fail_creation: bool = False) -> None:
        self.fail_creation = fail_creation
        self.connections: list[FakeRealtimeConnection] = []
        self.configs: list[RealtimeSessionConfig] = []

    async def create_session(
        self,
        config: RealtimeSessionConfig,
    ) -> RealtimeConnection:
        if self.fail_creation:
            raise RuntimeError("Fake realtime provider creation failed")
        connection = FakeRealtimeConnection()
        self.connections.append(connection)
        self.configs.append(config.model_copy(deep=True))
        return connection

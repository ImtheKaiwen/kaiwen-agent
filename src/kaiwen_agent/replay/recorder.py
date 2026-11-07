from __future__ import annotations

import asyncio

from kaiwen_agent.audit import SecretRedactor
from kaiwen_agent.events import AgentEvent, EventSink
from kaiwen_agent.replay.models import ReplayFrame
from kaiwen_agent.replay.store import ReplayStore


class ReplayRecorder(EventSink):
    """Records canonical events and optionally forwards them to another sink."""

    def __init__(
        self,
        *,
        store: ReplayStore,
        downstream: EventSink | None = None,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self.store = store
        self.downstream = downstream
        self.redactor = redactor or SecretRedactor()
        self._sequences: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def append(self, event: AgentEvent) -> None:
        clean_payload = self.redactor.redact(event.payload)
        if not isinstance(clean_payload, dict):  # pragma: no cover - dict input guarantees this
            raise TypeError("Redacted event payload must remain a mapping")
        clean_event = event.model_copy(update={"payload": clean_payload})
        async with self._lock:
            if event.trace_id not in self._sequences:
                self._sequences[event.trace_id] = len(await self.store.list_frames(event.trace_id))
            sequence = self._sequences[event.trace_id]
            appended = await self.store.append(
                event.trace_id,
                ReplayFrame(sequence=sequence, event=clean_event),
            )
            if appended:
                self._sequences[event.trace_id] = sequence + 1
        if self.downstream is not None:
            await self.downstream.append(event)

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncIterator

from kaiwen_agent.events import AgentEvent, EventSink, redact_agent_event


def encode_sse(event: AgentEvent) -> str:
    event = redact_agent_event(event)
    data = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.id}\nevent: {event.type}\ndata: {data}\n\n"


class SSEEventBroker:
    """Fan-out event sink with framework-neutral SSE streams."""

    def __init__(self, sink: EventSink | None = None, *, queue_size: int = 100) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be at least 1")
        self.sink = sink
        self.queue_size = queue_size
        self._subscribers: dict[str, set[asyncio.Queue[AgentEvent]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def append(self, event: AgentEvent) -> None:
        event = redact_agent_event(event)
        if self.sink is not None:
            await self.sink.append(event)
        async with self._lock:
            subscribers = tuple(self._subscribers.get(event.session_id, ()))
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                _ = queue.get_nowait()
                queue.put_nowait(event)

    async def stream(self, session_id: str) -> AsyncIterator[str]:
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=self.queue_size)
        async with self._lock:
            self._subscribers[session_id].add(queue)
        try:
            while True:
                yield encode_sse(await queue.get())
        finally:
            async with self._lock:
                subscribers = self._subscribers.get(session_id)
                if subscribers is not None:
                    subscribers.discard(queue)
                    if not subscribers:
                        self._subscribers.pop(session_id, None)

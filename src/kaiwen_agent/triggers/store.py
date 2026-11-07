from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Protocol

from kaiwen_agent.triggers.models import TriggerBatch


class TriggerStore(Protocol):
    async def mark_event_once(
        self, event_id: str, *, expires_at: datetime, now: datetime
    ) -> bool: ...

    async def get_batch(self, key: str) -> TriggerBatch | None: ...

    async def save_batch(self, batch: TriggerBatch) -> None: ...

    async def claim_due(self, *, due_before: datetime, limit: int = 100) -> list[TriggerBatch]: ...


class InMemoryTriggerStore:
    def __init__(self) -> None:
        self._seen: dict[str, datetime] = {}
        self._batches: dict[str, TriggerBatch] = {}
        self._lock = asyncio.Lock()

    async def mark_event_once(self, event_id: str, *, expires_at: datetime, now: datetime) -> bool:
        async with self._lock:
            self._seen = {key: expiry for key, expiry in self._seen.items() if expiry > now}
            if event_id in self._seen:
                return False
            self._seen[event_id] = expires_at
            return True

    async def get_batch(self, key: str) -> TriggerBatch | None:
        async with self._lock:
            batch = self._batches.get(key)
            return batch.model_copy(deep=True) if batch else None

    async def save_batch(self, batch: TriggerBatch) -> None:
        async with self._lock:
            self._batches[batch.key] = batch.model_copy(deep=True)

    async def claim_due(self, *, due_before: datetime, limit: int = 100) -> list[TriggerBatch]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        async with self._lock:
            due = [batch for batch in self._batches.values() if batch.due_at <= due_before]
            due.sort(key=lambda batch: (-batch.priority, batch.due_at))
            claimed = due[:limit]
            for batch in claimed:
                self._batches.pop(batch.key, None)
            return [batch.model_copy(deep=True) for batch in claimed]

from __future__ import annotations

import asyncio
from typing import Protocol

from kaiwen_agent.dead_letter.models import DeadLetterRecord, DeadLetterStatus


class DeadLetterStore(Protocol):
    async def save(self, record: DeadLetterRecord) -> None: ...

    async def get(self, record_id: str) -> DeadLetterRecord | None: ...

    async def list(
        self,
        *,
        tenant_id: str,
        owner_user_id: str | None,
        status: DeadLetterStatus | None,
        limit: int,
    ) -> list[DeadLetterRecord]: ...


class InMemoryDeadLetterStore:
    def __init__(self) -> None:
        self._records: dict[str, DeadLetterRecord] = {}
        self._lock = asyncio.Lock()

    async def save(self, record: DeadLetterRecord) -> None:
        async with self._lock:
            self._records[record.id] = record.model_copy(deep=True)

    async def get(self, record_id: str) -> DeadLetterRecord | None:
        async with self._lock:
            record = self._records.get(record_id)
            return record.model_copy(deep=True) if record is not None else None

    async def list(
        self,
        *,
        tenant_id: str,
        owner_user_id: str | None,
        status: DeadLetterStatus | None,
        limit: int,
    ) -> list[DeadLetterRecord]:
        async with self._lock:
            records = [
                record
                for record in self._records.values()
                if record.tenant_id == tenant_id
                and (owner_user_id is None or record.owner_user_id == owner_user_id)
                and (status is None or record.status == status)
            ]
            records.sort(key=lambda item: item.created_at, reverse=True)
            return [record.model_copy(deep=True) for record in records[:limit]]

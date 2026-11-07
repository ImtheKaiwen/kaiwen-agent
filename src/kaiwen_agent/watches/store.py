from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Protocol

from kaiwen_agent.watches.models import Watch, WatchStatus, WatchStrategy


class WatchStore(Protocol):
    async def save_watch(self, watch: Watch) -> None: ...

    async def get_watch(self, watch_id: str) -> Watch | None: ...

    async def list_watches(
        self,
        *,
        tenant_id: str,
        user_id: str | None = None,
        resource: str | None = None,
        strategy: WatchStrategy | None = None,
        status: WatchStatus | None = None,
        due_before: datetime | None = None,
        limit: int = 100,
    ) -> list[Watch]: ...


class InMemoryWatchStore:
    def __init__(self) -> None:
        self._watches: dict[str, Watch] = {}
        self._lock = asyncio.Lock()

    async def save_watch(self, watch: Watch) -> None:
        async with self._lock:
            self._watches[watch.id] = watch.model_copy(deep=True)

    async def get_watch(self, watch_id: str) -> Watch | None:
        async with self._lock:
            watch = self._watches.get(watch_id)
            return watch.model_copy(deep=True) if watch else None

    async def list_watches(
        self,
        *,
        tenant_id: str,
        user_id: str | None = None,
        resource: str | None = None,
        strategy: WatchStrategy | None = None,
        status: WatchStatus | None = None,
        due_before: datetime | None = None,
        limit: int = 100,
    ) -> list[Watch]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        async with self._lock:
            items = [
                watch
                for watch in self._watches.values()
                if watch.tenant_id == tenant_id
                and (user_id is None or watch.user_id == user_id)
                and (resource is None or watch.resource == resource)
                and (strategy is None or watch.strategy == strategy)
                and (status is None or watch.status == status)
                and (
                    due_before is None
                    or watch.next_check_at is None
                    or watch.next_check_at <= due_before
                )
            ]
            items.sort(key=lambda item: item.updated_at)
            return [item.model_copy(deep=True) for item in items[:limit]]

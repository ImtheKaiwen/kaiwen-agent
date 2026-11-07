from __future__ import annotations

import asyncio
from typing import Protocol

from kaiwen_agent.attention.models import AttentionItem


class AttentionStore(Protocol):
    async def save_attention(self, item: AttentionItem) -> None: ...

    async def list_attention(
        self, *, tenant_id: str, user_id: str, limit: int = 100
    ) -> list[AttentionItem]: ...


class InMemoryAttentionStore:
    def __init__(self) -> None:
        self._items: dict[str, AttentionItem] = {}
        self._lock = asyncio.Lock()

    async def save_attention(self, item: AttentionItem) -> None:
        async with self._lock:
            self._items[item.id] = item.model_copy(deep=True)

    async def list_attention(
        self, *, tenant_id: str, user_id: str, limit: int = 100
    ) -> list[AttentionItem]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        async with self._lock:
            items = [
                item
                for item in self._items.values()
                if item.tenant_id == tenant_id and item.user_id == user_id
            ]
            items.sort(key=lambda item: item.created_at, reverse=True)
            return [item.model_copy(deep=True) for item in items[:limit]]

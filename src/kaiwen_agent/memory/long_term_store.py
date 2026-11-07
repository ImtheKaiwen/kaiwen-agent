from __future__ import annotations

import asyncio
import math
import re
from typing import Protocol

from kaiwen_agent.memory.models import (
    MemoryItem,
    MemorySearchQuery,
    MemorySearchResult,
    MemoryStatus,
)
from kaiwen_agent.types import utc_now


class LongTermMemoryStore(Protocol):
    async def save_memory(self, item: MemoryItem) -> MemoryItem: ...

    async def get_memory(
        self, *, tenant_id: str, user_id: str, memory_id: str
    ) -> MemoryItem | None: ...

    async def find_active_by_key(
        self, *, tenant_id: str, user_id: str, namespace: str, subject: str, key: str
    ) -> MemoryItem | None: ...

    async def search_memories(
        self, query: MemorySearchQuery, *, embedding: list[float] | None
    ) -> list[MemorySearchResult]: ...

    async def list_memories(
        self,
        *,
        tenant_id: str,
        user_id: str,
        status: MemoryStatus | None = None,
        namespace: str | None = None,
        limit: int = 100,
    ) -> list[MemoryItem]: ...

    async def update_memory(self, item: MemoryItem) -> MemoryItem: ...

    async def forget_memory(self, *, tenant_id: str, user_id: str, memory_id: str) -> bool: ...


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[\w-]+", value.casefold()))


def _cosine(left: list[float] | None, right: list[float] | None) -> float:
    if left is None or right is None or len(left) != len(right) or not left:
        return 0
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(
        sum(value * value for value in right)
    )
    if denominator == 0:
        return 0
    dot_product = sum(a * b for a, b in zip(left, right, strict=True))
    return max(0, min(1, dot_product / denominator))


class InMemoryLongTermMemoryStore:
    def __init__(self) -> None:
        self._items: dict[str, MemoryItem] = {}
        self._lock = asyncio.Lock()

    async def save_memory(self, item: MemoryItem) -> MemoryItem:
        async with self._lock:
            if item.id in self._items:
                raise ValueError(f"Memory already exists: {item.id}")
            active = next(
                (
                    existing
                    for existing in self._items.values()
                    if existing.tenant_id == item.tenant_id
                    and existing.user_id == item.user_id
                    and existing.namespace == item.namespace
                    and existing.subject == item.subject
                    and existing.key == item.key
                    and existing.status == MemoryStatus.ACTIVE
                ),
                None,
            )
            if active is not None and item.status == MemoryStatus.ACTIVE:
                if active.content == item.content:
                    return active.model_copy(deep=True)
                if item.supersedes_id not in {None, active.id}:
                    raise ValueError("Concurrent memory conflict requires a fresh admission")
                item = item.model_copy(update={"supersedes_id": active.id})
            if item.supersedes_id is not None and item.status == MemoryStatus.ACTIVE:
                previous = self._items.get(item.supersedes_id)
                if previous is None or not self._same_owner(previous, item):
                    raise ValueError("Superseded memory is outside the trusted owner scope")
                self._items[previous.id] = previous.model_copy(
                    update={"status": MemoryStatus.SUPERSEDED, "updated_at": utc_now()}
                )
            self._items[item.id] = item.model_copy(deep=True)
            return item.model_copy(deep=True)

    async def get_memory(
        self, *, tenant_id: str, user_id: str, memory_id: str
    ) -> MemoryItem | None:
        async with self._lock:
            item = self._items.get(memory_id)
            if item is None or item.tenant_id != tenant_id or item.user_id != user_id:
                return None
            return item.model_copy(deep=True)

    async def find_active_by_key(
        self, *, tenant_id: str, user_id: str, namespace: str, subject: str, key: str
    ) -> MemoryItem | None:
        async with self._lock:
            for item in self._items.values():
                if (
                    item.tenant_id == tenant_id
                    and item.user_id == user_id
                    and item.namespace == namespace
                    and item.subject == subject
                    and item.key == key
                    and item.status == MemoryStatus.ACTIVE
                ):
                    return item.model_copy(deep=True)
            return None

    async def search_memories(
        self, query: MemorySearchQuery, *, embedding: list[float] | None
    ) -> list[MemorySearchResult]:
        query_tokens = _tokens(query.text)
        now = utc_now()
        async with self._lock:
            ranked: list[MemorySearchResult] = []
            for item in self._items.values():
                if (
                    item.tenant_id != query.tenant_id
                    or item.user_id != query.user_id
                    or item.namespace not in query.namespaces
                    or item.status != MemoryStatus.ACTIVE
                    or (item.expires_at is not None and item.expires_at <= now)
                ):
                    continue
                item_tokens = _tokens(f"{item.subject} {item.key} {item.content}")
                lexical = len(query_tokens & item_tokens) / max(1, len(query_tokens))
                semantic = _cosine(embedding, item.embedding)
                score = min(
                    1,
                    lexical * 0.35
                    + semantic * 0.4
                    + item.importance * 0.15
                    + item.confidence * 0.1,
                )
                ranked.append(
                    MemorySearchResult(
                        item=item.model_copy(deep=True),
                        score=score,
                        lexical_score=lexical,
                        semantic_score=semantic,
                    )
                )
            ranked.sort(key=lambda result: result.score, reverse=True)
            selected = ranked[: query.top_k]
            for result in selected:
                stored = self._items[result.item.id]
                self._items[stored.id] = stored.model_copy(update={"last_accessed_at": now})
            return selected

    async def list_memories(
        self,
        *,
        tenant_id: str,
        user_id: str,
        status: MemoryStatus | None = None,
        namespace: str | None = None,
        limit: int = 100,
    ) -> list[MemoryItem]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        async with self._lock:
            items = [
                item
                for item in self._items.values()
                if item.tenant_id == tenant_id
                and item.user_id == user_id
                and (status is None or item.status == status)
                and (namespace is None or item.namespace == namespace)
            ]
            items.sort(key=lambda item: item.updated_at, reverse=True)
            return [item.model_copy(deep=True) for item in items[:limit]]

    async def update_memory(self, item: MemoryItem) -> MemoryItem:
        async with self._lock:
            existing = self._items.get(item.id)
            if existing is None or not self._same_owner(existing, item):
                raise KeyError("Memory not found in trusted owner scope")
            if item.supersedes_id is not None and item.status == MemoryStatus.ACTIVE:
                previous = self._items.get(item.supersedes_id)
                if previous is None or not self._same_owner(previous, item):
                    raise ValueError("Superseded memory is outside the trusted owner scope")
                self._items[previous.id] = previous.model_copy(
                    update={"status": MemoryStatus.SUPERSEDED, "updated_at": utc_now()}
                )
            self._items[item.id] = item.model_copy(deep=True)
            return item.model_copy(deep=True)

    async def forget_memory(self, *, tenant_id: str, user_id: str, memory_id: str) -> bool:
        async with self._lock:
            item = self._items.get(memory_id)
            if item is None or item.tenant_id != tenant_id or item.user_id != user_id:
                return False
            self._items[memory_id] = item.model_copy(
                update={
                    "status": MemoryStatus.DELETED,
                    "content": "[forgotten]",
                    "structured_data": {},
                    "embedding": None,
                    "updated_at": utc_now(),
                }
            )
            return True

    @staticmethod
    def _same_owner(left: MemoryItem, right: MemoryItem) -> bool:
        return left.tenant_id == right.tenant_id and left.user_id == right.user_id

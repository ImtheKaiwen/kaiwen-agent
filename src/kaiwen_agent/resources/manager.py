from __future__ import annotations

import asyncio
from collections import Counter


class ResourceManager:
    def __init__(self, capacities: dict[str, int] | None = None) -> None:
        self._capacities = dict(capacities or {})
        if any(capacity < 1 for capacity in self._capacities.values()):
            raise ValueError("Resource capacities must be at least 1")
        self._usage: Counter[str] = Counter()
        self._owners: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    async def try_acquire(self, resource_keys: list[str], owner_id: str) -> bool:
        keys = sorted(set(resource_keys))
        async with self._lock:
            if owner_id in self._owners:
                raise ValueError(f"Owner already holds resources: {owner_id}")
            if any(self._usage[key] >= self._capacities.get(key, 1) for key in keys):
                return False
            for key in keys:
                self._usage[key] += 1
            self._owners[owner_id] = set(keys)
            return True

    async def release(self, owner_id: str) -> None:
        async with self._lock:
            keys = self._owners.pop(owner_id, set())
            for key in keys:
                self._usage[key] -= 1
                if self._usage[key] <= 0:
                    del self._usage[key]

    async def available(self, resource_keys: list[str]) -> bool:
        keys = set(resource_keys)
        async with self._lock:
            return all(self._usage[key] < self._capacities.get(key, 1) for key in keys)

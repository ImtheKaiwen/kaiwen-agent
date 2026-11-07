from __future__ import annotations

from typing import Protocol

from kaiwen_agent.types import ToolResult


class IdempotencyStore(Protocol):
    async def get(self, key: str) -> ToolResult | None: ...

    async def put(self, key: str, result: ToolResult) -> None: ...


class InMemoryIdempotencyStore:
    def __init__(self) -> None:
        self._results: dict[str, ToolResult] = {}

    async def get(self, key: str) -> ToolResult | None:
        result = self._results.get(key)
        return result.model_copy(deep=True) if result else None

    async def put(self, key: str, result: ToolResult) -> None:
        self._results[key] = result.model_copy(deep=True)

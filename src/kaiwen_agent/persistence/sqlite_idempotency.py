from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from kaiwen_agent.types import ToolResult, utc_now


class SQLiteIdempotencyStore:
    """Durable tool-result receipts using the existing IdempotencyStore contract."""

    is_durable = True

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(self._create_schema)
            self._initialized = True

    async def get(self, key: str) -> ToolResult | None:
        await self.initialize()

        def operation() -> ToolResult | None:
            with sqlite3.connect(self.path, timeout=30) as connection:
                row = connection.execute(
                    "SELECT result_json FROM tool_idempotency_receipts WHERE key = ?", (key,)
                ).fetchone()
                return ToolResult.model_validate_json(row[0]) if row else None

        return await asyncio.to_thread(operation)

    async def put(self, key: str, result: ToolResult) -> None:
        await self.initialize()

        def operation() -> None:
            with sqlite3.connect(self.path, timeout=30) as connection:
                connection.execute(
                    """INSERT OR IGNORE INTO tool_idempotency_receipts
                    (key, result_json, created_at) VALUES (?, ?, ?)""",
                    (key, result.model_dump_json(), utc_now().isoformat()),
                )

        await asyncio.to_thread(operation)

    def _create_schema(self) -> None:
        with sqlite3.connect(self.path, timeout=30) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS tool_idempotency_receipts (
                    key TEXT PRIMARY KEY, result_json TEXT NOT NULL, created_at TEXT NOT NULL
                )"""
            )

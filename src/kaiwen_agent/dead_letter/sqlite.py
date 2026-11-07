from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from kaiwen_agent.dead_letter.models import DeadLetterRecord, DeadLetterStatus

T = TypeVar("T")


class SQLiteDeadLetterStore:
    """Restart-safe local DLQ reference persistence."""

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
            await self._run(self._create_schema, initialize=False)
            self._initialized = True

    async def save(self, record: DeadLetterRecord) -> None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """INSERT INTO dead_letters
                (id, tenant_id, owner_user_id, status, created_at, value_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                    value_json=excluded.value_json""",
                (
                    record.id,
                    record.tenant_id,
                    record.owner_user_id,
                    record.status,
                    record.created_at.isoformat(),
                    record.model_dump_json(),
                ),
            )

        await self._run(operation)

    async def get(self, record_id: str) -> DeadLetterRecord | None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> DeadLetterRecord | None:
            row = connection.execute(
                "SELECT value_json FROM dead_letters WHERE id=?", (record_id,)
            ).fetchone()
            return DeadLetterRecord.model_validate_json(row["value_json"]) if row else None

        return await self._run(operation)

    async def list(
        self,
        *,
        tenant_id: str,
        owner_user_id: str | None,
        status: DeadLetterStatus | None,
        limit: int,
    ) -> list[DeadLetterRecord]:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> list[DeadLetterRecord]:
            clauses = ["tenant_id=?"]
            params: list[str | int] = [tenant_id]
            if owner_user_id is not None:
                clauses.append("owner_user_id=?")
                params.append(owner_user_id)
            if status is not None:
                clauses.append("status=?")
                params.append(status)
            params.append(limit)
            rows = connection.execute(
                f"""SELECT value_json FROM dead_letters
                WHERE {" AND ".join(clauses)}
                ORDER BY created_at DESC LIMIT ?""",
                params,
            ).fetchall()
            return [DeadLetterRecord.model_validate_json(row["value_json"]) for row in rows]

        return await self._run(operation)

    async def _run(
        self, operation: Callable[[sqlite3.Connection], T], *, initialize: bool = True
    ) -> T:
        if initialize:
            await self.initialize()

        def execute() -> T:
            connection = sqlite3.connect(self.path)
            connection.row_factory = sqlite3.Row
            try:
                result = operation(connection)
                connection.commit()
                return result
            finally:
                connection.close()

        return await asyncio.to_thread(execute)

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS dead_letters (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                owner_user_id TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                value_json TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS idx_dead_letters_scope
            ON dead_letters(tenant_id, owner_user_id, status, created_at DESC)"""
        )

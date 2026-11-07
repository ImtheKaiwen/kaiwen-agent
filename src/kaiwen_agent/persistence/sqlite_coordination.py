from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TypeVar

from kaiwen_agent.attention import AttentionItem
from kaiwen_agent.objectives import Objective, ObjectiveStatus
from kaiwen_agent.watches import Watch, WatchStatus, WatchStrategy

T = TypeVar("T")


class SQLiteCoordinationStore:
    """SQLite reference persistence for objectives, watches, and attention items."""

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

    async def save_objective(self, objective: Objective) -> None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """INSERT INTO objectives
                    (id, tenant_id, user_id, status, priority, updated_at, value_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                    priority=excluded.priority, updated_at=excluded.updated_at,
                    value_json=excluded.value_json""",
                (
                    objective.id,
                    objective.tenant_id,
                    objective.user_id,
                    objective.status,
                    objective.priority,
                    objective.updated_at.isoformat(),
                    objective.model_dump_json(),
                ),
            )

        await self._run(operation)

    async def get_objective(self, objective_id: str) -> Objective | None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> Objective | None:
            row = connection.execute(
                "SELECT value_json FROM objectives WHERE id=?", (objective_id,)
            ).fetchone()
            return Objective.model_validate_json(row["value_json"]) if row else None

        return await self._run(operation)

    async def list_objectives(
        self,
        *,
        tenant_id: str,
        user_id: str,
        status: ObjectiveStatus | None = None,
        limit: int = 100,
    ) -> list[Objective]:
        _validate_limit(limit)
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> list[Objective]:
            params: list[str | int] = [tenant_id, user_id]
            status_clause = ""
            if status is not None:
                status_clause = " AND status=?"
                params.append(status)
            params.append(limit)
            rows = connection.execute(
                f"""SELECT value_json FROM objectives
                WHERE tenant_id=? AND user_id=?{status_clause}
                ORDER BY priority DESC, updated_at DESC LIMIT ?""",
                params,
            ).fetchall()
            return [Objective.model_validate_json(row["value_json"]) for row in rows]

        return await self._run(operation)

    async def save_watch(self, watch: Watch) -> None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """INSERT INTO watches
                    (id, tenant_id, user_id, resource, strategy, status,
                     next_check_at, expires_at, updated_at, value_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                    next_check_at=excluded.next_check_at,
                    updated_at=excluded.updated_at, value_json=excluded.value_json""",
                (
                    watch.id,
                    watch.tenant_id,
                    watch.user_id,
                    watch.resource,
                    watch.strategy,
                    watch.status,
                    watch.next_check_at.isoformat() if watch.next_check_at else None,
                    watch.expires_at.isoformat(),
                    watch.updated_at.isoformat(),
                    watch.model_dump_json(),
                ),
            )

        await self._run(operation)

    async def get_watch(self, watch_id: str) -> Watch | None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> Watch | None:
            row = connection.execute(
                "SELECT value_json FROM watches WHERE id=?", (watch_id,)
            ).fetchone()
            return Watch.model_validate_json(row["value_json"]) if row else None

        return await self._run(operation)

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
        _validate_limit(limit)
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> list[Watch]:
            clauses = ["tenant_id=?"]
            params: list[str | int] = [tenant_id]
            filters = {
                "user_id": user_id,
                "resource": resource,
                "strategy": strategy,
                "status": status,
            }
            for column, value in filters.items():
                if value is not None:
                    clauses.append(f"{column}=?")
                    params.append(value)
            if due_before is not None:
                if due_before.tzinfo is None:
                    raise ValueError("due_before must be timezone-aware")
                clauses.append("(next_check_at IS NULL OR next_check_at<=?)")
                params.append(due_before.isoformat())
            params.append(limit)
            rows = connection.execute(
                f"""SELECT value_json FROM watches
                WHERE {" AND ".join(clauses)}
                ORDER BY updated_at ASC LIMIT ?""",
                params,
            ).fetchall()
            return [Watch.model_validate_json(row["value_json"]) for row in rows]

        return await self._run(operation)

    async def save_attention(self, item: AttentionItem) -> None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """INSERT INTO attention_items
                    (id, tenant_id, user_id, created_at, value_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET value_json=excluded.value_json""",
                (
                    item.id,
                    item.tenant_id,
                    item.user_id,
                    item.created_at.isoformat(),
                    item.model_dump_json(),
                ),
            )

        await self._run(operation)

    async def list_attention(
        self, *, tenant_id: str, user_id: str, limit: int = 100
    ) -> list[AttentionItem]:
        _validate_limit(limit)
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> list[AttentionItem]:
            rows = connection.execute(
                """SELECT value_json FROM attention_items
                WHERE tenant_id=? AND user_id=?
                ORDER BY created_at DESC LIMIT ?""",
                (tenant_id, user_id, limit),
            ).fetchall()
            return [AttentionItem.model_validate_json(row["value_json"]) for row in rows]

        return await self._run(operation)

    async def _run(
        self, operation: Callable[[sqlite3.Connection], T], *, initialize: bool = True
    ) -> T:
        if initialize:
            await self.initialize()

        def execute() -> T:
            connection = sqlite3.connect(self.path, timeout=30)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                result = operation(connection)
                connection.commit()
                return result
            finally:
                connection.close()

        return await asyncio.to_thread(execute)

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS objectives (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                status TEXT NOT NULL, priority INTEGER NOT NULL,
                updated_at TEXT NOT NULL, value_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_objectives_owner_status
                ON objectives(tenant_id, user_id, status, priority DESC, updated_at DESC);
            CREATE TABLE IF NOT EXISTS watches (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                resource TEXT NOT NULL, strategy TEXT NOT NULL, status TEXT NOT NULL,
                next_check_at TEXT, expires_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, value_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_watches_match
                ON watches(tenant_id, resource, strategy, status);
            CREATE INDEX IF NOT EXISTS idx_watches_poll
                ON watches(tenant_id, strategy, status, next_check_at);
            CREATE TABLE IF NOT EXISTS attention_items (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                created_at TEXT NOT NULL, value_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_attention_owner_created
                ON attention_items(tenant_id, user_id, created_at DESC);
            """
        )


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")

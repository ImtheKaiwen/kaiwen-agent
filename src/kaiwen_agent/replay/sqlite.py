from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from kaiwen_agent.replay.models import ReplayFrame

T = TypeVar("T")


class SQLiteReplayStore:
    """Restart-safe reference event trace storage."""

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

    async def append(self, trace_id: str, frame: ReplayFrame) -> bool:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> bool:
            duplicate = connection.execute(
                "SELECT 1 FROM replay_frames WHERE event_id=?", (frame.event.id,)
            ).fetchone()
            if duplicate:
                return False
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM replay_frames WHERE trace_id=?",
                (trace_id,),
            ).fetchone()
            if row["count"] != frame.sequence:
                raise ValueError("Replay frame sequence must be contiguous")
            connection.execute(
                """INSERT INTO replay_frames
                (trace_id, sequence, event_id, value_json)
                VALUES (?, ?, ?, ?)""",
                (trace_id, frame.sequence, frame.event.id, frame.model_dump_json()),
            )
            return True

        return await self._run(operation)

    async def list_frames(self, trace_id: str) -> list[ReplayFrame]:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> list[ReplayFrame]:
            rows = connection.execute(
                """SELECT value_json FROM replay_frames
                WHERE trace_id=? ORDER BY sequence ASC""",
                (trace_id,),
            ).fetchall()
            return [ReplayFrame.model_validate_json(row["value_json"]) for row in rows]

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
            """CREATE TABLE IF NOT EXISTS replay_frames (
                trace_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                value_json TEXT NOT NULL,
                PRIMARY KEY (trace_id, sequence)
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_replay_trace ON replay_frames(trace_id, sequence)"
        )

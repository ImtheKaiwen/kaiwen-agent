from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from kaiwen_agent.budgets.models import BudgetUsage
from kaiwen_agent.budgets.store import (
    BudgetReservationRequest,
    BudgetReservationResult,
    exceeds_budget,
)

T = TypeVar("T")


class SQLiteBudgetStore:
    """Restart-safe local budget usage storage; not a distributed reservation store."""

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

    async def get_usage(self, key: str) -> BudgetUsage:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> BudgetUsage:
            row = connection.execute(
                "SELECT value_json FROM budget_usage WHERE scope_key=?", (key,)
            ).fetchone()
            return BudgetUsage.model_validate_json(row["value_json"]) if row else BudgetUsage()

        return await self._run(operation)

    async def save_usage(self, key: str, usage: BudgetUsage) -> None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """INSERT INTO budget_usage (scope_key, value_json)
                VALUES (?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET value_json=excluded.value_json""",
                (key, usage.model_dump_json()),
            )

        await self._run(operation)

    async def reserve(
        self,
        requests: tuple[BudgetReservationRequest, ...],
        delta: BudgetUsage,
    ) -> BudgetReservationResult:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> BudgetReservationResult:
            connection.execute("BEGIN IMMEDIATE")
            projected: dict[str, BudgetUsage] = {}
            for request in requests:
                if request.key in projected:
                    continue
                row = connection.execute(
                    "SELECT value_json FROM budget_usage WHERE scope_key=?",
                    (request.key,),
                ).fetchone()
                current = (
                    BudgetUsage.model_validate_json(row["value_json"]) if row else BudgetUsage()
                )
                projected[request.key] = current.plus(delta)
            exceeded = tuple(
                request.rule.id
                for request in requests
                if exceeds_budget(projected[request.key], request.rule)
            )
            if exceeded:
                return BudgetReservationResult(
                    reserved=False,
                    exceeded_rule_ids=exceeded,
                )
            for key, usage in projected.items():
                connection.execute(
                    """INSERT INTO budget_usage (scope_key, value_json)
                    VALUES (?, ?)
                    ON CONFLICT(scope_key) DO UPDATE SET value_json=excluded.value_json""",
                    (key, usage.model_dump_json()),
                )
            return BudgetReservationResult(reserved=True)

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
            """CREATE TABLE IF NOT EXISTS budget_usage (
                scope_key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL
            )"""
        )

"""Bounded, validated listing of the tasks of one durable run, for read-only consumers.

`scan_run_tasks` is the one place that walks a `TaskRunQuery` for a run. It is shared by the Lead
view and by workflow delegation admission (ADR 0015) so a contract-violating custom store is
rejected the same way everywhere and the loop is bounded in one reviewed place.

However the store behaves, at most `max_tasks` rows are examined and the scan makes at most
`max_tasks` store calls: every page that reports more rows must examine at least one, its cursor
must strictly advance, its tasks must be unique and strictly ascending after the cursor, and tasks
plus unreadable rows may not exceed the requested limit. Any violation raises
`RunTaskScanError("task_listing_invalid")`. A store failure raises
`RunTaskScanError("task_store_unavailable")`. Neither error carries the underlying message; the
class name of a store failure is exposed as `error_type` for sanitized logging only.

Tasks that do not carry exactly the run's owner, session and durable run id are dropped and
counted, never returned. Reading never writes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from kaiwen_agent.runtime import RuntimeRunRecord
from kaiwen_agent.tasks import MAX_TASK_RUN_PAGE, AgentTask, TaskRunPage, TaskRunQuery


class RunTaskScanError(Exception):
    """The run's tasks could not be listed completely. `code` is stable and non-sensitive."""

    def __init__(self, code: str, error_type: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.error_type = error_type


@dataclass(frozen=True, slots=True)
class RunTaskScan:
    """The tasks found, how many examined rows were dropped, and whether more rows exist."""

    tasks: tuple[AgentTask, ...]
    unreadable: int
    truncated: bool


async def scan_run_tasks(
    store: TaskRunQuery, run: RuntimeRunRecord, *, max_tasks: int
) -> RunTaskScan:
    """Up to `max_tasks` rows of `run` in id order, whatever the store does."""

    if max_tasks < 1:
        raise ValueError("max_tasks must be at least 1")
    collected: list[AgentTask] = []
    unreadable = 0
    examined = 0
    cursor: str | None = None
    for _ in range(max_tasks):  # each continuing page examines >= 1 row, so this suffices
        limit = min(MAX_TASK_RUN_PAGE, max_tasks - examined)
        after = cursor
        failure: str | None = None
        page: Any = None
        try:
            page = await store.list_run_tasks(
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                runtime_run_id=run.id,
                limit=limit,
                after_id=after,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - sanitized below
            failure = type(error).__name__
        if failure is not None:  # raised outside the handler: the original is not chained
            raise RunTaskScanError("task_store_unavailable", failure)
        tasks, dropped, cursor = valid_page(page, limit=limit, after=after)
        examined += len(tasks) + dropped
        unreadable += dropped
        for task in tasks:
            # The store already scoped the query; a custom store is not trusted blindly.
            if belongs_to_run(task, run):
                collected.append(task)
            else:
                unreadable += 1
        if cursor is None or examined >= max_tasks:
            break
    return RunTaskScan(tasks=tuple(collected), unreadable=unreadable, truncated=cursor is not None)


def valid_page(
    page: Any, *, limit: int, after: str | None
) -> tuple[list[AgentTask], int, str | None]:
    """A store page checked against the `TaskRunQuery` contract, or `task_listing_invalid`."""

    if not isinstance(page, TaskRunPage):
        raise RunTaskScanError("task_listing_invalid")
    tasks, dropped, cursor = page.tasks, page.unreadable, page.next_after_id
    if (
        not isinstance(tasks, (tuple, list))
        or not all(isinstance(task, AgentTask) for task in tasks)
        or type(dropped) is not int
        or dropped < 0
        or len(tasks) + dropped > limit
        or (cursor is not None and (not isinstance(cursor, str) or not cursor))
    ):
        raise RunTaskScanError("task_listing_invalid")
    previous = after
    for task in tasks:
        # Strictly ascending and strictly after the cursor: no repeats, no going backwards.
        if previous is not None and task.id <= previous:
            raise RunTaskScanError("task_listing_invalid")
        previous = task.id
    if cursor is not None:
        examined = len(tasks) + dropped
        behind_cursor = after is not None and cursor <= after
        behind_tasks = bool(tasks) and cursor < tasks[-1].id
        if examined == 0 or behind_cursor or behind_tasks:
            raise RunTaskScanError("task_listing_invalid")
    return list(tasks), dropped, cursor


def belongs_to_run(task: AgentTask, run: RuntimeRunRecord) -> bool:
    """Whether a task names exactly this run's owner, session and durable run id."""

    return (
        task.tenant_id == run.tenant_id
        and task.user_id == run.user_id
        and task.session_id == run.session_id
        and task.metadata.get("runtime_run_id") == run.id
        and task.trace_id != run.id
    )

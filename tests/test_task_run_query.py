"""M6.3.3-A: owner- and run-scoped task query, its verified ownership and SQLite migration.

The query must filter by tenant, user and durable run in SQL before any row is materialized, page
deterministically with a bounded keyset, and return a task only if a durable run confirms its
ownership. Historical rows that cannot be verified stay unavailable; ownership is never guessed
from model-authored data. Every wait is bounded so a regression fails instead of hanging.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from kaiwen_agent.persistence import SQLitePersistence
from kaiwen_agent.runtime import AutonomyLevel, DurableRunManager, RuntimeRunRecord
from kaiwen_agent.tasks import (
    MAX_TASK_RUN_PAGE,
    AgentTask,
    InMemoryTaskStore,
    TaskRunPage,
    TaskRunQuery,
)
from tests.test_agent_runtime_worker import owner, pins

TIMEOUT = 30


def run_async(factory: Any) -> Any:
    async def bounded() -> Any:
        return await asyncio.wait_for(factory(), timeout=TIMEOUT)

    return asyncio.run(bounded())


async def make_run(
    store: SQLitePersistence, *, tenant: str = "tenant-1", user: str = "user-1"
) -> RuntimeRunRecord:
    return await DurableRunManager(store).create(
        identity=owner(tenant=tenant, user=user),
        autonomy=AutonomyLevel.AUTONOMOUS,
        version_pins=pins(),
    )


def make_task(
    run_id: str,
    name: str,
    *,
    tenant: str | None = "tenant-1",
    user: str | None = "user-1",
    session: str = "session-1",
    trace: str = "trace-1",
    **fields: Any,
) -> AgentTask:
    return AgentTask(
        id=f"task_{name}",
        name=name,
        agent_id="worker",
        session_id=session,
        tenant_id=tenant,
        user_id=user,
        trace_id=trace,
        metadata={"runtime_run_id": run_id, "logical_task_id": name, "workflow_cycle": 1},
        **fields,
    )


async def listed(store: SQLitePersistence, run: RuntimeRunRecord, **options: Any) -> TaskRunPage:
    return await store.list_run_tasks(
        tenant_id=run.tenant_id, user_id=run.user_id, runtime_run_id=run.id, **options
    )


# ------------------------------------------------------------------ scoping and isolation


def test_the_query_returns_only_the_tasks_of_the_requested_owner_and_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "s.sqlite3")
        mine = await make_run(store)
        sibling = await make_run(store)  # same owner, another run
        other_user = await make_run(store, user="user-2")
        other_tenant = await make_run(store, tenant="tenant-2")
        await store.save_task(make_task(mine.id, "a"))
        await store.save_task(make_task(mine.id, "b"))
        await store.save_task(make_task(sibling.id, "sibling"))
        await store.save_task(make_task(other_user.id, "user2", user="user-2"))
        await store.save_task(make_task(other_tenant.id, "tenant2", tenant="tenant-2"))

        page = await listed(store, mine)
        assert [task.id for task in page.tasks] == ["task_a", "task_b"]
        assert page.next_after_id is None and page.unreadable == 0
        # Another owner naming my run id gets nothing, and so does my id under their identity.
        stranger = await store.list_run_tasks(
            tenant_id="tenant-1", user_id="user-2", runtime_run_id=mine.id
        )
        assert stranger.tasks == ()
        foreign_tenant = await store.list_run_tasks(
            tenant_id="tenant-2", user_id="user-1", runtime_run_id=mine.id
        )
        assert foreign_tenant.tasks == ()
        assert [t.id for t in (await listed(store, sibling)).tasks] == ["task_sibling"]

    run_async(scenario)


def test_filtering_happens_in_sql_before_any_row_is_materialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "s.sqlite3")
        mine = await make_run(store)
        foreign = await make_run(store, user="user-2")
        for index in range(12):
            await store.save_task(make_task(mine.id, f"mine{index:02d}"))
        for index in range(300):
            await store.save_task(make_task(foreign.id, f"theirs{index:03d}", user="user-2"))

        original = AgentTask.model_validate_json
        materialized: list[int] = []

        def counting(data: Any, *args: Any, **kwargs: Any) -> AgentTask:
            materialized.append(1)
            return original(data, *args, **kwargs)

        monkeypatch.setattr(AgentTask, "model_validate_json", staticmethod(counting))
        page = await listed(store, mine, limit=5)
        assert len(page.tasks) == 5 and page.next_after_id is not None
        # Only the requested page was materialized, none of the 300 foreign rows.
        assert len(materialized) == 5

    run_async(scenario)


def test_the_query_uses_the_owner_run_index(tmp_path: Path) -> None:
    store = SQLitePersistence(tmp_path / "s.sqlite3")
    run_async(store.initialize)
    with sqlite3.connect(store.path) as connection:
        plan = connection.execute(
            """EXPLAIN QUERY PLAN SELECT id, task_json FROM tasks
            WHERE tenant_id = ? AND user_id = ? AND runtime_run_id = ? AND id > ?
            ORDER BY id ASC LIMIT ?""",
            ("t", "u", "r", "", 10),
        ).fetchall()
    detail = " ".join(str(row[-1]) for row in plan)
    assert "idx_tasks_owner_run" in detail
    assert "SCAN" not in detail.replace("SCAN tasks USING", "")  # no full-table scan


# ------------------------------------------------------------------ pagination


def test_keyset_pagination_is_deterministic_complete_and_duplicate_free(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "s.sqlite3")
        run = await make_run(store)
        names = [f"t{index:03d}" for index in range(25)]
        for name in reversed(names):  # insertion order must not matter
            await store.save_task(make_task(run.id, name))

        async def walk(size: int) -> list[str]:
            ids: list[str] = []
            after: str | None = None
            while True:
                page = await listed(store, run, limit=size, after_id=after)
                ids.extend(task.id for task in page.tasks)
                after = page.next_after_id
                if after is None:
                    return ids

        expected = [f"task_{name}" for name in names]
        assert await walk(7) == expected == await walk(7)
        assert await walk(25) == expected and await walk(1) == expected

    run_async(scenario)


def test_a_task_saved_between_pages_neither_repeats_nor_skips_later_tasks(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "s.sqlite3")
        run = await make_run(store)
        for name in ("b", "d", "f"):
            await store.save_task(make_task(run.id, name))
        first = await listed(store, run, limit=2)
        assert [t.id for t in first.tasks] == ["task_b", "task_d"]
        await store.save_task(make_task(run.id, "a"))  # sorts before the cursor
        await store.save_task(make_task(run.id, "e"))  # sorts after it
        second = await listed(store, run, limit=2, after_id=first.next_after_id)
        assert [t.id for t in second.tasks] == ["task_e", "task_f"]

    run_async(scenario)


def test_page_size_and_scope_arguments_are_validated(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "s.sqlite3")
        run = await make_run(store)
        for bad in (0, -1, MAX_TASK_RUN_PAGE + 1):
            with pytest.raises(ValueError, match="limit"):
                await listed(store, run, limit=bad)
        await listed(store, run, limit=MAX_TASK_RUN_PAGE)  # the maximum itself is allowed
        for field in ("tenant_id", "user_id", "runtime_run_id"):
            scope = {"tenant_id": "t", "user_id": "u", "runtime_run_id": "r", field: ""}
            with pytest.raises(ValueError, match="required"):
                await store.list_run_tasks(**scope)
        with pytest.raises(ValueError, match="after_id"):
            await listed(store, run, after_id=5)  # type: ignore[arg-type]

    run_async(scenario)


# ------------------------------------------------------------------ verified ownership claim


def test_a_task_is_returned_only_if_a_durable_run_confirms_its_owner_claim(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "s.sqlite3")
        run = await make_run(store)
        good = make_task(run.id, "good")
        await store.save_task(good)
        unverifiable = {
            "norun": make_task("runtime_does_not_exist", "norun"),
            "otheruser": make_task(run.id, "otheruser", user="user-2"),
            "othertenant": make_task(run.id, "othertenant", tenant="tenant-2"),
            "othersession": make_task(run.id, "othersession", session="session-2"),
            "tracerun": make_task(run.id, "tracerun", trace=run.id),
            "noowner": make_task(run.id, "noowner", tenant=None, user=None),
            # Model-authored `input` never establishes ownership.
            "inputonly": AgentTask(
                id="task_inputonly",
                name="inputonly",
                agent_id="worker",
                session_id="session-1",
                tenant_id="tenant-1",
                user_id="user-1",
                trace_id="trace-1",
                input={"runtime_run_id": run.id, "tenant_id": "tenant-1", "user_id": "user-1"},
            ),
        }
        for task in unverifiable.values():
            await store.save_task(task)

        assert [t.id for t in (await listed(store, run)).tasks] == ["task_good"]
        # Existing behavior is untouched: every task is still stored and readable by id.
        for task in unverifiable.values():
            assert await store.get_task(task.id) == task
        assert len(await store.list_tasks()) == 1 + len(unverifiable)

    run_async(scenario)


def test_saving_again_after_the_run_exists_verifies_the_task(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "s.sqlite3")
        runs = DurableRunManager(store)
        early = make_task("runtime_future", "early")
        await store.save_task(early)  # the run does not exist yet: unavailable
        probe = await store.list_run_tasks(
            tenant_id="tenant-1", user_id="user-1", runtime_run_id="runtime_future"
        )
        assert probe.tasks == ()
        run = await runs.create(
            identity=owner(), autonomy=AutonomyLevel.AUTONOMOUS, version_pins=pins()
        )
        retagged = make_task(run.id, "early")
        await store.save_task(retagged)
        assert [t.id for t in (await listed(store, run)).tasks] == ["task_early"]

    run_async(scenario)


def test_a_row_whose_json_disagrees_with_its_columns_is_dropped_and_counted(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "s.sqlite3")
        run = await make_run(store)
        other = await make_run(store)
        await store.save_task(make_task(run.id, "ok"))
        await store.save_task(make_task(run.id, "tampered"))
        await store.save_task(make_task(run.id, "garbled"))
        with sqlite3.connect(store.path) as connection:
            forged = make_task(other.id, "tampered").model_dump_json()  # names another run
            connection.execute(
                "UPDATE tasks SET task_json = ? WHERE id = 'task_tampered'", (forged,)
            )
            connection.execute("UPDATE tasks SET task_json = '{not json' WHERE id = 'task_garbled'")
        page = await listed(store, run)
        assert [t.id for t in page.tasks] == ["task_ok"]
        assert page.unreadable == 2

    run_async(scenario)


# ------------------------------------------------------------------ backward compatibility


def test_existing_task_stores_are_unchanged_and_lack_the_capability() -> None:
    assert not isinstance(InMemoryTaskStore(), TaskRunQuery)

    class Legacy:
        async def save_task(self, task: AgentTask) -> None: ...
        async def get_task(self, task_id: str) -> AgentTask | None: ...
        async def list_tasks(self, *, status: Any = None) -> list[AgentTask]:
            return []

    assert not isinstance(Legacy(), TaskRunQuery)
    store = SQLitePersistence(Path("unused.sqlite3"))
    assert isinstance(store, TaskRunQuery)


# ------------------------------------------------------------------ migration of existing databases

LEGACY_TASKS = """CREATE TABLE tasks (
    id TEXT PRIMARY KEY, status TEXT NOT NULL, agent_id TEXT NOT NULL,
    parent_id TEXT, task_json TEXT NOT NULL, updated_at TEXT NOT NULL
)"""
LEGACY_RUNS = """CREATE TABLE runtime_runs (
    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
    status TEXT NOT NULL, run_json TEXT NOT NULL, updated_at TEXT NOT NULL
)"""


def legacy_database(path: Path, run: RuntimeRunRecord, rows: dict[str, str]) -> None:
    """A database written before ownership columns existed."""

    with sqlite3.connect(path) as connection:
        # Databases written by earlier versions were created by SQLitePersistence, so WAL.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(LEGACY_TASKS)
        connection.execute(LEGACY_RUNS)
        connection.execute(
            "INSERT INTO runtime_runs VALUES (?, ?, ?, ?, ?, ?)",
            (run.id, run.tenant_id, run.user_id, run.status, run.model_dump_json(), "now"),
        )
        connection.executemany(
            "INSERT INTO tasks (id, status, agent_id, parent_id, task_json, updated_at) "
            "VALUES (?, 'pending', 'worker', NULL, ?, 'now')",
            list(rows.items()),
        )


def make_legacy_run() -> RuntimeRunRecord:
    return RuntimeRunRecord(
        id="runtime_legacy",
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        autonomy=AutonomyLevel.AUTONOMOUS,
        version_pins=pins(),
    )


def legacy_rows(run: RuntimeRunRecord) -> dict[str, str]:
    bad_meta = make_task(run.id, "badmeta")
    bad_meta.metadata["runtime_run_id"] = 123
    return {
        "task_ok": make_task(run.id, "ok").model_dump_json(),
        "task_norun": make_task("runtime_missing", "norun").model_dump_json(),
        "task_otheruser": make_task(run.id, "otheruser", user="user-2").model_dump_json(),
        "task_othersession": make_task(run.id, "othersession", session="s2").model_dump_json(),
        "task_tracerun": make_task(run.id, "tracerun", trace=run.id).model_dump_json(),
        "task_badmeta": bad_meta.model_dump_json(),
        "task_malformed": "{not json",
        "task_idmismatch": make_task(run.id, "other").model_dump_json(),
    }


def columns(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}


def test_migration_verifies_verifiable_rows_and_leaves_the_rest_unavailable(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    run = make_legacy_run()
    legacy_database(path, run, legacy_rows(run))

    async def scenario() -> None:
        store = SQLitePersistence(path)
        await store.initialize()
        assert {"tenant_id", "user_id", "runtime_run_id"} <= columns(path)
        page = await listed(store, run)
        assert [t.id for t in page.tasks] == ["task_ok"]
        assert page.unreadable == 0
        # Nothing was lost or rewritten: the global listing still skips only the malformed row.
        assert await store.get_task("task_norun") is not None
        # A second process starting later changes nothing.
        again = SQLitePersistence(path)
        await again.initialize()
        assert [t.id for t in (await listed(again, run)).tasks] == ["task_ok"]

    run_async(scenario)
    with sqlite3.connect(path) as connection:
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(tasks)")}
        unverified_rows = connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE runtime_run_id IS NULL"
        ).fetchone()[0]
    assert "idx_tasks_owner_run" in indexes
    assert unverified_rows == len(legacy_rows(run)) - 1


def test_migration_backfills_across_batch_boundaries(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    run = make_legacy_run()
    rows = {
        f"task_{index:05d}": make_task(run.id, f"{index:05d}").model_dump_json()
        for index in range(1_100)
    }
    legacy_database(path, run, rows)

    async def scenario() -> None:
        store = SQLitePersistence(path)
        await store.initialize()
        seen = 0
        after: str | None = None
        while True:
            page = await listed(store, run, limit=MAX_TASK_RUN_PAGE, after_id=after)
            seen += len(page.tasks)
            after = page.next_after_id
            if after is None:
                break
        assert seen == 1_100

    run_async(scenario)


def test_a_failed_migration_rolls_back_and_the_next_start_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "legacy.sqlite3"
    run = make_legacy_run()
    legacy_database(path, run, legacy_rows(run))

    def explode(connection: sqlite3.Connection, task: AgentTask) -> None:
        raise RuntimeError("simulated crash during backfill")

    with monkeypatch.context() as patched:
        patched.setattr("kaiwen_agent.persistence.sqlite._verified_task_owner", explode)
        with pytest.raises(RuntimeError, match="simulated crash"):
            run_async(SQLitePersistence(path).initialize)
    # Atomic: the schema is the old one, so nothing is half migrated and unrecoverable.
    assert "runtime_run_id" not in columns(path)

    async def retry() -> None:
        store = SQLitePersistence(path)
        await store.initialize()
        assert [t.id for t in (await listed(store, run)).tasks] == ["task_ok"]

    run_async(retry)


def test_two_processes_migrating_the_same_database_both_succeed(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    run = make_legacy_run()
    legacy_database(path, run, legacy_rows(run))

    async def scenario() -> None:
        first, second = SQLitePersistence(path), SQLitePersistence(path)
        await asyncio.gather(first.initialize(), second.initialize())
        assert [t.id for t in (await listed(second, run)).tasks] == ["task_ok"]

    run_async(scenario)
    assert sorted(columns(path) & {"tenant_id", "user_id", "runtime_run_id"}) == [
        "runtime_run_id",
        "tenant_id",
        "user_id",
    ]

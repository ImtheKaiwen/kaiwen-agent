"""Package 2: transactional ownership enforcement and fail-closed configuration (ADR 0016).

Real SQLite, a real `SQLiteLeaseStore` and a deterministic clock. The central scenario is the one
that a check of the run record's stored token cannot catch: the lease is taken over (or simply
expires) while the run record still names the old owner.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kaiwen_agent.control import RunControl
from kaiwen_agent.coordination import (
    DistributedLease,
    InMemoryLeaseStore,
    OwnerEpoch,
    RedisLeaseStore,
    SQLiteLeaseStore,
)
from kaiwen_agent.exceptions import FencedConfigurationError, OwnershipLostError
from kaiwen_agent.persistence import SQLitePersistence
from kaiwen_agent.runtime import DurableRunManager, RuntimeRunStatus
from kaiwen_agent.tasks import AgentTask, TaskScheduler, TaskStatus
from kaiwen_agent.tasks.fenced import FencedTaskStore
from kaiwen_agent.workflow import FencedExecution, WorkflowCompiler, WorkflowController
from tests.test_agent_runtime_worker import Flow, _empty_gate, owner

TTL = 30.0


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FencedFlow(Flow):
    """`Flow` over one SQLite database that is also the lease authority, with a fake clock."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.clock = Clock()
        self.store.lease_clock = self.clock
        self.leases = SQLiteLeaseStore(self.store)
        self.fenced = FencedExecution(persistence=self.store, leases=self.leases)
        self.schedulers: list[TaskScheduler] = []

    def scheduler(self, graph: Any) -> TaskScheduler:
        scheduler = TaskScheduler(
            graph=graph,
            workers=self.workers,
            store=self.fenced.task_store,
            control=RunControl(checkpoints=self.store),
            max_concurrency=self.max_concurrency,
        )
        self.schedulers.append(scheduler)
        return scheduler

    def fenced_controller(self, **options: Any) -> WorkflowController:
        settings: dict[str, Any] = {
            "runs": self.runs,
            "compiler": WorkflowCompiler(
                workers=self.workers,
                worker_allowlist=frozenset(self.registered),
                effective_capabilities=frozenset({"deploy"}),
                resource_allowlist=frozenset({"repo:main"}),
            ),
            "scheduler_factory": self.scheduler,
            "completion_gate": _empty_gate(),
            "leases": self.leases,
            "lease_owner_id": "controller-1",
            "lease_ttl_seconds": TTL,
            "action_intents": self.store,
            "fenced": self.fenced,
        }
        settings.update(options)
        return WorkflowController(**settings)

    async def owned(self, run: Any, owner_id: str = "a") -> tuple[DistributedLease, OwnerEpoch]:
        """Acquire the run's lease, issue its epoch and start the run under it (as a controller)."""

        lease = await self.leases.acquire(
            key=f"runtime-run:{run.id}", owner_id=owner_id, ttl_seconds=TTL
        )
        assert lease is not None
        epoch = await self.issue(lease, run)
        await self.runs.start(run_id=run.id, identity=owner(), owner_epoch=epoch)
        return lease, epoch

    async def issue(self, lease: DistributedLease, run: Any) -> OwnerEpoch:
        return await self.leases.issue_owner_epoch(
            lease,
            run_id=run.id,
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            session_id=run.session_id,
        )

    async def status(self, run: Any) -> RuntimeRunStatus:
        record = await self.runs.get(run_id=run.id, identity=owner())
        assert record is not None
        return record.status

    async def stored_task(self, task_id: str) -> AgentTask | None:
        return await self.store.get_task(task_id)

    def task(self, run: Any, task_id: str = "task_1", **fields: Any) -> AgentTask:
        return AgentTask(
            id=task_id,
            name="demo",
            agent_id="w",
            session_id=run.session_id,
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            trace_id="trace-1",
            metadata={"runtime_run_id": run.id, "logical_task_id": task_id},
            **fields,
        )


def run_async(factory: Any) -> Any:
    async def bounded() -> Any:
        return await asyncio.wait_for(factory(), timeout=60)

    return asyncio.run(bounded())


async def refused(call: Any, code: str) -> None:
    with pytest.raises(OwnershipLostError) as caught:
        await call
    assert caught.value.code == code


# ------------------------------------------------------------------ protected task writes


def test_a_live_epoch_writes_tasks_and_the_epoch_is_published_on_the_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        run = await flow.new_run()
        _, epoch = await flow.owned(run)
        await flow.store.save_task_fenced(flow.task(run), epoch)
        stored = await flow.stored_task("task_1")
        assert stored is not None
        record = await flow.runs.get(run_id=run.id, identity=owner())
        assert record is not None and record.fenced is True
        assert record.owner_fencing_token == epoch.epoch == 1

    run_async(scenario)


def test_an_expired_lease_denies_the_write_even_if_no_successor_exists(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        run = await flow.new_run()
        _, epoch = await flow.owned(run)
        await flow.store.save_task_fenced(flow.task(run), epoch)
        flow.clock.advance(TTL + 1)  # nobody else acquired anything
        late = flow.task(run, status=TaskStatus.COMPLETED)
        await refused(flow.store.save_task_fenced(late, epoch), "lease_expired")
        stored = await flow.stored_task("task_1")
        assert stored is not None and stored.status is not TaskStatus.COMPLETED  # rolled back

    run_async(scenario)


def test_takeover_before_the_new_owner_updates_the_run_record_still_denies_the_old_owner(
    tmp_path: Path,
) -> None:
    """The race a stored-token comparison cannot see: lease generation 2 exists, record says 1."""

    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        run = await flow.new_run()
        _, old = await flow.owned(run)
        flow.clock.advance(TTL + 1)
        new_lease = await flow.leases.acquire(
            key=f"runtime-run:{run.id}", owner_id="b", ttl_seconds=TTL
        )
        assert new_lease is not None and new_lease.fencing_token == 2
        record = await flow.runs.get(run_id=run.id, identity=owner())
        assert record is not None and record.owner_fencing_token == 1  # not yet published
        await refused(flow.store.save_task_fenced(flow.task(run), old), "lease_superseded")
        assert await flow.stored_task("task_1") is None

    run_async(scenario)


def test_after_the_new_owner_commits_its_epoch_the_old_owner_cannot_write_anything(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        run = await flow.new_run()
        _, old = await flow.owned(run)
        flow.clock.advance(TTL + 1)
        # Recovery blocks the orphaned run, then the new owner starts it and publishes epoch 2.
        await flow.runs.block(run_id=run.id, identity=owner(), reason="owner_lost")
        _, new = await flow.owned(run, owner_id="b")
        assert new.epoch == 2
        await flow.store.save_task_fenced(flow.task(run), new)
        for call, code in (
            (flow.store.save_task_fenced(flow.task(run, "task_2"), old), "lease_superseded"),
            (
                flow.runs.complete(run_id=run.id, identity=owner(), owner_epoch=old),
                "lease_superseded",
            ),
            (
                flow.runs.block(run_id=run.id, identity=owner(), reason="x", owner_epoch=old),
                "lease_superseded",
            ),
            (
                flow.runs.fail(run_id=run.id, identity=owner(), reason="x", owner_epoch=old),
                "lease_superseded",
            ),
            (
                flow.runs.cancel(run_id=run.id, identity=owner(), owner_epoch=old),
                "lease_superseded",
            ),
        ):
            await refused(call, code)
        assert await flow.status(run) is RuntimeRunStatus.RUNNING  # the new owner's state stands
        assert await flow.stored_task("task_2") is None

    run_async(scenario)


def test_a_lower_epoch_can_never_replace_a_published_higher_one(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        run = await flow.new_run()
        record = await flow.runs.get(run_id=run.id, identity=owner())
        assert record is not None
        # A higher epoch (5) was already published for this run by a previous owner.
        await flow.store.save_runtime_run(
            record.model_copy(
                update={
                    "owner_fencing_token": 5,
                    "owner_lease_key": f"runtime-run:{run.id}",
                    "fenced": True,
                }
            )
        )
        lease = await flow.leases.acquire(
            key=f"runtime-run:{run.id}", owner_id="a", ttl_seconds=TTL
        )
        assert lease is not None and lease.fencing_token == 1  # a lease generation below 5
        epoch = await flow.issue(lease, run)
        await refused(
            flow.runs.start(run_id=run.id, identity=owner(), owner_epoch=epoch), "epoch_regressed"
        )
        assert await flow.status(run) is RuntimeRunStatus.CREATED

    run_async(scenario)


def test_the_run_must_be_running_and_the_epoch_published_for_a_task_write(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        run = await flow.new_run()
        lease = await flow.leases.acquire(
            key=f"runtime-run:{run.id}", owner_id="a", ttl_seconds=TTL
        )
        assert lease is not None
        epoch = await flow.issue(lease, run)
        # Holding the lease is not enough: the run is not RUNNING under this epoch yet.
        await refused(flow.store.save_task_fenced(flow.task(run), epoch), "run_not_running")
        await flow.runs.start(run_id=run.id, identity=owner(), owner_epoch=epoch)
        await flow.runs.complete(run_id=run.id, identity=owner(), owner_epoch=epoch)
        await refused(flow.store.save_task_fenced(flow.task(run), epoch), "run_not_running")

    run_async(scenario)


def test_a_live_lease_whose_epoch_was_never_published_cannot_write(tmp_path: Path) -> None:
    """Holding a valid, newer lease is not enough: the run's published owner must be this epoch."""

    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        run = await flow.new_run()
        await flow.owned(run)  # generation 1 started the run and published itself
        flow.clock.advance(TTL + 1)
        lease = await flow.leases.acquire(
            key=f"runtime-run:{run.id}", owner_id="b", ttl_seconds=TTL
        )
        assert lease is not None and lease.fencing_token == 2
        unpublished = await flow.issue(lease, run)  # live, but never wrote the run record
        await refused(
            flow.store.save_task_fenced(flow.task(run), unpublished), "epoch_not_published"
        )
        assert await flow.stored_task("task_1") is None

    run_async(scenario)


def test_an_epoch_is_bound_to_its_run_owner_session_database_and_credential(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        run = await flow.new_run()
        other = await flow.new_run()
        _, epoch = await flow.owned(run)
        # A task of another run cannot be written under this run's epoch.
        await refused(flow.store.save_task_fenced(flow.task(other), epoch), "task_not_in_run")
        # A task whose owner fields disagree with the epoch is refused.
        stranger = flow.task(run).model_copy(update={"tenant_id": "tenant-2"})
        await refused(flow.store.save_task_fenced(stranger, epoch), "task_not_in_run")
        # A forged epoch (right shape, wrong credential) is not authority.
        forged = OwnerEpoch(
            database_id=epoch.database_id,
            lease_key=epoch.lease_key,
            owner_id=epoch.owner_id,
            epoch=epoch.epoch,
            run_id=epoch.run_id,
            tenant_id=epoch.tenant_id,
            user_id=epoch.user_id,
            session_id=epoch.session_id,
            credential="guess",
        )
        await refused(flow.store.save_task_fenced(flow.task(run), forged), "lease_not_held")
        # An epoch from another database is refused.
        foreign = OwnerEpoch(
            database_id="db_other",
            lease_key=epoch.lease_key,
            owner_id=epoch.owner_id,
            epoch=epoch.epoch,
            run_id=epoch.run_id,
            tenant_id=epoch.tenant_id,
            user_id=epoch.user_id,
            session_id=epoch.session_id,
            credential=epoch.credential,
        )
        await refused(flow.store.save_task_fenced(flow.task(run), foreign), "database_mismatch")
        # An epoch for another run id cannot be built from this lease.
        with pytest.raises(FencedConfigurationError):
            await flow.leases.issue_owner_epoch(
                (await flow.leases.acquire(key="runtime-run:x", owner_id="a", ttl_seconds=TTL))  # type: ignore[arg-type]
                or _unreachable(),
                run_id=run.id,
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                session_id=run.session_id,
            )

    run_async(scenario)


def _unreachable() -> Any:
    raise AssertionError("lease should have been acquired")


def test_an_epoch_does_not_expose_its_credential() -> None:
    epoch = OwnerEpoch(
        database_id="db",
        lease_key="runtime-run:r",
        owner_id="o",
        epoch=1,
        run_id="r",
        tenant_id="t",
        user_id="u",
        session_id="s",
        credential="SECRET-CREDENTIAL",
    )
    assert "SECRET-CREDENTIAL" not in repr(epoch) and "SECRET-CREDENTIAL" not in str(epoch)


# ------------------------------------------------------------------ no unfenced path


def test_the_fenced_store_has_no_ordinary_save_fallback(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        run = await flow.new_run()
        with pytest.raises(OwnershipLostError) as caught:
            await flow.fenced.task_store.save_task(flow.task(run))  # no epoch bound
        assert caught.value.code == "no_epoch_bound"
        assert await flow.stored_task("task_1") is None

    run_async(scenario)


def test_a_fenced_run_cannot_be_started_without_an_epoch(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        run = await flow.new_run()
        await flow.owned(run)
        await flow.runs.block(run_id=run.id, identity=owner(), reason="owner_lost")
        # An unfenced executor (or a stale caller with a bare token) cannot continue it.
        await refused(
            flow.runs.start(run_id=run.id, identity=owner(), owner_lease_key="k", owner_fence=9),
            "fenced_run_requires_epoch",
        )
        await refused(flow.runs.start(run_id=run.id, identity=owner()), "fenced_run_requires_epoch")

    run_async(scenario)


def test_non_fenced_runs_and_existing_stores_are_unchanged(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow = Flow(tmp_path)
        run = await flow.new_run()
        record = await flow.runs.start(
            run_id=run.id, identity=owner(), owner_lease_key="k", owner_fence=1
        )
        assert record.fenced is False
        await flow.runs.complete(run_id=run.id, identity=owner(), owner_fence=1)
        assert flow.runs.supports_fenced_execution is True  # SQLite declares it; use is opt-in

    run_async(scenario)


def test_old_run_records_without_the_fenced_flag_load_as_unfenced(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow = Flow(tmp_path)
        run = await flow.new_run()
        with sqlite3.connect(flow.store.path) as connection:
            row = connection.execute("SELECT run_json FROM runtime_runs").fetchone()
            import json

            stripped = json.loads(row[0])
            stripped.pop("fenced", None)
            connection.execute("UPDATE runtime_runs SET run_json = ?", (json.dumps(stripped),))
        loaded = await flow.runs.get(run_id=run.id, identity=owner())
        assert loaded is not None and loaded.fenced is False

    run_async(scenario)


# ------------------------------------------------------------------ configuration fails closed


def test_unsupported_compositions_are_refused_at_construction(tmp_path: Path) -> None:
    flow = FencedFlow(tmp_path)
    other_file = SQLitePersistence(tmp_path / "other.sqlite3")
    same_file_other_adapter = SQLitePersistence(flow.store.path)
    with pytest.raises(FencedConfigurationError):  # Redis lease store
        FencedExecution(persistence=flow.store, leases=RedisLeaseStore(object()))  # type: ignore[arg-type]
    with pytest.raises(FencedConfigurationError):  # in-memory lease store
        FencedExecution(persistence=flow.store, leases=InMemoryLeaseStore())  # type: ignore[arg-type]
    with pytest.raises(FencedConfigurationError):  # lease database is a different file
        FencedExecution(persistence=other_file, leases=flow.leases)
    with pytest.raises(FencedConfigurationError):  # same path, second adapter, second clock
        FencedExecution(persistence=same_file_other_adapter, leases=flow.leases)
    with pytest.raises(FencedConfigurationError):  # custom task store with a matching method name

        class Custom:
            fenced_execution_contract = "kaiwen.fenced-execution.v1"

            async def save_task_fenced(self, task: Any, epoch: Any) -> None: ...

        FencedExecution(persistence=Custom(), leases=flow.leases)  # type: ignore[arg-type]
    with pytest.raises(FencedConfigurationError):
        FencedTaskStore(object())  # type: ignore[arg-type]


def test_the_controller_refuses_parts_that_are_not_the_fenced_authority(tmp_path: Path) -> None:
    flow = FencedFlow(tmp_path)
    elsewhere = SQLitePersistence(tmp_path / "elsewhere.sqlite3")
    with pytest.raises(FencedConfigurationError):  # not the fenced lease store
        flow.fenced_controller(leases=InMemoryLeaseStore())
    with pytest.raises(FencedConfigurationError):  # no lease store at all
        flow.fenced_controller(leases=None)
    with pytest.raises(FencedConfigurationError):  # runs in another database
        flow.fenced_controller(runs=DurableRunManager(elsewhere))
    with pytest.raises(FencedConfigurationError):  # action intents in another database
        flow.fenced_controller(action_intents=elsewhere)


def test_a_scheduler_over_an_unfenced_store_is_refused_before_it_writes(tmp_path: Path) -> None:
    from kaiwen_agent.workers import FunctionWorker
    from tests.test_agent_runtime_worker import plan, spec

    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        ran: list[str] = []

        async def work(task: AgentTask, context: Any) -> dict[str, Any]:
            ran.append(task.id)
            return {"ok": True}

        flow.workers.register("w", FunctionWorker(work))
        flow.registered.add("w")

        def unfenced(graph: Any) -> TaskScheduler:
            return TaskScheduler(graph=graph, workers=flow.workers, store=flow.store)

        run = await flow.new_run()
        controller = flow.fenced_controller(scheduler_factory=unfenced)
        with pytest.raises(FencedConfigurationError):
            await controller.execute(
                run=run,
                identity=owner(),
                plan=plan(spec("t", "w")),
                standards=__import__("kaiwen_agent.standards", fromlist=["x"]).StandardResolution(
                    pack_refs=(), requirements=(), prompt_refs=()
                ),
                protected_input_fingerprint="objective-1",
            )
        assert ran == [] and await flow.stored_task("any") is None  # nothing wrote or ran

    run_async(scenario)


TAKEOVER_CHILD = """
import asyncio, sys
from datetime import datetime
from kaiwen_agent.persistence import SQLitePersistence
from kaiwen_agent.coordination import SQLiteLeaseStore

async def main():
    persistence = SQLitePersistence(sys.argv[1])
    persistence.lease_clock = lambda: datetime.fromisoformat(sys.argv[3])
    lease = await SQLiteLeaseStore(persistence).acquire(
        key=sys.argv[2], owner_id="process-2", ttl_seconds=30
    )
    print("EPOCH", lease.fencing_token if lease else "none", flush=True)

asyncio.run(main())
"""


def test_a_takeover_by_another_process_on_the_same_file_denies_the_old_owner(
    tmp_path: Path,
) -> None:
    """The lease changes in a different process; this process's next write evaluates it."""

    import os
    import sys

    import kaiwen_agent

    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        run = await flow.new_run()
        _, epoch = await flow.owned(run)
        await flow.store.save_task_fenced(flow.task(run), epoch)
        later = (flow.clock.now + timedelta(seconds=TTL + 1)).isoformat()
        root = str(Path(kaiwen_agent.__file__).resolve().parent.parent)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            TAKEOVER_CHILD,
            str(flow.store.path),
            f"runtime-run:{run.id}",
            later,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": root},
        )
        out, err = await process.communicate()
        assert process.returncode == 0, err.decode()
        assert out.decode().strip() == "EPOCH 2"
        # Nothing was published on the run record by the other process; this write still fails.
        flow.clock.advance(TTL + 1)
        await refused(
            flow.store.save_task_fenced(flow.task(run, status=TaskStatus.COMPLETED), epoch),
            "lease_superseded",
        )
        stored = await flow.stored_task("task_1")
        assert stored is not None and stored.status is not TaskStatus.COMPLETED

    run_async(scenario)

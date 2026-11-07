"""Run ownership leases and stale RUNNING recovery (M5).

Observed before the change: a durable run that was RUNNING when its process died, with no
action in flight, stayed RUNNING forever. Age alone is not proof of process death, so
ownership is a lease with a heartbeat; recovery only blocks runs whose lease nobody holds,
never guesses for runs without a lease, and never resumes anything automatically.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from kaiwen_agent.artifacts import ArtifactManager
from kaiwen_agent.control import RunControl
from kaiwen_agent.coordination import DistributedLease, InMemoryLeaseStore
from kaiwen_agent.exceptions import RunOwnershipError
from kaiwen_agent.persistence import SQLitePersistence
from kaiwen_agent.quality import CompletionGate, VerifierRegistry
from kaiwen_agent.runtime import (
    DurableRunManager,
    DurableRuntimeStores,
    RuntimeRecoveryManager,
    RuntimeRunStatus,
)
from kaiwen_agent.tasks import TaskScheduler
from kaiwen_agent.workers import FunctionWorker, WorkerRegistry
from kaiwen_agent.workflow import (
    WorkflowCompiler,
    WorkflowController,
    WorkflowOutcomeStatus,
    WorkflowPlan,
    WorkflowTaskSpec,
)
from tests.test_workflow_runtime import _empty_standards, _identity, _runtime_run


def _plan() -> WorkflowPlan:
    return WorkflowPlan(
        objective="Owned",
        tasks=(WorkflowTaskSpec(id="work", name="Work", worker_id="worker"),),
    )


def _controller(
    store: SQLitePersistence,
    worker: Any,
    *,
    leases: Any,
    ttl: float = 30,
) -> WorkflowController:
    workers = WorkerRegistry()
    workers.register("worker", FunctionWorker(worker))
    return WorkflowController(
        runs=DurableRunManager(store),
        compiler=WorkflowCompiler(
            workers=workers,
            worker_allowlist=frozenset({"worker"}),
            effective_capabilities=frozenset(),
            resource_allowlist=frozenset(),
        ),
        scheduler_factory=lambda graph: TaskScheduler(
            graph=graph,
            workers=workers,
            store=store,
            control=RunControl(checkpoints=store),
        ),
        completion_gate=CompletionGate(verifiers=VerifierRegistry(), artifacts=ArtifactManager()),
        leases=leases,
        lease_owner_id="process-1",
        lease_ttl_seconds=ttl,
    )


async def _execute(controller: WorkflowController, run: Any) -> Any:
    return await controller.execute(
        run=run,
        identity=_identity(),
        plan=_plan(),
        standards=_empty_standards(),
        protected_input_fingerprint="input-1",
    )


def test_running_workflow_holds_a_lease_that_is_released_on_completion(tmp_path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "owner.sqlite3")
        leases = InMemoryLeaseStore()
        seen: dict[str, Any] = {}

        async def work(task: Any, context: Any) -> dict[str, Any]:
            record = await store.get_runtime_run(
                run_id=run.id, tenant_id="tenant-1", user_id="user-1"
            )
            assert record is not None
            seen["key"] = record.owner_lease_key
            seen["contender"] = await leases.acquire(
                key=record.owner_lease_key or "", owner_id="intruder", ttl_seconds=5
            )
            return {}

        _, run = await _runtime_run(store)
        outcome = await _execute(_controller(store, work, leases=leases), run)

        assert outcome.status == WorkflowOutcomeStatus.COMPLETED
        assert seen["key"] == f"runtime-run:{run.id}"
        assert seen["contender"] is None, "a second owner acquired a held run lease"
        free = await leases.acquire(key=seen["key"], owner_id="next", ttl_seconds=5)
        assert free is not None, "lease was not released"

    asyncio.run(scenario())


def test_second_executor_cannot_start_a_run_that_is_owned(tmp_path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "owner.sqlite3")
        leases = InMemoryLeaseStore()
        _, run = await _runtime_run(store)
        held = await leases.acquire(key=f"runtime-run:{run.id}", owner_id="other", ttl_seconds=30)
        assert held is not None

        async def work(task: Any, context: Any) -> dict[str, Any]:
            raise AssertionError("must not run")

        with pytest.raises(RunOwnershipError):
            await _execute(_controller(store, work, leases=leases), run)
        record = await store.get_runtime_run(run_id=run.id, tenant_id="tenant-1", user_id="user-1")
        assert record is not None and record.status == RuntimeRunStatus.CREATED

    asyncio.run(scenario())


def test_losing_the_lease_stops_the_run_without_writing_state(tmp_path) -> None:
    started = asyncio.Event()

    class _LosingLeases(InMemoryLeaseStore):
        async def renew(
            self, lease: DistributedLease, *, ttl_seconds: float, now: datetime | None = None
        ) -> DistributedLease | None:
            await started.wait()  # lose ownership only once the work is running
            return None  # another owner took over

    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "owner.sqlite3")
        stopped = asyncio.Event()

        async def work(task: Any, context: Any) -> dict[str, Any]:
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                stopped.set()
                raise
            return {}

        _, run = await _runtime_run(store)
        controller = _controller(store, work, leases=_LosingLeases(), ttl=0.3)
        with pytest.raises(RunOwnershipError):
            await asyncio.wait_for(_execute(controller, run), timeout=5)
        assert stopped.is_set(), "work continued after ownership was lost"
        record = await store.get_runtime_run(run_id=run.id, tenant_id="tenant-1", user_id="user-1")
        # The new owner decides the run's fate; this process must not record one.
        assert record is not None and record.status == RuntimeRunStatus.RUNNING

    asyncio.run(scenario())


async def _running_run(store: SQLitePersistence, *, lease_key: str | None) -> Any:
    manager, run = await _runtime_run(store)
    await manager.start(run_id=run.id, identity=_identity(), owner_lease_key=lease_key)
    return run


def _recovery(store: SQLitePersistence, leases: Any) -> RuntimeRecoveryManager:
    return RuntimeRecoveryManager(
        stores=DurableRuntimeStores(runs=store, actions=store, approvals=store), leases=leases
    )


def test_recovery_blocks_a_running_run_whose_lease_nobody_holds(tmp_path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "recover.sqlite3")
        run = await _running_run(store, lease_key="runtime-run:orphan")
        report = await _recovery(store, InMemoryLeaseStore()).reconcile(owner_id="process-2")

        assert report.owner_lost_run_ids == (run.id,)
        record = await store.get_runtime_run(run_id=run.id, tenant_id="tenant-1", user_id="user-1")
        assert record is not None
        assert record.status == RuntimeRunStatus.BLOCKED
        assert record.blocked_reason == "owner_lost"

    asyncio.run(scenario())


def test_recovery_leaves_runs_with_a_live_owner_or_unknown_ownership_alone(tmp_path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "recover.sqlite3")
        leases = InMemoryLeaseStore()
        live = await _running_run(store, lease_key="runtime-run:live")
        assert await leases.acquire(key="runtime-run:live", owner_id="p1", ttl_seconds=60)
        legacy = await _running_run(store, lease_key=None)

        report = await _recovery(store, leases).reconcile(owner_id="process-2")

        assert report.owner_lost_run_ids == ()
        for run in (live, legacy):
            record = await store.get_runtime_run(
                run_id=run.id, tenant_id="tenant-1", user_id="user-1"
            )
            assert record is not None and record.status == RuntimeRunStatus.RUNNING

    asyncio.run(scenario())


def test_recovery_never_resumes_a_blocked_run_and_is_idempotent(tmp_path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "recover.sqlite3")
        run = await _running_run(store, lease_key="runtime-run:orphan")
        recovery = _recovery(store, InMemoryLeaseStore())
        await recovery.reconcile(owner_id="process-2")
        second = await recovery.reconcile(owner_id="process-3")

        assert second.owner_lost_run_ids == ()
        record = await store.get_runtime_run(run_id=run.id, tenant_id="tenant-1", user_id="user-1")
        assert record is not None and record.status == RuntimeRunStatus.BLOCKED

    asyncio.run(scenario())

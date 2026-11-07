"""Uncertain-action lookup completeness and run-state write races (M5 review).

Reproduced before the fix:
- cancellation inspected only the first 1,000 executing or uncertain intents, so an
  in-flight action outside that page let the run be recorded as safely CANCELLED;
- recovery wrote its stale RUNNING snapshot over a state the owner had just written;
- a stale owner whose lease had been taken over could still complete the run that the
  new owner was executing, because writes were conditional on neither status nor owner.
"""

from __future__ import annotations

import asyncio
import sqlite3
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
    RuntimeRunConflictError,
    RuntimeRunStatus,
)
from kaiwen_agent.tasks import TaskScheduler
from kaiwen_agent.tools.durability import ActionIntentRecord, ActionIntentStatus
from kaiwen_agent.workers import FunctionWorker, WorkerRegistry
from kaiwen_agent.workflow import (
    WorkflowCompiler,
    WorkflowController,
    WorkflowOutcomeStatus,
    WorkflowPlan,
    WorkflowTaskSpec,
)
from tests.test_workflow_runtime import _empty_standards, _identity, _runtime_run

PLAN = WorkflowPlan(
    objective="Race",
    tasks=(WorkflowTaskSpec(id="work", name="Work", worker_id="worker"),),
)


def _controller(
    store: Any,
    worker: Any,
    *,
    action_intents: Any = None,
    leases: Any = None,
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
        action_intents=action_intents,
        leases=leases,
        lease_ttl_seconds=ttl,
    )


async def _execute(controller: WorkflowController, run: Any) -> Any:
    return await controller.execute(
        run=run,
        identity=_identity(),
        plan=PLAN,
        standards=_empty_standards(),
        protected_input_fingerprint="input-1",
    )


def _intent(index: int, *, run_id: str, status: str = "executing") -> ActionIntentRecord:
    return ActionIntentRecord(
        id=f"intent-{run_id}-{index}",
        runtime_run_id=run_id,
        tenant_id="tenant-1",
        user_id="user-1",
        tool_name="send",
        call_id=f"call-{index}",
        arguments_checksum="a" * 64,
        idempotency_key=f"key-{run_id}-{index}",
        side_effect="reversible",
        status=ActionIntentStatus(status),
    )


def _bulk_insert(path: Any, intents: list[ActionIntentRecord]) -> None:
    """Insert rows directly; creating thousands through the async API would be slow."""

    connection = sqlite3.connect(path)
    with connection:
        connection.executemany(
            """INSERT INTO action_intents
                (id, runtime_run_id, tenant_id, user_id, status, idempotency_key,
                 intent_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    intent.id,
                    intent.runtime_run_id,
                    intent.tenant_id,
                    intent.user_id,
                    intent.status.value,
                    intent.idempotency_key,
                    intent.model_dump_json(),
                    intent.updated_at.isoformat(),
                )
                for intent in intents
            ],
        )
    connection.close()


class _HidesScopedQuery:
    """A durable store without the optional run-scoped intent query."""

    def __init__(self, inner: SQLitePersistence) -> None:
        self._inner = inner
        self.is_durable = True

    def __getattr__(self, name: str) -> Any:
        if name == "list_run_action_intents":
            raise AttributeError(name)
        return getattr(self._inner, name)


async def _cancelled_run(
    tmp_path, *, unrelated: int, target_in_flight: bool, hide_scoped_query: bool
) -> Any:
    store = SQLitePersistence(tmp_path / "lookup.sqlite3")
    await store.initialize()
    manager, run = await _runtime_run(store)
    # Older rows first, so the target run's intent falls outside a 1,000-row page.
    _bulk_insert(
        tmp_path / "lookup.sqlite3",
        [_intent(i, run_id="runtime-other") for i in range(unrelated)],
    )
    if target_in_flight:
        _bulk_insert(tmp_path / "lookup.sqlite3", [_intent(0, run_id=run.id)])
    started = asyncio.Event()

    async def slow(task: Any, context: Any) -> dict[str, Any]:
        started.set()
        await asyncio.sleep(60)
        return {}

    intents_view: Any = _HidesScopedQuery(store) if hide_scoped_query else store
    controller = _controller(store, slow, action_intents=intents_view)
    task = asyncio.create_task(_execute(controller, run))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    record = await manager.get(run_id=run.id, identity=_identity())
    assert record is not None
    return record


def test_cancellation_sees_an_in_flight_action_beyond_the_first_thousand_intents(tmp_path) -> None:
    record = asyncio.run(
        _cancelled_run(tmp_path, unrelated=1_005, target_in_flight=True, hide_scoped_query=False)
    )

    assert record.status == RuntimeRunStatus.BLOCKED
    assert record.blocked_reason is not None
    assert record.blocked_reason.startswith("uncertain_action:intent-")


def test_cancellation_without_a_scoped_query_never_trusts_an_incomplete_page(tmp_path) -> None:
    record = asyncio.run(
        _cancelled_run(tmp_path, unrelated=1_005, target_in_flight=True, hide_scoped_query=True)
    )

    assert record.status == RuntimeRunStatus.BLOCKED
    assert record.blocked_reason is not None
    assert record.blocked_reason.startswith("uncertain_action:")


def test_incomplete_page_with_no_relevant_action_is_still_not_proof_of_safety(tmp_path) -> None:
    record = asyncio.run(
        _cancelled_run(tmp_path, unrelated=1_005, target_in_flight=False, hide_scoped_query=True)
    )

    assert record.status == RuntimeRunStatus.BLOCKED
    assert record.blocked_reason == "uncertain_action:lookup_incomplete"


def test_scoped_query_proves_no_action_exists_even_with_many_unrelated_intents(tmp_path) -> None:
    record = asyncio.run(
        _cancelled_run(tmp_path, unrelated=1_005, target_in_flight=False, hide_scoped_query=False)
    )

    assert record.status == RuntimeRunStatus.CANCELLED


def test_complete_small_page_without_a_scoped_query_allows_a_safe_cancel(tmp_path) -> None:
    record = asyncio.run(
        _cancelled_run(tmp_path, unrelated=5, target_in_flight=False, hide_scoped_query=True)
    )

    assert record.status == RuntimeRunStatus.CANCELLED


def test_failed_lookup_blocks_instead_of_cancelling(tmp_path) -> None:
    class _Broken(_HidesScopedQuery):
        async def list_action_intents(self, **kwargs: Any) -> Any:
            raise RuntimeError("store unavailable")

    async def scenario() -> Any:
        store = SQLitePersistence(tmp_path / "lookup.sqlite3")
        manager, run = await _runtime_run(store)
        started = asyncio.Event()

        async def slow(task: Any, context: Any) -> dict[str, Any]:
            started.set()
            await asyncio.sleep(60)
            return {}

        controller = _controller(store, slow, action_intents=_Broken(store))
        task = asyncio.create_task(_execute(controller, run))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return await manager.get(run_id=run.id, identity=_identity())

    record = asyncio.run(scenario())
    assert record is not None and record.status == RuntimeRunStatus.BLOCKED
    assert record.blocked_reason == "uncertain_action:unverified"


class _SnapshotStore(SQLitePersistence):
    """Lets a legitimate owner write between recovery's listing and recovery's write."""

    after_list: Any = None

    async def list_runtime_runs(self, **kwargs: Any) -> Any:
        runs = await super().list_runtime_runs(**kwargs)
        if self.after_list is not None:
            hook, self.after_list = self.after_list, None
            await hook()
        return runs


def test_recovery_does_not_overwrite_a_state_the_owner_wrote_after_the_snapshot(tmp_path) -> None:
    async def scenario() -> None:
        store = _SnapshotStore(tmp_path / "snapshot.sqlite3")
        manager, run = await _runtime_run(store)
        await manager.start(
            run_id=run.id, identity=_identity(), owner_lease_key="runtime-run:x", owner_fence=1
        )

        async def owner_pauses() -> None:
            await manager.pause(
                run_id=run.id, identity=_identity(), safe_boundary="owner-boundary", owner_fence=1
            )

        store.after_list = owner_pauses
        recovery = RuntimeRecoveryManager(
            stores=DurableRuntimeStores(runs=store, actions=store, approvals=store),
            leases=InMemoryLeaseStore(),
        )
        report = await recovery.reconcile(owner_id="process-2")

        assert report.owner_lost_run_ids == ()
        record = await manager.get(run_id=run.id, identity=_identity())
        assert record is not None
        assert record.status == RuntimeRunStatus.PAUSED
        assert record.safe_boundary == "owner-boundary"

    asyncio.run(scenario())


class _BlindLeases:
    """A process whose heartbeat has stalled: it never notices that it lost its lease."""

    def __init__(self, shared: InMemoryLeaseStore) -> None:
        self.shared = shared

    async def acquire(self, **kwargs: Any) -> DistributedLease | None:
        return await self.shared.acquire(**kwargs)

    async def renew(
        self, lease: DistributedLease, *, ttl_seconds: float, now: datetime | None = None
    ) -> DistributedLease | None:
        return lease

    async def release(self, lease: DistributedLease) -> bool:
        return await self.shared.release(lease)


def test_a_stale_owner_cannot_complete_a_run_the_new_owner_is_executing(tmp_path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "takeover.sqlite3")
        shared = InMemoryLeaseStore()
        manager, run = await _runtime_run(store)
        started_a, started_b = asyncio.Event(), asyncio.Event()
        release_a, release_b = asyncio.Event(), asyncio.Event()

        async def work_a(task: Any, context: Any) -> dict[str, Any]:
            started_a.set()
            await release_a.wait()
            return {}

        async def work_b(task: Any, context: Any) -> dict[str, Any]:
            started_b.set()
            await release_b.wait()
            return {}

        owner_a = asyncio.create_task(
            _execute(_controller(store, work_a, leases=_BlindLeases(shared), ttl=0.3), run)
        )
        await started_a.wait()
        await asyncio.sleep(0.5)  # A's lease expires; its stalled heartbeat does not renew it

        recovery = RuntimeRecoveryManager(
            stores=DurableRuntimeStores(runs=store, actions=store, approvals=store),
            leases=shared,
        )
        assert (await recovery.reconcile(owner_id="process-2")).owner_lost_run_ids == (run.id,)
        blocked = await manager.get(run_id=run.id, identity=_identity())
        assert blocked is not None and blocked.status == RuntimeRunStatus.BLOCKED

        owner_b = asyncio.create_task(_execute(_controller(store, work_b, leases=shared), blocked))
        await started_b.wait()
        takeover = await manager.get(run_id=run.id, identity=_identity())
        assert takeover is not None and takeover.status == RuntimeRunStatus.RUNNING

        release_a.set()
        with pytest.raises(RunOwnershipError):
            await asyncio.wait_for(owner_a, timeout=10)

        after_stale_write = await manager.get(run_id=run.id, identity=_identity())
        assert after_stale_write is not None
        assert after_stale_write.status == RuntimeRunStatus.RUNNING, "a stale owner wrote state"
        assert after_stale_write.owner_fencing_token == takeover.owner_fencing_token

        release_b.set()
        outcome = await asyncio.wait_for(owner_b, timeout=10)
        assert outcome.status == WorkflowOutcomeStatus.COMPLETED

    asyncio.run(scenario())


def test_conditional_run_writes_enforce_status_and_owner_fence(tmp_path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "guard.sqlite3")
        manager, run = await _runtime_run(store)
        await manager.start(
            run_id=run.id, identity=_identity(), owner_lease_key="runtime-run:x", owner_fence=7
        )
        current = await manager.get(run_id=run.id, identity=_identity())
        assert current is not None
        paused = current.model_copy(update={"status": RuntimeRunStatus.PAUSED})

        with pytest.raises(RuntimeRunConflictError):
            await store.save_runtime_run(paused, expected_status=RuntimeRunStatus.PAUSED)
        with pytest.raises(RuntimeRunConflictError):
            await store.save_runtime_run(
                paused, expected_status=RuntimeRunStatus.RUNNING, expected_owner_fence=6
            )
        await store.save_runtime_run(
            paused, expected_status=RuntimeRunStatus.RUNNING, expected_owner_fence=7
        )
        persisted = await manager.get(run_id=run.id, identity=_identity())
        assert persisted is not None and persisted.status == RuntimeRunStatus.PAUSED

        with pytest.raises(RuntimeRunConflictError):
            await manager.cancel(run_id=run.id, identity=_identity(), owner_fence=99)

    asyncio.run(scenario())

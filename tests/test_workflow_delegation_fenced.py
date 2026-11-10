"""M6.3.3-C1 on fenced execution (ADR 0015 and ADR 0016): a stale owner cannot delegate.

Real SQLite, a real `SQLiteLeaseStore` on an injected clock and the real controller, scheduler and
fenced task store. Ownership is lost at exact, deterministic points (no sleeps). What is asserted is
what the platform enforces: after the owner epoch stops being current, no protected task write is
accepted, so the delegated task is not persisted, the run is not completed and the gate is not
consulted. What is not asserted: a worker already invoked before the loss may still have effects.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kaiwen_agent.exceptions import FencedConfigurationError, OwnershipLostError, RunOwnershipError
from kaiwen_agent.runtime import RuntimeRunStatus
from kaiwen_agent.tasks import TaskScheduler
from tests.test_agent_runtime_worker import owner
from tests.test_workflow_delegation_cycle import Harness, Source, propose_for, run_async
from tests.test_workflow_delegation_r1 import lease_harness

EXPIRED = 3600


def test_a_lease_that_expires_without_another_owner_still_refuses_the_delegated_write(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness, leases = lease_harness(tmp_path, source=Source(propose_for("lead")))
        run = await harness.new_run()
        checks: list[int] = []
        original = leases.renew

        async def renew(lease: Any, **options: Any) -> Any:
            renewed = await original(lease, **options)
            checks.append(1)
            if renewed is not None and len(checks) == 2:  # after A's last successful check
                harness.flow.clock.advance(EXPIRED)  # expiry, and nobody else holds the lease
            return renewed

        leases.renew = renew  # type: ignore[method-assign]
        with pytest.raises(OwnershipLostError):
            await harness.execute(run)
        assert harness.ran == ["lead"] and await harness.logical_ids(run) == ["lead"]
        assert harness.gate.calls == 0
        assert await harness.status(run) is RuntimeRunStatus.RUNNING  # nothing recorded for A

    run_async(scenario)


def test_ownership_lost_while_the_delegated_worker_runs_records_nothing_for_it(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = Harness(tmp_path, source=Source(propose_for("lead")))
        run = await harness.new_run()

        async def lose() -> None:
            harness.flow.clock.advance(EXPIRED)  # the lease expires inside the child's worker

        harness.on_run["a"] = lose
        with pytest.raises(RunOwnershipError):
            await harness.execute(run)
        assert harness.ran == ["lead", "a"]  # the worker was already authorized and did run
        rows = await harness.store.list_run_tasks(
            tenant_id="tenant-1", user_id="user-1", runtime_run_id=run.id, limit=50
        )
        # The child's COMPLETED write was refused: it is not recorded as completed, and the run
        # was neither completed nor judged by the gate on behalf of the stale owner.
        child = next(t for t in rows.tasks if t.metadata["logical_task_id"] == "a")
        assert child.status.value != "completed"
        assert harness.gate.calls == 0
        assert await harness.status(run) is RuntimeRunStatus.RUNNING

    run_async(scenario)


def test_an_unfenced_scheduler_is_refused_before_it_can_write_the_delegated_cycle(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = Harness(tmp_path, source=Source(propose_for("lead")))
        run = await harness.new_run()
        original = harness.factory

        def factory(graph: Any) -> TaskScheduler:
            scheduler = original(graph)
            if len(harness.graphs) == 2:  # the delegated cycle gets an ordinary task store
                scheduler.store = harness.store
            return scheduler

        harness.factory = factory  # type: ignore[method-assign]
        with pytest.raises(FencedConfigurationError, match="fenced task store"):
            await harness.execute(run)
        assert harness.ran == ["lead"] and await harness.logical_ids(run) == ["lead"]

    run_async(scenario)


def test_a_valid_owner_delegates_and_the_epoch_is_the_one_published_for_the_run(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = Harness(tmp_path, source=Source(propose_for("lead")))
        run = await harness.new_run()
        outcome = await harness.execute(run)
        assert outcome.status.value == "completed" and harness.ran == ["lead", "a"]
        record = await harness.flow.runs.get(run_id=run.id, identity=owner())
        assert record is not None and record.fenced and record.owner_fencing_token == 1
        assert await harness.logical_ids(run) == ["a", "lead"]

    run_async(scenario)

"""PR #14 audit R1: incomplete inventories, real lease ownership and honest replay semantics.

- An authoritative quota decision needs a complete task inventory. A page that reports
  unreadable rows, or rows that do not belong to the run, is refused with a stable code, while a
  genuinely empty or healthy listing is unaffected.
- The run record's stored fencing token does not prove the lease is still held: the lease can be
  replaced while the record still names the old token. The controller asks the lease store itself
  (renew, which only the current holder can pass) after authorization and again right before the
  delegated tasks are scheduled. This narrows the window; it is not an atomic guarantee, and the
  tests state the remaining limit instead of hiding it.
- Replay is at-least-once. Deterministic ids give one task row, not exactly-once execution.

Lease loss is simulated with a controlled lease store (no sleeps): a second executor takes the
lease with a clock past expiry while the heartbeat (30 s TTL, first tick at 10 s) has not fired.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from kaiwen_agent.coordination import DistributedLease, InMemoryLeaseStore
from kaiwen_agent.exceptions import RunOwnershipError
from kaiwen_agent.policy import DelegationPolicy
from kaiwen_agent.quality import CompletionGateStatus
from kaiwen_agent.runtime import RuntimeRunStatus
from kaiwen_agent.tasks import AgentTask, TaskRunPage
from kaiwen_agent.types import utc_now
from kaiwen_agent.workflow import WorkflowOutcomeStatus
from tests.test_agent_runtime_worker import owner
from tests.test_delegation_job_path import SECRET
from tests.test_workflow_delegation_cycle import (
    Harness,
    Source,
    blocked,
    propose_for,
    run_async,
)

# ------------------------------------------------------------------ incomplete inventory


class Tampered:
    """A task store whose pages are altered after the real store answered."""

    def __init__(self, inner: Any, alter: Any) -> None:
        self.inner = inner
        self.alter = alter

    async def list_run_tasks(self, **query: Any) -> TaskRunPage:
        return self.alter(await self.inner.list_run_tasks(**query))  # type: ignore[no-any-return]


def foreign(task: AgentTask) -> AgentTask:
    """A row the store returned although it belongs to another run."""

    return task.model_copy(
        update={"id": "task_zzz_foreign", "metadata": {**task.metadata, "runtime_run_id": "other"}}
    )


def test_unreadable_rows_make_the_inventory_incomplete_and_deny(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(tmp_path, source=Source(propose_for("lead")))
        admission = harness.admission(
            tasks=Tampered(
                harness.store,
                lambda page: TaskRunPage(page.tasks, page.next_after_id, page.unreadable + 1),
            )
        )
        run = await harness.new_run()
        outcome = await harness.execute(run, controller=harness.controller(admission=admission))
        # The readable rows (the parent only) would leave every quota open: still no allow.
        blocked(outcome, "delegation_denied:state_incomplete")
        assert harness.ran == ["lead"] and harness.gate.calls == 0
        assert await harness.logical_ids(run) == ["lead"]

    run_async(scenario)


def test_rows_dropped_for_an_ownership_mismatch_make_the_inventory_incomplete(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = Harness(tmp_path, source=Source(propose_for("lead")))
        admission = harness.admission(
            tasks=Tampered(
                harness.store,
                lambda page: TaskRunPage((*page.tasks, foreign(page.tasks[0])), None, 0),
            )
        )
        run = await harness.new_run()
        outcome = await harness.execute(run, controller=harness.controller(admission=admission))
        blocked(outcome, "delegation_denied:state_incomplete")
        assert harness.ran == ["lead"] and SECRET not in (outcome.reason or "")

    run_async(scenario)


def test_a_healthy_listing_still_admits_and_an_empty_one_is_not_incomplete(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        healthy = Harness(tmp_path / "healthy", source=Source(propose_for("lead")))
        outcome = await healthy.execute(await healthy.new_run())
        assert outcome.status is WorkflowOutcomeStatus.COMPLETED
        assert healthy.ran == ["lead", "a"]

        # A run with no task rows at all is complete (nothing was dropped): the parent is simply
        # not there, which is a different, equally closed answer.
        empty = Harness(tmp_path / "empty", source=Source(propose_for("lead")))
        admission = empty.admission(
            tasks=Tampered(empty.store, lambda page: TaskRunPage((), None, 0))
        )
        out = await empty.execute(
            await empty.new_run(), controller=empty.controller(admission=admission)
        )
        blocked(out, "delegation_denied:parent_not_found")

    run_async(scenario)


# ------------------------------------------------------------------ real lease ownership


class ReplaceableLeases(InMemoryLeaseStore):
    """The reference lease store plus a deterministic way for a second executor to take over."""

    def __init__(self) -> None:
        super().__init__()
        self.taken: list[DistributedLease] = []
        self.fail_renew = False

    async def takeover(self, key: str) -> DistributedLease:
        # Observed past expiry, exactly as a second executor would see an expired lease.
        lease = await self.acquire(
            key=key,
            owner_id="controller-2",
            ttl_seconds=30,
            now=utc_now() + timedelta(seconds=3600),
        )
        assert lease is not None
        self.taken.append(lease)
        return lease

    def takeover_now(self, key: str) -> None:
        """The same takeover without awaiting, for a synchronous hook (the compiler)."""

        current = self._leases[key]
        self._fences[key] = current.fencing_token + 1
        self._leases[key] = current.model_copy(
            update={
                "owner_id": "controller-2",
                "token": "lease_taken",
                "fencing_token": current.fencing_token + 1,
            }
        )

    async def renew(self, lease: DistributedLease, **options: Any) -> DistributedLease | None:
        if self.fail_renew:
            raise RuntimeError(SECRET)
        return await super().renew(lease, **options)


async def record_token(harness: Harness, run: Any) -> int | None:
    record = await harness.flow.runs.get(run_id=run.id, identity=owner())
    assert record is not None
    return record.owner_fencing_token


def lease_harness(tmp_path: Path, **options: Any) -> tuple[Harness, ReplaceableLeases]:
    harness = Harness(tmp_path, **options)
    leases = ReplaceableLeases()
    harness.leases = leases
    return harness, leases


def test_the_stored_fencing_token_alone_does_not_prove_the_lease_is_held(
    tmp_path: Path,
) -> None:
    """The reproduction: the lease is replaced, the run record still names the old token."""

    async def scenario() -> None:
        harness, leases = lease_harness(tmp_path)
        run = await harness.new_run()
        stolen: list[DistributedLease] = []

        async def take(request: Any) -> Any:
            stolen.append(await leases.takeover(f"runtime-run:{run.id}"))
            # The new owner has not written the run record yet: it still carries the old token.
            assert await record_token(harness, run) == stolen[0].fencing_token - 1
            return propose_for("lead")(request)

        harness.source = Source(take)
        with pytest.raises(RunOwnershipError):
            await harness.execute(run)
        assert harness.ran == ["lead"] and await harness.logical_ids(run) == ["lead"]
        assert harness.gate.calls == 0

    run_async(scenario)


def test_lease_loss_during_policy_evaluation_stops_before_delegated_tasks(
    tmp_path: Path,
) -> None:
    class Takes:
        policy_id = "takes"

        def __init__(self) -> None:
            self.leases: ReplaceableLeases | None = None
            self.key = ""

        async def evaluate(self, context: Any) -> Any:
            if context.action == "agent.delegate" and self.leases is not None:
                await self.leases.takeover(self.key)
            return None

    async def scenario() -> None:
        takes = Takes()
        harness, leases = lease_harness(
            tmp_path,
            source=Source(propose_for("lead")),
            rules=[
                DelegationPolicy(
                    max_depth=3,
                    max_parallel_jobs=3,
                    max_jobs_per_run=6,
                    allowed_delegates={"coder"},
                ),
                takes,
            ],
        )
        run = await harness.new_run()
        takes.leases, takes.key = leases, f"runtime-run:{run.id}"
        with pytest.raises(RunOwnershipError):
            await harness.execute(run)
        assert harness.ran == ["lead"] and harness.gate.calls == 0
        assert await harness.logical_ids(run) == ["lead"]

    run_async(scenario)


def test_lease_loss_after_authorization_but_before_scheduling_runs_nothing(
    tmp_path: Path,
) -> None:
    """The check is repeated right before the delegated cycle is handed to the scheduler."""

    async def scenario() -> None:
        harness, leases = lease_harness(tmp_path, source=Source(propose_for("lead")))
        run = await harness.new_run()
        real_compile = harness.compiler.compile

        def compile_then_lose(*args: Any, **kwargs: Any) -> Any:
            graph = real_compile(*args, **kwargs)
            if kwargs.get("delegation") is not None:
                leases.takeover_now(f"runtime-run:{run.id}")
            return graph

        harness.compiler.compile = compile_then_lose  # type: ignore[method-assign]
        with pytest.raises(RunOwnershipError):
            await harness.execute(run)
        assert harness.ran == ["lead"]  # the delegated task never started
        assert await harness.logical_ids(run) == ["lead"]

    run_async(scenario)


def test_a_failing_lease_store_fails_closed_without_leaking(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness, leases = lease_harness(tmp_path)
        run = await harness.new_run()

        def flip(request: Any) -> Any:
            leases.fail_renew = True
            return propose_for("lead")(request)

        harness.source = Source(flip)
        with pytest.raises(RunOwnershipError) as caught:
            await harness.execute(run)
        assert SECRET not in str(caught.value)
        assert harness.ran == ["lead"] and harness.gate.calls == 0

    run_async(scenario)


def test_a_holder_whose_lease_is_intact_delegates_and_the_gate_still_decides(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness, leases = lease_harness(tmp_path, source=Source(propose_for("lead")))
        harness.gate.status = CompletionGateStatus.BLOCKED
        run = await harness.new_run()
        outcome = await harness.execute(run)
        assert harness.ran == ["lead", "a"] and harness.gate.calls == 1
        assert outcome.status is WorkflowOutcomeStatus.BLOCKED
        assert outcome.reason == "quality_blocked"  # delegation never replaced the gate

    run_async(scenario)


def test_cancellation_after_a_lease_check_is_still_recorded_safely(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness, _ = lease_harness(tmp_path)
        run = await harness.new_run()
        pending: list[asyncio.Task[Any]] = []

        async def cancel_me(request: Any) -> Any:
            pending[0].cancel()
            await asyncio.sleep(0)
            return propose_for("lead")(request)

        harness.source = Source(cancel_me)
        task = asyncio.ensure_future(harness.execute(run))
        pending.append(task)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=20)
        assert await harness.status(run) is RuntimeRunStatus.CANCELLED
        assert harness.ran == ["lead"]

    run_async(scenario)


# ------------------------------------------------------------------ replay is at-least-once


def test_replay_reruns_a_completed_worker_and_only_the_row_is_deduplicated(
    tmp_path: Path,
) -> None:
    """Deterministic ids give one task row. They do not give exactly-once execution."""

    async def scenario() -> None:
        harness = Harness(
            tmp_path,
            source=Source(propose_for("lead"), None, propose_for("lead"), None),
        )
        harness.gate.status = CompletionGateStatus.BLOCKED
        run = await harness.new_run()
        blocked(await harness.execute(run), "quality_blocked")
        assert harness.ran == ["lead", "a"]
        harness.rules = [
            DelegationPolicy(
                max_depth=3, max_parallel_jobs=3, max_jobs_per_run=6, allowed_delegates={"coder"}
            )
        ]
        harness.gate.status = CompletionGateStatus.PASS
        outcome = await harness.execute(run)
        assert outcome.status is WorkflowOutcomeStatus.COMPLETED
        # The same worker bodies ran again on replay: at-least-once, not exactly-once.
        assert harness.ran.count("lead") == 2
        assert await harness.logical_ids(run) == ["a", "lead"]  # but never two rows for one task

    run_async(scenario)

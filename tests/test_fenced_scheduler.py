"""Package 3: the scheduler under fenced execution (ADR 0016).

Real `WorkflowController`, real `TaskScheduler` over the controller's fenced task store, real
SQLite and a real `SQLiteLeaseStore`; workers are deterministic functions. Ownership is lost by
advancing the injected clock past the lease expiry at an exact point (inside a worker, inside the
store, or before the scheduler starts): no sleeps and no timing assumptions.

What these tests establish: after a fenced write is refused the scheduler stops dispatch, cancels
the other workers, releases its resources, records nothing (no success, no failure, no retry, no
safe cancellation) and reports the original refusal. What they do not claim: a worker already
invoked keeps running until it is cancelled, and its own external effects are not undone.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from kaiwen_agent.exceptions import OwnershipLostError
from kaiwen_agent.runtime import RuntimeRunStatus
from kaiwen_agent.standards import StandardResolution
from kaiwen_agent.tasks import AgentTask, TaskStatus
from kaiwen_agent.workers import FunctionWorker
from kaiwen_agent.workflow import WorkflowController
from tests.test_agent_runtime_worker import owner, plan, spec
from tests.test_fenced_writes import TTL, FencedFlow, run_async

RETRY = {"retry_policy": {"max_attempts": 3, "retry_on": ["execution_error"]}}
FINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}


class Probe:
    """Records what ran and lets a worker lose ownership at an exact point."""

    def __init__(self, flow: FencedFlow) -> None:
        self.flow = flow
        self.ran: list[str] = []
        self.cancelled: list[str] = []
        self.lose_in: set[str] = set()
        self.swallow_progress = False

    async def work(self, task: AgentTask, context: Any) -> dict[str, Any]:
        name = str(task.metadata["logical_task_id"])
        self.ran.append(name)
        try:
            if name in self.lose_in:
                self.flow.clock.advance(TTL + 1)  # the lease expires while this worker runs
            if name == "hang":
                await asyncio.Event().wait()
            if name == "progress":
                with suppress(Exception):  # a worker that swallows the refusal
                    await context.report_progress(0.5)
                await asyncio.Event().wait()
            if name == "raises":
                raise RuntimeError("worker failed")
            if name == "denied":
                # What a platform tool does when the fenced claim is refused: the refusal itself
                # surfaces from the worker while the task store would still accept writes.
                raise OwnershipLostError("lease_superseded")
            return {"done": name}
        except asyncio.CancelledError:
            self.cancelled.append(name)
            raise


def build(tmp_path: Path) -> tuple[FencedFlow, Probe]:
    flow = FencedFlow(tmp_path)
    probe = Probe(flow)
    flow.workers.register("w", FunctionWorker(probe.work))
    flow.registered.add("w")
    return flow, probe


async def execute(flow: FencedFlow, run: Any, workflow: Any, controller: Any = None) -> Any:
    controller = controller or flow.fenced_controller()
    return await controller.execute(
        run=run,
        identity=owner(),
        plan=workflow,
        standards=StandardResolution(pack_refs=(), requirements=(), prompt_refs=()),
        protected_input_fingerprint="objective-1",
    )


async def rows(flow: FencedFlow, run: Any) -> dict[str, TaskStatus]:
    page = await flow.store.list_run_tasks(
        tenant_id="tenant-1", user_id="user-1", runtime_run_id=run.id, limit=100
    )
    return {str(task.metadata["logical_task_id"]): task.status for task in page.tasks}


async def refused_with(
    flow: FencedFlow, run: Any, workflow: Any, code: str, **options: Any
) -> None:
    controller = flow.fenced_controller(**options)
    with pytest.raises(OwnershipLostError) as caught:
        await execute(flow, run, workflow, controller)
    assert caught.value.code == code


def test_a_fenced_workflow_completes_through_the_fenced_store_only(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow, probe = build(tmp_path)

        async def forbidden(task: AgentTask) -> None:
            raise AssertionError("an ordinary save_task was used in fenced mode")

        flow.store.save_task = forbidden  # type: ignore[method-assign]
        run = await flow.new_run()
        outcome = await execute(
            flow, run, plan(spec("a", "w"), spec("b", "w", dependencies=("a",)))
        )
        assert outcome.status.value == "completed"
        assert probe.ran == ["a", "b"]
        assert await rows(flow, run) == {"a": TaskStatus.COMPLETED, "b": TaskStatus.COMPLETED}
        record = await flow.runs.get(run_id=run.id, identity=owner())
        assert record is not None and record.fenced and record.owner_fencing_token == 1

    run_async(scenario)


def test_ownership_lost_before_task_creation_writes_and_runs_nothing(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow, probe = build(tmp_path)
        run = await flow.new_run()

        def lose_then_schedule(graph: Any) -> Any:
            flow.clock.advance(TTL + 1)
            return flow.scheduler(graph)

        await refused_with(
            flow, run, plan(spec("a", "w")), "lease_expired", scheduler_factory=lose_then_schedule
        )
        assert probe.ran == [] and await rows(flow, run) == {}
        assert await flow.status(run) is RuntimeRunStatus.RUNNING  # nothing recorded for it

    run_async(scenario)


def test_ownership_lost_before_the_queued_write_never_invokes_the_worker(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow, probe = build(tmp_path)
        run = await flow.new_run()
        original = flow.store.save_task_fenced

        async def lose_before_queue(task: AgentTask, epoch: Any) -> None:
            if task.status is TaskStatus.QUEUED:
                flow.clock.advance(TTL + 1)
            await original(task, epoch)

        flow.store.save_task_fenced = lose_before_queue  # type: ignore[method-assign]
        await refused_with(flow, run, plan(spec("a", "w")), "lease_expired")
        assert probe.ran == []  # the dispatch was not authorized
        assert (await rows(flow, run)).get("a") not in FINAL

    run_async(scenario)


def test_ownership_lost_after_running_persisted_but_before_invocation(tmp_path: Path) -> None:
    """A successful RUNNING write is not a permanent lease: the worker is invoked, but cannot
    commit anything, and nothing further is dispatched."""

    async def scenario() -> None:
        flow, probe = build(tmp_path)
        run = await flow.new_run()
        original = flow.store.save_task_fenced

        async def lose_after_running(task: AgentTask, epoch: Any) -> None:
            await original(task, epoch)
            if task.status is TaskStatus.RUNNING:
                flow.clock.advance(TTL + 1)

        flow.store.save_task_fenced = lose_after_running  # type: ignore[method-assign]
        await refused_with(
            flow, run, plan(spec("a", "w"), spec("b", "w", dependencies=("a",))), "lease_expired"
        )
        assert probe.ran == ["a"]  # the documented limit: invocation is not atomically guarded
        states = await rows(flow, run)
        assert states["a"] is TaskStatus.RUNNING  # no success was recorded through the dead epoch
        assert "b" not in probe.ran and states.get("b") not in FINAL

    run_async(scenario)


def test_ownership_lost_while_a_worker_executes_records_no_success_failure_or_cancel(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        flow, probe = build(tmp_path)
        probe.lose_in = {"a"}
        run = await flow.new_run()
        await refused_with(
            flow, run, plan(spec("a", "w"), spec("b", "w", dependencies=("a",))), "lease_expired"
        )
        states = await rows(flow, run)
        assert probe.ran == ["a"] and states["a"] is TaskStatus.RUNNING
        assert not (set(states.values()) & FINAL)  # no misleading terminal state
        assert await flow.status(run) is RuntimeRunStatus.RUNNING  # no fail/block written either

    run_async(scenario)


def test_other_active_workers_are_cancelled_and_resources_are_released(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow, probe = build(tmp_path)
        probe.lose_in = {"a"}
        run = await flow.new_run()
        workflow = plan(spec("a", "w"), spec("hang", "w", resource_keys=("repo:main",)))
        await refused_with(flow, run, workflow, "lease_expired")
        assert "hang" in probe.cancelled  # cooperatively cancelled, not left running
        assert (await rows(flow, run))["hang"] not in FINAL
        scheduler = flow.schedulers[-1]
        assert await scheduler.resources.try_acquire(("repo:main",), "probe")  # no leaked lock

    run_async(scenario)


def test_a_failure_after_ownership_loss_is_not_recorded_and_never_retried(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow, probe = build(tmp_path)
        probe.lose_in = {"raises"}
        run = await flow.new_run()
        await refused_with(flow, run, plan(spec("raises", "w", **RETRY)), "lease_expired")
        assert probe.ran == ["raises"]  # not re-dispatched as an ordinary retry
        assert (await rows(flow, run))["raises"] is TaskStatus.RUNNING  # not FAILED, not READY

    run_async(scenario)


def test_a_retry_is_an_ordinary_retry_while_ownership_holds(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow, probe = build(tmp_path)
        run = await flow.new_run()
        outcome = await execute(flow, run, plan(spec("raises", "w", **RETRY)))
        assert outcome.status.value == "failed"  # fails after its attempts, recorded normally
        assert probe.ran == ["raises"] * 3

    run_async(scenario)


def test_ownership_lost_during_progress_stops_a_worker_that_swallows_the_error(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        flow, probe = build(tmp_path)
        probe.lose_in = set()
        run = await flow.new_run()
        original = flow.store.save_task_fenced

        async def lose_on_progress(task: AgentTask, epoch: Any) -> None:
            if task.progress == 0.5:
                flow.clock.advance(TTL + 1)
            await original(task, epoch)

        flow.store.save_task_fenced = lose_on_progress  # type: ignore[method-assign]
        # Well below the heartbeat interval (10 s), and not `wait_for`: its timeout cancellation
        # would itself end the run. Only the scheduler can stop a worker that swallows the error.
        pending = asyncio.ensure_future(execute(flow, run, plan(spec("progress", "w"))))
        done, _ = await asyncio.wait({pending}, timeout=3)
        if not done:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            pytest.fail("the scheduler did not stop a worker that swallowed the refusal")
        with pytest.raises(OwnershipLostError) as caught:
            pending.result()
        assert caught.value.code == "lease_expired"
        assert probe.ran == ["progress"] and "progress" in probe.cancelled  # stopped anyway
        assert (await rows(flow, run))["progress"] is TaskStatus.RUNNING

    run_async(scenario)


def test_cancellation_is_recorded_while_owned_and_not_recorded_after_ownership_loss(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        # Owned: the run is cancelled and the cancellation is recorded as safe (no action runs).
        flow, probe = build(tmp_path / "owned")
        run = await flow.new_run()
        pending = asyncio.ensure_future(execute(flow, run, plan(spec("hang", "w"))))
        while "hang" not in probe.ran:
            await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert await flow.status(run) is RuntimeRunStatus.CANCELLED

        # Lost: nothing may be recorded through the dead epoch, least of all a safe cancellation.
        flow2, probe2 = build(tmp_path / "lost")
        run2 = await flow2.new_run()
        pending2 = asyncio.ensure_future(execute(flow2, run2, plan(spec("hang", "w"))))
        while "hang" not in probe2.ran:
            await asyncio.sleep(0)
        flow2.clock.advance(TTL + 1)
        pending2.cancel()
        with pytest.raises((asyncio.CancelledError, OwnershipLostError)):
            await pending2
        assert await flow2.status(run2) is RuntimeRunStatus.RUNNING

    run_async(scenario)


def test_the_scheduler_cannot_be_driven_by_a_stale_executor_after_takeover(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow, probe = build(tmp_path)
        run = await flow.new_run()
        original = flow.store.save_task_fenced
        taken = False

        async def take_over_first(task: AgentTask, epoch: Any) -> None:
            nonlocal taken
            if not taken:
                taken = True
                # A second executor acquires the lease (generation 2) before this write runs.
                flow.clock.advance(TTL + 1)
                second = await flow.leases.acquire(
                    key=f"runtime-run:{run.id}", owner_id="controller-2", ttl_seconds=TTL
                )
                assert second is not None and second.fencing_token == 2
            await original(task, epoch)

        flow.store.save_task_fenced = take_over_first  # type: ignore[method-assign]
        controller: WorkflowController = flow.fenced_controller()
        with pytest.raises(OwnershipLostError) as caught:
            await execute(flow, run, plan(spec("a", "w")), controller)
        assert caught.value.code == "lease_superseded"  # takeover, not merely expiry
        assert probe.ran == [] and await rows(flow, run) == {}

    run_async(scenario)


def test_after_one_refusal_the_scheduler_attempts_no_further_write_at_all(tmp_path: Path) -> None:
    """The refusal is remembered: later cleanup, failure or cancellation writes are never tried,
    even if the store would (wrongly or transiently) accept them."""

    async def scenario() -> None:
        flow, probe = build(tmp_path)
        run = await flow.new_run()
        original = flow.store.save_task_fenced
        attempts: list[tuple[str, TaskStatus]] = []
        refused_once = False

        async def refuse_the_first_running_write(task: AgentTask, epoch: Any) -> None:
            nonlocal refused_once
            attempts.append((str(task.metadata["logical_task_id"]), task.status))
            if task.status is TaskStatus.RUNNING and not refused_once:
                refused_once = True
                raise OwnershipLostError("lease_superseded")
            await original(task, epoch)

        flow.store.save_task_fenced = refuse_the_first_running_write  # type: ignore[method-assign]
        workflow = plan(spec("a", "w"), spec("hang", "w"))
        with pytest.raises(OwnershipLostError):
            await asyncio.wait_for(execute(flow, run, workflow), 20)
        refusal_at = next(
            i for i, (_, status) in enumerate(attempts) if status is TaskStatus.RUNNING
        )
        assert attempts[refusal_at + 1 :] == [] or all(
            status is not TaskStatus.RUNNING for _, status in attempts[refusal_at + 1 :]
        )
        # Nothing was written after the refusal: no FAILED, CANCELLED or COMPLETED row exists.
        assert not (set((await rows(flow, run)).values()) & FINAL)
        assert len(attempts) == refusal_at + 1  # not a single further attempt

    run_async(scenario)


def test_a_refusal_raised_by_a_worker_is_ownership_loss_not_a_task_failure(
    tmp_path: Path,
) -> None:
    """A refused tool claim reaches the scheduler as the worker's exception. It must stop the run:
    no FAILED record, no retry, even though the task store itself would accept those writes."""

    async def scenario() -> None:
        flow, probe = build(tmp_path)
        run = await flow.new_run()
        with pytest.raises(OwnershipLostError) as caught:
            await asyncio.wait_for(
                execute(flow, run, plan(spec("denied", "w", **RETRY), spec("b", "w"))), 20
            )
        assert caught.value.code == "lease_superseded"
        assert probe.ran.count("denied") == 1  # never re-dispatched as a retry
        assert (await rows(flow, run))["denied"] is TaskStatus.RUNNING  # not FAILED, not READY

    run_async(scenario)

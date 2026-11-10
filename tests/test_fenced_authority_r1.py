"""Audit R1, findings A and C: no ordinary call can bypass the epoch of a fenced run (ADR 0016).

A: run-state transitions. C: action-intent finalization. Recovery keeps a narrow authority of its
own: it must hold the run's lease, and with it can only block the run or mark an EXECUTING action
uncertain. It can never complete a run, finish an action or record a receipt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kaiwen_agent.coordination import OwnerEpoch
from kaiwen_agent.exceptions import OwnershipLostError
from kaiwen_agent.runtime import RuntimeRunStatus
from kaiwen_agent.tools.durability import ActionIntentStatus
from kaiwen_agent.types import ToolResult
from tests.test_agent_runtime_worker import Flow, owner
from tests.test_fenced_audit_r1 import intent_for
from tests.test_fenced_writes import TTL, FencedFlow, run_async

# ------------------------------------------------------------------ A: run-state transitions


async def lost_run(tmp_path: Path) -> tuple[FencedFlow, Any, OwnerEpoch]:
    """A fenced run that is RUNNING under an epoch whose lease has expired."""

    flow = FencedFlow(tmp_path)
    run = await flow.new_run()
    _, epoch = await flow.owned(run)
    flow.clock.advance(TTL + 1)
    return flow, run, epoch


async def reconcile(flow: FencedFlow) -> Any:
    return await flow.recover()


@pytest.mark.parametrize("transition", ["complete", "fail", "cancel", "pause", "block"])
def test_no_ordinary_transition_can_finish_a_fenced_running_run(
    tmp_path: Path, transition: str
) -> None:
    async def scenario() -> None:
        flow, run, _ = await lost_run(tmp_path)
        who = {"run_id": run.id, "identity": owner()}
        calls = {
            "complete": lambda: flow.runs.complete(**who),
            "fail": lambda: flow.runs.fail(reason="x", **who),
            "cancel": lambda: flow.runs.cancel(**who),
            "pause": lambda: flow.runs.pause(safe_boundary="b", **who),
            "block": lambda: flow.runs.block(reason="x", **who),
        }
        with pytest.raises(OwnershipLostError) as caught:
            await calls[transition]()
        assert caught.value.code == "fenced_run_requires_epoch"
        assert await flow.status(run) is RuntimeRunStatus.RUNNING

    run_async(scenario)


def test_the_fenced_flag_cannot_be_cleared_or_forged_by_a_direct_store_update(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        flow, run, _ = await lost_run(tmp_path)
        record = await flow.runs.get(run_id=run.id, identity=owner())
        assert record is not None and record.fenced
        for update in (
            {"fenced": False},
            {"fenced": False, "owner_fencing_token": 99},
            {"status": RuntimeRunStatus.COMPLETED, "fenced": False},
        ):
            with pytest.raises(OwnershipLostError):
                await flow.store.save_runtime_run(record.model_copy(update=update))
        # Once recovery blocked the run, clearing the flag is still refused, and for that reason.
        await reconcile(flow)
        blocked = await flow.runs.get(run_id=run.id, identity=owner())
        assert blocked is not None and blocked.status is RuntimeRunStatus.BLOCKED
        with pytest.raises(OwnershipLostError) as caught:
            await flow.store.save_runtime_run(
                blocked.model_copy(update={"fenced": False, "status": RuntimeRunStatus.CANCELLED})
            )
        assert caught.value.code == "fenced_flag_immutable"
        stored = await flow.runs.get(run_id=run.id, identity=owner())
        assert stored is not None and stored.fenced  # still flagged, still blocked, not cancelled
        assert stored.status is RuntimeRunStatus.BLOCKED
        # And the flag cannot be set on a run by anything but a fenced start.
        other = await flow.new_run()
        plain = await flow.runs.get(run_id=other.id, identity=owner())
        assert plain is not None
        with pytest.raises(OwnershipLostError):
            await flow.store.save_runtime_run(plain.model_copy(update={"fenced": True}))

    run_async(scenario)


def test_recovery_blocks_a_fenced_run_whose_owner_is_gone_and_can_do_nothing_else(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        flow, run, _ = await lost_run(tmp_path)
        report = await reconcile(flow)
        assert report.owner_lost_run_ids == (run.id,)
        assert await flow.status(run) is RuntimeRunStatus.BLOCKED
        # The recovery authority is a lease it really holds, and it can only block.
        flow2, run2, _ = await lost_run(tmp_path / "second")
        probe = await flow2.leases.acquire(
            key=f"runtime-run:{run2.id}", owner_id="recovery", ttl_seconds=TTL
        )
        assert probe is not None
        recovery = await flow2.issue(probe, run2)
        record = await flow2.runs.get(run_id=run2.id, identity=owner())
        assert record is not None
        for status in (
            RuntimeRunStatus.COMPLETED,
            RuntimeRunStatus.FAILED,
            RuntimeRunStatus.PAUSED,
        ):
            with pytest.raises(OwnershipLostError) as caught:
                await flow2.store.save_runtime_run(
                    record.model_copy(update={"status": status}), recovery_epoch=recovery
                )
            assert caught.value.code == "recovery_may_only_block"
        assert await flow2.status(run2) is RuntimeRunStatus.RUNNING

    run_async(scenario)


def test_recovery_leaves_a_live_fenced_owner_alone(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        run = await flow.new_run()
        _, epoch = await flow.owned(run)  # the lease is live
        await flow.store.prepare_action_intent(intent_for(run), owner_epoch=epoch)
        await flow.store.transition_action_intent(
            intent_id="intent-1",
            tenant_id="tenant-1",
            user_id="user-1",
            expected=frozenset({ActionIntentStatus.PREPARED}),
            status=ActionIntentStatus.EXECUTING,
            owner_epoch=epoch,
        )
        report = await reconcile(flow)
        assert report.uncertain_action_ids == () and report.blocked_run_ids == ()
        assert report.owner_lost_run_ids == ()
        assert await flow.status(run) is RuntimeRunStatus.RUNNING
        stored = await flow.store.get_action_intent(
            intent_id="intent-1", tenant_id="tenant-1", user_id="user-1"
        )
        assert stored is not None and stored.status is ActionIntentStatus.EXECUTING

    run_async(scenario)


def test_non_fenced_runs_keep_their_ordinary_transitions(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow = Flow(tmp_path)
        run = await flow.new_run()
        await flow.runs.start(run_id=run.id, identity=owner(), owner_lease_key="k", owner_fence=1)
        await flow.runs.block(run_id=run.id, identity=owner(), reason="operator")
        cancelled = await flow.runs.cancel(run_id=run.id, identity=owner())
        assert cancelled.status is RuntimeRunStatus.CANCELLED and cancelled.fenced is False

    run_async(scenario)


# ------------------------------------------------------------------ C: action-intent finalization


async def running_intents(tmp_path: Path) -> tuple[FencedFlow, Any, OwnerEpoch]:
    """A fenced run with one EXECUTING and one PREPARED intent; then the lease expires."""

    flow = FencedFlow(tmp_path)
    run = await flow.new_run()
    _, epoch = await flow.owned(run)
    for intent_id in ("intent-1", "intent-2"):
        await flow.store.prepare_action_intent(intent_for(run, intent_id), owner_epoch=epoch)
    await flow.store.transition_action_intent(
        intent_id="intent-1",
        tenant_id="tenant-1",
        user_id="user-1",
        expected=frozenset({ActionIntentStatus.PREPARED}),
        status=ActionIntentStatus.EXECUTING,
        owner_epoch=epoch,
    )
    flow.clock.advance(TTL + 1)
    return flow, run, epoch


async def intent_status(flow: FencedFlow, intent_id: str) -> ActionIntentStatus:
    stored = await flow.store.get_action_intent(
        intent_id=intent_id, tenant_id="tenant-1", user_id="user-1"
    )
    assert stored is not None
    return stored.status


def test_no_ordinary_call_can_finalize_the_intent_of_a_fenced_running_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow, _, _ = await running_intents(tmp_path)
        result = ToolResult(call_id="call-1", name="demo.write", output={"ok": True})
        attempts = (
            ("intent-1", {ActionIntentStatus.EXECUTING}, ActionIntentStatus.SUCCEEDED, result),
            ("intent-1", {ActionIntentStatus.EXECUTING}, ActionIntentStatus.FAILED, None),
            ("intent-1", {ActionIntentStatus.EXECUTING}, ActionIntentStatus.UNCERTAIN, None),
            ("intent-2", {ActionIntentStatus.PREPARED}, ActionIntentStatus.CANCELLED, None),
            ("intent-2", {ActionIntentStatus.PREPARED}, ActionIntentStatus.EXECUTING, None),
        )
        for intent_id, expected, status, outcome in attempts:
            with pytest.raises(OwnershipLostError) as caught:
                await flow.store.transition_action_intent(
                    intent_id=intent_id,
                    tenant_id="tenant-1",
                    user_id="user-1",
                    expected=frozenset(expected),
                    status=status,
                    result=outcome,
                )
            assert caught.value.code == "fenced_run_requires_epoch"
        # Nothing was recorded: the unknown outcome stays recoverable, no false receipt exists.
        assert await intent_status(flow, "intent-1") is ActionIntentStatus.EXECUTING
        assert await intent_status(flow, "intent-2") is ActionIntentStatus.PREPARED

    run_async(scenario)


def test_recovery_marks_an_orphaned_fenced_action_uncertain_but_cannot_finalize_it(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        flow, run, _ = await running_intents(tmp_path)
        probe = await flow.leases.acquire(
            key=f"runtime-run:{run.id}", owner_id="recovery", ttl_seconds=TTL
        )
        assert probe is not None
        recovery = await flow.issue(probe, run)
        with pytest.raises(OwnershipLostError) as caught:
            await flow.store.transition_action_intent(
                intent_id="intent-1",
                tenant_id="tenant-1",
                user_id="user-1",
                expected=frozenset({ActionIntentStatus.EXECUTING}),
                status=ActionIntentStatus.SUCCEEDED,
                result=ToolResult(call_id="c", name="demo.write", output={}),
                recovery_epoch=recovery,
            )
        assert caught.value.code == "recovery_may_only_mark_uncertain"
        await flow.leases.release(probe)
        report = await reconcile(flow)
        assert len(report.uncertain_action_ids) == 1 and report.blocked_run_ids == (run.id,)
        assert await intent_status(flow, "intent-1") is ActionIntentStatus.UNCERTAIN
        assert await flow.status(run) is RuntimeRunStatus.BLOCKED

    run_async(scenario)


def test_once_the_run_is_blocked_an_operator_can_reconcile_an_uncertain_action(
    tmp_path: Path,
) -> None:
    """Operator reconciliation keeps its existing authority: the run is no longer RUNNING."""

    async def scenario() -> None:
        flow, _, _ = await running_intents(tmp_path)
        await reconcile(flow)
        resolved = await flow.store.transition_action_intent(
            intent_id="intent-1",
            tenant_id="tenant-1",
            user_id="user-1",
            expected=frozenset({ActionIntentStatus.UNCERTAIN}),
            status=ActionIntentStatus.FAILED,
            error_code="operator_reconciled",
        )
        assert resolved is not None and resolved.status is ActionIntentStatus.FAILED

    run_async(scenario)


def test_not_even_the_live_owner_can_clear_the_flag_or_use_the_recovery_path(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        run = await flow.new_run()
        _, epoch = await flow.owned(run)  # a live owner
        record = await flow.runs.get(run_id=run.id, identity=owner())
        assert record is not None
        with pytest.raises(OwnershipLostError) as caught:
            await flow.store.save_runtime_run(
                record.model_copy(update={"status": RuntimeRunStatus.COMPLETED, "fenced": False}),
                owner_epoch=epoch,
            )
        assert caught.value.code == "fenced_flag_immutable"
        # Recovery's authority is a lease newer than the owner's. The owner's own epoch is not.
        with pytest.raises(OwnershipLostError) as caught:
            await flow.store.save_runtime_run(
                record.model_copy(update={"status": RuntimeRunStatus.BLOCKED}),
                recovery_epoch=epoch,
            )
        assert caught.value.code == "epoch_regressed"
        assert await flow.status(run) is RuntimeRunStatus.RUNNING

    run_async(scenario)

"""Audit R1 for the fenced execution foundation (ADR 0016).

B: lease expiry is judged at the protected transaction, not at an earlier sample.
A: no ordinary run-state transition can bypass the epoch of a fenced run.
C: no ordinary call can finalize the intent of a fenced run; recovery keeps a narrow authority.

Deterministic: an injected clock, a second SQLite connection holding the write lock, and events.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from kaiwen_agent.exceptions import OwnershipLostError
from kaiwen_agent.runtime import RuntimeRunStatus
from kaiwen_agent.tools.durability import ActionIntentRecord, ActionIntentStatus
from tests.test_agent_runtime_worker import owner
from tests.test_fenced_writes import TTL, FencedFlow, run_async


def intent_for(run: Any, intent_id: str = "intent-1") -> ActionIntentRecord:
    return ActionIntentRecord(
        id=intent_id,
        runtime_run_id=run.id,
        tenant_id="tenant-1",
        user_id="user-1",
        conversation_id="conversation-1",
        tool_name="demo.write",
        call_id="call-1",
        arguments_checksum="a" * 64,
        idempotency_key=f"{run.id}:{intent_id}",
        side_effect="reversible",
    )


async def expiry_while_waiting_for_the_lock(
    flow: FencedFlow, call: Callable[[], Awaitable[Any]]
) -> BaseException | None:
    """Run `call` while another connection holds the write lock; the lease expires meanwhile.

    The lock is released only after the clock passed the lease expiry. An implementation that
    sampled the clock before waiting for the lock sees a live lease; one that samples inside its
    transaction sees an expired one. Returns the exception `call` raised, if any.
    """

    sampled = threading.Event()
    inner = flow.clock

    def spying_clock() -> Any:
        sampled.set()
        return inner()

    flow.store.lease_clock = spying_clock
    blocker = sqlite3.connect(flow.store.path)
    blocker.execute("BEGIN IMMEDIATE")  # another writer holds the database
    try:
        pending = asyncio.ensure_future(call())
        # An early sampler reads the clock at once; a late one has not been able to yet.
        await asyncio.get_running_loop().run_in_executor(None, sampled.wait, 0.5)
        flow.clock.advance(TTL + 1)
    finally:
        blocker.rollback()
        blocker.close()
    try:
        await asyncio.wait_for(pending, timeout=30)
    except BaseException as error:  # noqa: BLE001 - the outcome is the assertion
        return error
    return None


def expect_refused(error: BaseException | None, code: str = "lease_expired") -> None:
    assert isinstance(error, OwnershipLostError), error
    assert error.code == code


def test_a_task_write_judges_expiry_at_its_transaction(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        run = await flow.new_run()
        _, epoch = await flow.owned(run)
        error = await expiry_while_waiting_for_the_lock(
            flow, lambda: flow.store.save_task_fenced(flow.task(run), epoch)
        )
        expect_refused(error)
        assert await flow.stored_task("task_1") is None

    run_async(scenario)


def test_a_run_transition_judges_expiry_at_its_transaction(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        run = await flow.new_run()
        _, epoch = await flow.owned(run)
        error = await expiry_while_waiting_for_the_lock(
            flow, lambda: flow.runs.complete(run_id=run.id, identity=owner(), owner_epoch=epoch)
        )
        expect_refused(error)
        assert await flow.status(run) is RuntimeRunStatus.RUNNING

    run_async(scenario)


def test_intent_preparation_judges_expiry_at_its_transaction(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        run = await flow.new_run()
        _, epoch = await flow.owned(run)
        error = await expiry_while_waiting_for_the_lock(
            flow, lambda: flow.store.prepare_action_intent(intent_for(run), owner_epoch=epoch)
        )
        expect_refused(error)
        assert (
            await flow.store.list_action_intents(statuses=frozenset(ActionIntentStatus), limit=10)
            == []
        )

    run_async(scenario)


def test_the_intent_claim_judges_expiry_at_its_transaction(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        run = await flow.new_run()
        _, epoch = await flow.owned(run)
        await flow.store.prepare_action_intent(intent_for(run), owner_epoch=epoch)

        def claim() -> Awaitable[Any]:
            return flow.store.transition_action_intent(
                intent_id="intent-1",
                tenant_id="tenant-1",
                user_id="user-1",
                expected=frozenset({ActionIntentStatus.PREPARED}),
                status=ActionIntentStatus.EXECUTING,
                owner_epoch=epoch,
            )

        expect_refused(await expiry_while_waiting_for_the_lock(flow, claim))
        stored = await flow.store.get_action_intent(
            intent_id="intent-1", tenant_id="tenant-1", user_id="user-1"
        )
        assert stored is not None and stored.status is ActionIntentStatus.PREPARED

    run_async(scenario)


def test_renewal_does_not_revive_a_lease_that_expired_while_waiting(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        run = await flow.new_run()
        lease, _ = await flow.owned(run)
        outcomes: list[Any] = []

        async def renew() -> None:
            outcomes.append(await flow.leases.renew(lease, ttl_seconds=TTL))

        error = await expiry_while_waiting_for_the_lock(flow, renew)
        assert error is None
        assert outcomes == [None]  # refused: a lease that expired while waiting is not renewed
        flow.store.lease_clock = flow.clock
        assert await flow.leases.renew(lease, ttl_seconds=TTL) is None  # still expired

    run_async(scenario)


def test_issuing_an_epoch_judges_expiry_at_its_transaction(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        run = await flow.new_run()
        lease = await flow.leases.acquire(
            key=f"runtime-run:{run.id}", owner_id="a", ttl_seconds=TTL
        )
        assert lease is not None
        error = await expiry_while_waiting_for_the_lock(flow, lambda: flow.issue(lease, run))
        expect_refused(error)

    run_async(scenario)


def test_acquisition_judges_the_previous_holders_expiry_at_its_transaction(
    tmp_path: Path,
) -> None:
    """The mirror case: a lease that expired while waiting for the lock can be taken, and the
    grant carries the epoch and times of the transaction, not of an earlier sample."""

    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        run = await flow.new_run()
        await flow.owned(run)
        key = f"runtime-run:{run.id}"
        before = flow.clock.now
        result: list[Any] = []

        async def take() -> Any:
            lease = await flow.leases.acquire(key=key, owner_id="b", ttl_seconds=TTL)
            result.append(lease)

        error = await expiry_while_waiting_for_the_lock(flow, take)
        assert error is None and result[0] is not None
        assert result[0].fencing_token == 2
        assert result[0].acquired_at > before  # stamped at the transaction, after the expiry

    run_async(scenario)

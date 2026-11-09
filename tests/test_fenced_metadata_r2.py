"""Final audit: the fenced ownership metadata of a run is immutable outside a publication.

Outside an explicitly authorized ownership publication (a fenced start that publishes a strictly
higher epoch), the persisted `owner_lease_key` and `owner_fencing_token` of a fenced run must not
change, whoever writes: an ordinary store update, an operator transition, or even the live owner.
Operator cancel and fail of an idle (blocked) fenced run keep working without touching them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kaiwen_agent.exceptions import OwnershipLostError
from kaiwen_agent.runtime import RuntimeRunStatus
from tests.test_agent_runtime_worker import owner
from tests.test_fenced_authority_r1 import lost_run, reconcile
from tests.test_fenced_writes import FencedFlow, run_async


async def blocked_fenced_run(tmp_path: Path) -> tuple[FencedFlow, Any, Any]:
    flow, run, _ = await lost_run(tmp_path)
    await reconcile(flow)  # recovery blocks the orphaned run
    record = await flow.runs.get(run_id=run.id, identity=owner())
    assert record is not None and record.status is RuntimeRunStatus.BLOCKED and record.fenced
    return flow, run, record


async def metadata(flow: FencedFlow, run: Any) -> tuple[Any, Any]:
    record = await flow.runs.get(run_id=run.id, identity=owner())
    assert record is not None
    return record.owner_fencing_token, record.owner_lease_key


@pytest.mark.parametrize(
    "change",
    [
        {"owner_fencing_token": 2},  # forge a different generation (1 is the published one)
        {"owner_fencing_token": 99},
        {"owner_lease_key": "runtime-run:other"},
        {"owner_lease_key": None},
        {"owner_fencing_token": None, "owner_lease_key": None},
    ],
)
def test_an_ordinary_update_cannot_change_the_ownership_of_a_blocked_fenced_run(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    async def scenario() -> None:
        flow, run, record = await blocked_fenced_run(tmp_path)
        before = await metadata(flow, run)
        for status in (RuntimeRunStatus.BLOCKED, RuntimeRunStatus.CANCELLED):
            with pytest.raises(OwnershipLostError) as caught:
                await flow.store.save_runtime_run(
                    record.model_copy(update={**change, "status": status})
                )
            assert caught.value.code == "fenced_owner_immutable"
        assert await metadata(flow, run) == before

    run_async(scenario)


def test_operator_cancel_and_fail_of_an_idle_fenced_run_leave_ownership_unchanged(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        for transition in ("cancel", "fail"):
            flow, run, _ = await blocked_fenced_run(tmp_path / transition)
            before = await metadata(flow, run)
            who = {"run_id": run.id, "identity": owner()}
            done = (
                await flow.runs.cancel(**who)
                if transition == "cancel"
                else await flow.runs.fail(reason="operator", **who)
            )
            assert done.status.value in {"cancelled", "failed"} and done.fenced
            assert await metadata(flow, run) == before

    run_async(scenario)


def test_a_live_owner_cannot_change_the_lease_key_or_generation_in_a_transition(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        flow = FencedFlow(tmp_path)
        run = await flow.new_run()
        _, epoch = await flow.owned(run)
        record = await flow.runs.get(run_id=run.id, identity=owner())
        assert record is not None
        before = await metadata(flow, run)
        for change in ({"owner_lease_key": "runtime-run:other"}, {"owner_fencing_token": 7}):
            with pytest.raises(OwnershipLostError):
                await flow.store.save_runtime_run(
                    record.model_copy(update={**change, "status": RuntimeRunStatus.COMPLETED}),
                    owner_epoch=epoch,
                )
        assert await metadata(flow, run) == before
        assert (await flow.runs.get(run_id=run.id, identity=owner())).status is (  # type: ignore[union-attr]
            RuntimeRunStatus.RUNNING
        )

    run_async(scenario)


def test_recovery_still_blocks_with_a_newer_epoch_and_keeps_the_published_owner(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        flow, run, _ = await lost_run(tmp_path)
        before = await metadata(flow, run)
        await reconcile(flow)
        assert await metadata(flow, run) == before  # blocking never republishes ownership
        # And a later fenced start still needs a strictly higher epoch than the published one.
        flow.clock.advance(1)
        _, new = await flow.owned(run, owner_id="b")
        assert new.epoch > before[0]

    run_async(scenario)

"""PR #14 audit R2: the authorization-to-dispatch window, lease-error logging, replay semantics.

Finding 1 is a reproduced limit of the current contracts, not a fixed defect. A lease check
(`renew`) is a check at one instant. Nothing in `TaskScheduler`, `TaskStore.save_task` or the
SQLite task write carries or enforces the owner's fencing token, so a controller that lost the lease
after its last check still persists and executes the delegated child. The test below states the
required behavior and is a strict `xfail`: it documents the missing primitive and will fail loudly
(XPASS) the day task persistence and dispatch are fenced, at which point the marker must go.
"""

from __future__ import annotations

import logging
import traceback
from pathlib import Path
from typing import Any

import pytest

from kaiwen_agent.exceptions import RunOwnershipError
from kaiwen_agent.tasks import TaskScheduler
from tests.test_agent_runtime_worker import owner
from tests.test_delegation_job_path import SECRET
from tests.test_workflow_delegation_cycle import Source, propose_for, run_async
from tests.test_workflow_delegation_r1 import lease_harness


@pytest.mark.xfail(
    strict=True,
    reason="no fenced task persistence or dispatch exists: a lease check is not an atomic guard",
)
def test_a_controller_that_loses_the_lease_after_its_last_check_must_not_start_the_child(
    tmp_path: Path,
) -> None:
    """Final ALLOW, a successful lease check, then B takes the lease before the scheduler starts."""

    async def scenario() -> None:
        harness, leases = lease_harness(tmp_path, source=Source(propose_for("lead")))
        run = await harness.new_run()
        key = f"runtime-run:{run.id}"
        token_before = (
            await harness.flow.runs.get(run_id=run.id, identity=owner())
        ).owner_fencing_token  # type: ignore[union-attr]
        original = harness.factory

        def factory(graph: Any) -> TaskScheduler:
            if len(harness.graphs) == 1:  # the delegated cycle: after the controller's last check
                leases.takeover_now(key)
            return original(graph)

        harness.factory = factory  # type: ignore[method-assign]
        with pytest.raises(RunOwnershipError):
            await harness.execute(run)
        record = await harness.flow.runs.get(run_id=run.id, identity=owner())
        assert record is not None and record.owner_fencing_token == token_before  # unchanged
        # Required: the stale owner neither persisted nor executed the delegated child.
        assert harness.ran == ["lead"]
        assert await harness.logical_ids(run) == ["lead"]

    run_async(scenario)


def test_the_unfenced_dispatch_outcome_is_exactly_what_the_audit_asked_to_see(
    tmp_path: Path,
) -> None:
    """The reported reproduction, pinned as a fact about the current contracts (not a guarantee)."""

    async def scenario() -> None:
        harness, leases = lease_harness(tmp_path, source=Source(propose_for("lead")))
        run = await harness.new_run()
        key = f"runtime-run:{run.id}"
        original = harness.factory

        def factory(graph: Any) -> TaskScheduler:
            if len(harness.graphs) == 1:
                leases.takeover_now(key)
            return original(graph)

        harness.factory = factory  # type: ignore[method-assign]
        # The controller finishes the delegated cycle under a lease it no longer holds: the child
        # was persisted and executed. Only the heartbeat (or a fenced run write) can notice later.
        outcome = await harness.execute(run)
        assert harness.ran == ["lead", "a"]
        assert await harness.logical_ids(run) == ["a", "lead"]
        assert outcome is not None

    run_async(scenario)


# ------------------------------------------------------------------ lease errors in logs


def test_a_secret_bearing_lease_error_reaches_neither_the_exception_nor_the_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        harness, leases = lease_harness(tmp_path)
        run = await harness.new_run()

        def flip(request: Any) -> Any:
            leases.fail_renew = True
            return propose_for("lead")(request)

        harness.source = Source(flip)
        with caplog.at_level(logging.DEBUG), pytest.raises(RunOwnershipError) as caught:
            await harness.execute(run)
        outward = "".join(traceback.format_exception(caught.value))
        assert SECRET not in outward
        records = [r for r in caplog.records if r.name.startswith("kaiwen_agent")]
        assert records, "the failure is still logged, with a stable code"
        for record in records:
            assert SECRET not in record.getMessage()
            assert SECRET not in "".join(
                traceback.format_exception(*record.exc_info) if record.exc_info else ()
            )
        assert SECRET not in caplog.text
        assert any("RuntimeError" in r.getMessage() for r in records)  # class name only

    run_async(scenario)

"""PR #14 audit R2: the authorization-to-dispatch window, lease-error logging, replay semantics.

Finding 1 was a reproduced limit: a lease check (`renew`) is a check at one instant, and nothing in
task persistence enforced the owner's fencing token, so a controller that lost the lease after its
last check still persisted and executed the delegated child. With fenced execution (ADR 0016) every
task write runs inside a transaction that re-reads the authoritative lease row, so the same
scenario now stops at the first protected write. The former strict `xfail` is the first test below,
unchanged in what it asserts.

What is still not claimed: a task or external operation authorized before the loss may have effects
afterwards, and nothing here is exactly-once.
"""

from __future__ import annotations

import logging
import traceback
from pathlib import Path
from typing import Any

import pytest

from kaiwen_agent.exceptions import FencedConfigurationError, OwnershipLostError, RunOwnershipError
from kaiwen_agent.runtime import DurableRunManager
from kaiwen_agent.workflow import WorkflowController
from tests.test_agent_runtime_worker import owner
from tests.test_delegation_job_path import SECRET
from tests.test_workflow_delegation_cycle import Harness, Source, propose_for, run_async
from tests.test_workflow_delegation_r1 import lease_harness


def test_a_controller_that_loses_the_lease_after_its_last_check_must_not_start_the_child(
    tmp_path: Path,
) -> None:
    """Final ALLOW, a successful lease check, then B takes the lease before the scheduler starts."""

    async def scenario() -> None:
        harness, leases = lease_harness(tmp_path, source=Source(propose_for("lead")))
        run = await harness.new_run()
        key = f"runtime-run:{run.id}"

        checks: list[int] = []
        original_renew = leases.renew

        async def renew(lease: Any, **options: Any) -> Any:
            renewed = await original_renew(lease, **options)
            checks.append(1)
            if renewed is not None and len(checks) == 2:
                # The boundary checks twice (after authorization and right before scheduling).
                # B takes over immediately after the second, A's last successful check.
                await leases.takeover(key)
            return renewed

        leases.renew = renew  # type: ignore[method-assign]
        with pytest.raises(RunOwnershipError) as caught:
            await harness.execute(run)
        assert isinstance(caught.value, OwnershipLostError)
        record = await harness.flow.runs.get(run_id=run.id, identity=owner())
        assert record is not None and record.owner_fencing_token == 1  # A's epoch; B never wrote
        # The stale owner neither persisted nor executed the delegated child.
        assert harness.ran == ["lead"]
        assert await harness.logical_ids(run) == ["lead"]
        assert harness.gate.calls == 0

    run_async(scenario)


def test_delegation_cannot_be_composed_without_fenced_execution(tmp_path: Path) -> None:
    """The unfenced dispatch the audit reproduced is no longer an assemblable configuration."""

    harness = Harness(tmp_path)
    with pytest.raises(FencedConfigurationError, match="requires fenced execution"):
        harness.controller(fenced=None)
    with pytest.raises(FencedConfigurationError, match="fenced lease store"):
        harness.controller(leases=None)
    other = DurableRunManager(harness.store)
    with pytest.raises(FencedConfigurationError, match="same durable runs"):
        harness.controller(admission=harness.admission(runs=other))
    assert isinstance(harness.controller(), WorkflowController)


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

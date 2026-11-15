"""M6.4-A: the credential-free coding-team example, run through its public composition.

`examples/coding-team-acceptance/main.py` composes Lead, Coder, Tester and Reviewer over the real
fenced stack (SQLite, leases, compiler, controller, scheduler), the trusted evidence pipeline, a
real completion gate and a real team acceptance. These tests run its scenarios deterministically
(scripted models, an injected lease clock, barriers instead of sleeps) and pin that only accepted
deliveries complete. Invariants already pinned elsewhere (see docs/coding-team-beta.md) are not
repeated; this file covers the composition itself.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from kaiwen_agent.exceptions import RunOwnershipError
from kaiwen_agent.workflow import WorkflowOutcomeStatus

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "coding-team-acceptance" / "main.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("coding_team_acceptance_example", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


example = load()


def run(coroutine: Any) -> Any:
    async def bounded() -> Any:
        return await asyncio.wait_for(coroutine, timeout=120)

    return asyncio.run(bounded())


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def named(name: str) -> Any:
    return next(s for s in example.SCENARIOS if s.name == name)


@pytest.mark.parametrize("scenario", example.SCENARIOS, ids=lambda s: s.name)
def test_every_documented_scenario_ends_as_documented(tmp_path: Path, scenario: Any) -> None:
    result = run(example.run_scenario(scenario, tmp_path))
    assert result.as_expected, (result.outcome.status, result.outcome.reason, result.acceptance)
    delivered = result.outcome.status is WorkflowOutcomeStatus.COMPLETED
    # Only a passing gate plus an accepting team acceptance completes the fenced run.
    assert (result.run_status == "completed") is delivered
    decision = result.outcome.team_acceptance
    if delivered:
        assert decision is not None and decision.accepted
        assert result.outcome.quality_result is not None
    else:
        assert result.run_status == "blocked" and result.outcome.reason is not None


def tasks_by_logical(result: Any) -> dict[str, Any]:
    return {str(task.metadata["logical_task_id"]): task for task in result.outcome.tasks}


def test_cross_cycle_delegation_and_reviewer_lineage(tmp_path: Path) -> None:
    result = run(example.run_scenario(named("accepted"), tmp_path))
    tasks = tasks_by_logical(result)
    cycles = {name: task.metadata["workflow_cycle"] for name, task in tasks.items()}
    assert cycles == {"lead": 1, "code": 2, "test": 2, "plan2": 2, "review": 3}
    record = tasks["review"].output["review_assessment"]
    assert [item["cycle"] for item in record["reviewed"]] == [2, 2]  # the proven source cycles
    assert record["reviewer_worker_id"] == "reviewer" and record["run_id"] == result.run_id
    # The decision relied on evidence the completion gate itself verified, never a model claim.
    decision = result.outcome.team_acceptance
    assert decision is not None and len(decision.evidence_refs) == 1
    gate_refs = {
        ref
        for verification in result.outcome.quality_result.verifications
        for ref in verification.evidence_refs
    }
    assert set(decision.evidence_refs) <= gate_refs


def test_same_cycle_dependencies_are_reviewed_with_the_proven_cycle(tmp_path: Path) -> None:
    result = run(example.run_scenario(named("same_cycle"), tmp_path))
    tasks = tasks_by_logical(result)
    assert {t.metadata["workflow_cycle"] for n, t in tasks.items() if n != "lead"} == {2}
    record = tasks["review"].output["review_assessment"]
    assert [item["cycle"] for item in record["reviewed"]] == [2, 2]


def test_reviewer_identity_is_the_host_worker_mapping(tmp_path: Path) -> None:
    result = run(example.run_scenario(named("accepted"), tmp_path))
    tasks = tasks_by_logical(result)
    assert tasks["review"].agent_id == "reviewer"
    assert all(tasks[name].agent_id != "reviewer" for name in ("code", "test", "plan2", "lead"))


def test_the_acceptance_is_opt_in_and_a_legacy_flow_keeps_its_behavior(tmp_path: Path) -> None:
    legacy = example.Scenario(
        "legacy",
        WorkflowOutcomeStatus.COMPLETED,
        None,
        team_acceptance=False,
        recommendation="changes_requested",
    )
    result = run(example.run_scenario(legacy, tmp_path))
    # Without team_acceptance only the completion gate decides, exactly as before M6.3.4.
    assert result.outcome.status is WorkflowOutcomeStatus.COMPLETED
    assert result.outcome.team_acceptance is None


def test_losing_ownership_during_acceptance_never_completes(tmp_path: Path) -> None:
    clock = Clock()
    approver = example.ExampleApprover("approve", on_ask=lambda: clock.advance(3600))
    with pytest.raises(RunOwnershipError):
        run(example.run_scenario(named("human_approved"), tmp_path, approver=approver, clock=clock))


class Barrier(example.ExampleApprover):
    def __init__(self) -> None:
        super().__init__("approve")
        self.started = asyncio.Event()

    async def decision(self, subject: Any) -> Any:
        self.started.set()
        await asyncio.Event().wait()


def test_cancellation_propagates_out_of_the_approval_wait(tmp_path: Path) -> None:
    async def scenario() -> None:
        approver = Barrier()
        pending = asyncio.ensure_future(
            example.run_scenario(named("human_approved"), tmp_path, approver=approver)
        )
        await approver.started.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

    run(scenario())


def test_a_blocked_run_can_be_replayed_and_is_decided_again_from_storage(tmp_path: Path) -> None:
    result = run(example.run_scenario(named("approval_denied"), tmp_path))
    assert result.outcome.reason == "team_blocked:human_denied"
    assert result.approver is not None
    result.approver.mode = "approve"  # the human changed their mind; the decision is recomputed
    again = run(result.replay())
    assert again.status is WorkflowOutcomeStatus.COMPLETED
    assert again.team_acceptance is not None and again.team_acceptance.accepted


def test_a_blocked_scenario_with_the_wrong_reason_is_not_as_expected(tmp_path: Path) -> None:
    wrong = example.Scenario(
        "changes_requested",
        WorkflowOutcomeStatus.BLOCKED,
        "team_blocked:something_else",
        recommendation="changes_requested",
    )
    result = run(example.run_scenario(wrong, tmp_path))
    assert result.outcome.status is WorkflowOutcomeStatus.BLOCKED
    assert not result.as_expected  # the reason is part of the expectation


def test_the_example_main_reports_failure_when_a_scenario_misbehaves(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(example.main_async(["accepted"])) == 0
    wrong = example.Scenario("accepted", WorkflowOutcomeStatus.BLOCKED, "nonsense")
    monkeypatch.setattr(example, "SCENARIOS", (wrong,))
    assert run(example.main_async([])) == 1  # a mismatch is a failure, never a quiet success
    output = capsys.readouterr().out
    assert "[BAD]" in output and "did not end as documented" in output
    assert run(example.main_async(["no-such-scenario"])) == 2

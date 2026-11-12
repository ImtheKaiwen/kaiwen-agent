"""M6.3.4 audit R1: workspace freshness at completion, same-cycle reviews, evidence authority.

Finding A: the workspace may change while a human approval is awaited; the run must not complete
against the old fingerprint. Finding B: a reviewer whose reviewed work was a same-cycle dependency
must be accepted (the cycle is the authoritative one, never a model or label value). Finding C:
acceptance must verify evidence with the very artifacts and signer the completion gate uses.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from kaiwen_agent.artifacts import ArtifactManager
from kaiwen_agent.exceptions import FencedConfigurationError, RunOwnershipError
from kaiwen_agent.kits.coding import EvidenceSigner
from kaiwen_agent.persistence import SQLitePersistence
from kaiwen_agent.quality import CompletionGate, VerifierRegistry
from kaiwen_agent.runtime import RuntimeRunStatus
from kaiwen_agent.types import ModelResponse
from kaiwen_agent.workflow import (
    HumanApproval,
    HumanApprovalStatus,
    TaskContextAssembler,
    TeamAcceptance,
    WorkflowOutcomeStatus,
)
from kaiwen_agent.workflow import review as rv
from tests.test_agent_runtime_worker import _artifact_context, _evidence, owner
from tests.test_cross_cycle_e2e import SimpleArtifacts, view_call
from tests.test_lead_tools import blocked, done, proposal_call, run_async
from tests.test_team_acceptance_e2e import Human, TeamHarness, team_blocked
from tests.test_workflow_fresh_evidence import SIGNER


class GatedHuman:
    """A human approval source whose answer is released by the test (deterministic barrier)."""

    def __init__(self, *, fail: bool = False) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.subjects: list[Any] = []
        self.fail = fail

    async def decision(self, subject: Any) -> HumanApproval:
        self.subjects.append(subject)
        self.started.set()
        await self.release.wait()
        if self.fail:
            raise RuntimeError("source went away")
        return HumanApproval(status=HumanApprovalStatus.APPROVED, subject_digest=subject.digest)


def same_cycle_scripts(harness: TeamHarness, **review: Any) -> dict[str, list[Any]]:
    """Lead (cycle 1) proposes code, test (after code) and review (after both): one cycle 2."""

    return {
        "lead": [
            view_call(),
            ModelResponse(
                tool_calls=[
                    *proposal_call("p1", id="code", name="Implement", role="coder").tool_calls,
                    *proposal_call(
                        "p2", id="test", name="Verify", role="tester", dependencies=["code"]
                    ).tool_calls,
                    *proposal_call(
                        "p3",
                        id="review",
                        name="Review the delivery",
                        role="reviewer",
                        dependencies=["code", "test"],
                    ).tool_calls,
                ]
            ),
            done("planned"),
        ],
        "review": [harness.review_step(**review), done("reviewed")],
    }


# ------------------------------------------------------------------------------- finding A


def test_a_workspace_change_while_a_human_approval_is_pending_never_completes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        human = GatedHuman()
        harness = TeamHarness(tmp_path, human=human)
        run = await harness.flow.new_run()
        pending = asyncio.ensure_future(harness.execute(run))
        await human.started.wait()  # team acceptance began and is awaiting the approval source
        harness.fingerprints.value = "fp-2"  # the protected workspace changes meanwhile
        human.release.set()  # an otherwise valid approval for the OLD subject
        outcome = await pending
        blocked(outcome, "workspace_changed_after_acceptance")
        assert await harness.status(run) is RuntimeRunStatus.BLOCKED
        assert outcome.team_acceptance is not None and outcome.team_acceptance.accepted
        assert human.subjects[0].fingerprint == "fp-1"  # the approval was for the earlier state

    run_async(scenario)


def test_an_unchanged_workspace_completes_after_a_late_human_approval(tmp_path: Path) -> None:
    async def scenario() -> None:
        human = GatedHuman()
        harness = TeamHarness(tmp_path, human=human)
        run = await harness.flow.new_run()
        pending = asyncio.ensure_future(harness.execute(run))
        await human.started.wait()
        human.release.set()
        outcome = await pending
        assert outcome.status is WorkflowOutcomeStatus.COMPLETED
        assert outcome.verified_input_fingerprint == "fp-1"
        assert await harness.status(run) is RuntimeRunStatus.COMPLETED

    run_async(scenario)


def test_an_unavailable_fingerprint_after_acceptance_fails_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        human = GatedHuman()
        harness = TeamHarness(tmp_path, human=human)
        run = await harness.flow.new_run()
        pending = asyncio.ensure_future(harness.execute(run))
        await human.started.wait()

        async def broken(*, run: Any, identity: Any) -> str:
            raise RuntimeError("workspace unreadable")

        harness.fingerprints.current = broken  # type: ignore[method-assign]
        human.release.set()
        blocked(await pending, "protected_input_unavailable")
        assert await harness.status(run) is RuntimeRunStatus.BLOCKED

    run_async(scenario)


def test_a_changed_workspace_cannot_reuse_the_approval_of_an_earlier_fingerprint(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        first = Human(HumanApprovalStatus.APPROVED)
        harness = TeamHarness(tmp_path, human=first)
        run = await harness.flow.new_run()
        assert (await harness.execute(run)).status is WorkflowOutcomeStatus.COMPLETED
        old = first.asked[0]
        # A later fingerprint produces a different subject, so an echoed old digest cannot match.
        second = Human(HumanApprovalStatus.APPROVED)
        again = TeamHarness(tmp_path / "again", human=second)
        run2 = await again.flow.new_run()
        again.fingerprints.value = "fp-9"
        again.provider.scripts["review"] = [again.review_step(), done()]
        await again.execute(run2)
        assert second.asked and second.asked[0].digest != old.digest

        class Stale:
            async def decision(self, subject: Any) -> HumanApproval:
                return HumanApproval(status=HumanApprovalStatus.APPROVED, subject_digest=old.digest)

        stale = TeamHarness(tmp_path / "stale", human=Stale())
        run3 = await stale.flow.new_run()
        stale.fingerprints.value = "fp-9"
        outcome = await stale.execute(run3)
        team_blocked(outcome, "blocked", "approval_mismatch")

    run_async(scenario)


def test_ownership_loss_while_awaiting_approval_raises_and_never_completes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        human = GatedHuman()
        harness = TeamHarness(tmp_path, human=human)
        run = await harness.flow.new_run()
        pending = asyncio.ensure_future(harness.execute(run))
        await human.started.wait()
        harness.flow.clock.advance(3600)  # the lease expires while the approval is awaited
        human.release.set()
        with pytest.raises(RunOwnershipError):
            await pending
        assert await harness.status(run) is not RuntimeRunStatus.COMPLETED

    run_async(scenario)


def test_a_cancelled_approval_lookup_propagates_and_never_completes(tmp_path: Path) -> None:
    async def scenario() -> None:
        human = GatedHuman()
        harness = TeamHarness(tmp_path, human=human)
        run = await harness.flow.new_run()
        pending = asyncio.ensure_future(harness.execute(run))
        await human.started.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert await harness.status(run) is not RuntimeRunStatus.COMPLETED

    run_async(scenario)


# ------------------------------------------------------------------------------- finding B


def test_a_same_cycle_review_of_its_dependencies_is_accepted(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = TeamHarness(tmp_path)
        harness.provider.scripts.update(same_cycle_scripts(harness))
        run = await harness.flow.new_run()
        outcome = await harness.execute(run)
        assert outcome.status is WorkflowOutcomeStatus.COMPLETED and outcome.cycles == 2
        assert outcome.team_acceptance is not None and outcome.team_acceptance.accepted
        tasks = await harness.stored(run)
        assert {t.metadata["workflow_cycle"] for t in tasks.values() if t.id != ""} >= {1, 2}
        record = tasks["review"].output[rv.REVIEW_OUTPUT_KEY]
        # The record carries the authoritative, host-derived cycle of each reviewed task.
        assert [item["cycle"] for item in record["reviewed"]] == [2, 2]

    run_async(scenario)


def test_a_same_cycle_review_still_detects_changed_and_missing_work(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = TeamHarness(tmp_path)
        harness.provider.scripts.update(same_cycle_scripts(harness, reviewed=("test",)))
        run = await harness.flow.new_run()
        outcome = await harness.execute(run)
        # Reviewing only `test` does not cover the required `code`: that is still refused.
        team_blocked(outcome, "changes_required", "review_coverage_incomplete")

    run_async(scenario)


def test_a_cross_cycle_review_keeps_working_and_records_the_earlier_cycle(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = TeamHarness(tmp_path)
        run = await harness.flow.new_run()
        outcome = await harness.execute(run)
        assert outcome.status is WorkflowOutcomeStatus.COMPLETED
        record = (await harness.stored(run))["review"].output[rv.REVIEW_OUTPUT_KEY]
        assert [item["cycle"] for item in record["reviewed"]] == [2, 2]

    run_async(scenario)


# ------------------------------------------------------------------------------- finding C


def with_assembler(harness: TeamHarness, **changes: Any) -> TeamAcceptance:
    good = harness.acceptance()
    base: dict[str, Any] = {
        "store": harness.flow.store,
        "artifacts": harness.artifacts,
        "evidence_signer": SIGNER,
    }
    base.update(changes)
    return TeamAcceptance(
        policy=good.policy, tasks=good.tasks, runs=good.runs, assembler=TaskContextAssembler(**base)
    )


def test_acceptance_evidence_must_use_the_gates_artifacts_signer_and_task_store(
    tmp_path: Path,
) -> None:
    harness = TeamHarness(tmp_path)
    harness.controller(team_acceptance=with_assembler(harness))  # the control composes
    other_store = SQLitePersistence(tmp_path / "other.sqlite3")
    other_signer = EvidenceSigner(key=b"another-signing-key-material-02")
    cases: dict[str, Any] = {
        "task store": {"store": other_store},
        "artifact manager": {"artifacts": ArtifactManager()},
        "evidence signer": {"evidence_signer": other_signer},
        "missing signer": {"evidence_signer": None},
    }
    for label, change in cases.items():
        with pytest.raises(FencedConfigurationError):
            harness.controller(team_acceptance=with_assembler(harness, **change))
            pytest.fail(f"accepted a mismatched {label}")


def test_a_foreign_signer_would_authenticate_evidence_the_gate_rejects(tmp_path: Path) -> None:
    """Why the guard matters: with another signer, acceptance and the gate disagree."""

    async def scenario() -> None:
        harness = TeamHarness(tmp_path)
        run = await harness.flow.new_run()
        forger = EvidenceSigner(key=b"another-signing-key-material-02")
        context = _artifact_context(run.id)
        forged = await _evidence(SimpleArtifacts(harness.artifacts), context, signer=forger)
        real = TaskContextAssembler(
            store=harness.flow.store, artifacts=harness.artifacts, evidence_signer=SIGNER
        )
        foreign = TaskContextAssembler(
            store=harness.flow.store, artifacts=harness.artifacts, evidence_signer=forger
        )
        assert await real.owned_references([forged], run, authenticate=True) == []
        assert await foreign.owned_references([forged], run, authenticate=True) == [forged]

    run_async(scenario)


def test_a_platform_gate_without_signed_evidence_verifiers_is_refused(tmp_path: Path) -> None:
    harness = TeamHarness(tmp_path)
    bare = CompletionGate(verifiers=VerifierRegistry(), artifacts=harness.artifacts)
    with pytest.raises(FencedConfigurationError, match="signed_evidence_verifier"):
        harness.controller(completion_gate=bare)


def test_a_foreign_task_store_in_the_assembler_changes_no_decision(tmp_path: Path) -> None:
    """The assembler's store is unused by evidence checks: refusing it is configuration hygiene."""

    async def scenario() -> None:
        harness = TeamHarness(tmp_path, acceptance=False)
        run = await harness.flow.new_run()
        outcome = await harness.execute(run)
        assert outcome.status is WorkflowOutcomeStatus.COMPLETED
        fresh = await harness.flow.runs.get(run_id=run.id, identity=owner())
        assert fresh is not None
        foreign = SQLitePersistence(tmp_path / "foreign.sqlite3")
        decisions = [
            await with_assembler(harness, **change).evaluate(
                run=fresh, gate=await harness.gate.evaluate(None, context=None), fingerprint="fp-1"
            )
            for change in ({}, {"store": foreign})
        ]
        assert decisions[0].accepted and decisions[0] == decisions[1]

    run_async(scenario)


def test_an_assembler_without_the_platform_gate_is_refused_when_evidence_is_required(
    tmp_path: Path,
) -> None:
    harness = TeamHarness(tmp_path)

    class Custom:
        async def evaluate(self, resolution: Any, *, context: Any) -> Any:
            return await harness.gate.evaluate(resolution, context=context)

    with pytest.raises(FencedConfigurationError, match="completion gate"):
        harness.controller(completion_gate=Custom())


def test_the_gate_pipeline_composition_is_not_required_when_evidence_is_not(
    tmp_path: Path,
) -> None:
    harness = TeamHarness(tmp_path, policy={"require_evidence": False})

    class Custom:
        async def evaluate(self, resolution: Any, *, context: Any) -> Any:
            return await harness.gate.evaluate(resolution, context=context)

    harness.controller(
        completion_gate=Custom(),
        team_acceptance=with_assembler(harness, artifacts=None, evidence_signer=None),
    )


# ----------------------------------------------------------------------- semantic review


def test_an_approval_cannot_leave_criteria_unknown(tmp_path: Path) -> None:
    async def scenario() -> None:
        from tests.test_team_acceptance import assessment

        good = assessment()
        unknown = assessment(criteria=[{"criterion": "edge cases", "verdict": "unknown"}])
        assert rv.contradiction(good) is None
        assert rv.contradiction(unknown) == "approve_with_unknown_criteria"
        # Not an approval: an unknown criterion is fine in a changes request.
        changes = assessment(
            "changes_requested",
            criteria=[{"criterion": "edge cases", "verdict": "unknown"}],
            requested_changes=["check them"],
        )
        assert rv.contradiction(changes) is None

    run_async(scenario)

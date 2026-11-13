"""M6.3.4 operator decisions A, B and C.

A: with `require_evidence` (the default) every evidence reference a reviewer cites must be among the
references the authoritative completion gate itself verified (`PASS` verifications only); an
authentic same-run artifact is not enough, and a weaker legacy mode needs an explicit opt-in.
B: an empty `required_tasks` must not silently imply a reviewed delivery.
C: the reviewer mapping is immutable and the evidence composition is revalidated before acceptance.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from kaiwen_agent.artifacts import ArtifactManager
from kaiwen_agent.kits.coding import EvidenceSigner
from kaiwen_agent.quality import CompletionGateStatus, VerificationStatus
from kaiwen_agent.runtime import RuntimeRunStatus
from kaiwen_agent.workflow import (
    TaskContextAssembler,
    TeamAcceptance,
    TeamAcceptancePolicy,
    WorkflowOutcomeStatus,
)
from tests.test_agent_runtime_worker import _artifact_context, _evidence
from tests.test_lead_tools import blocked, run_async
from tests.test_team_acceptance import Status, expect, gate_of, team
from tests.test_team_acceptance_e2e import Human, TeamHarness
from tests.test_team_acceptance_r1 import GatedHuman
from tests.test_workflow_fresh_evidence import SIGNER

PASS = VerificationStatus.PASS


# ----------------------------------------------------------------------------------- decision A


def test_a_review_citing_gate_verified_evidence_is_accepted(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path)
        expect(await t.decide(), Status.ACCEPTED, "accepted")
        decision = await t.decide()
        assert decision.evidence_refs == (t.evidence,)  # what acceptance relied on is named

    run_async(scenario)


def test_an_authentic_but_unrelated_artifact_is_not_gate_verified_evidence(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path, with_reviewer=False)
        other = await _evidence(t.env, _artifact_context(t.run.id))  # signed, same run, unrelated
        await t.add_reviewer(evidence=(other,))
        expect(await t.decide(), Status.CHANGES_REQUIRED, "evidence_not_gate_verified")

    run_async(scenario)


def test_a_reviewer_may_cite_only_a_subset_but_never_a_foreign_reference(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path, with_reviewer=False)
        extra = await _evidence(t.env, _artifact_context(t.run.id))
        await t.add_reviewer(evidence=(t.evidence,))
        both = gate_of((PASS, (t.evidence, extra)))
        expect(await t.decide(gate=both), Status.ACCEPTED, "accepted")  # a subset is fine
        mixed = await team(tmp_path / "m", with_reviewer=False)
        other = await _evidence(mixed.env, _artifact_context(mixed.run.id))
        await mixed.add_reviewer(evidence=(mixed.evidence, other))
        expect(await mixed.decide(), Status.CHANGES_REQUIRED, "evidence_not_gate_verified")

    run_async(scenario)


@pytest.mark.parametrize(
    "status",
    [
        VerificationStatus.FAIL,
        VerificationStatus.INCONCLUSIVE,
        VerificationStatus.APPROVAL_REQUIRED,
    ],
)
def test_evidence_of_a_verification_that_did_not_pass_is_never_permitted(
    tmp_path: Path, status: VerificationStatus
) -> None:
    async def scenario() -> None:
        t = await team(tmp_path, with_reviewer=False)
        mandatory = await _evidence(t.env, _artifact_context(t.run.id))
        await t.add_reviewer(evidence=(t.evidence,))
        # An optional requirement failed (the gate still passed) while a mandatory one passed.
        gate = gate_of((PASS, (mandatory,)), (status, (t.evidence,)))
        expect(await t.decide(gate=gate), Status.CHANGES_REQUIRED, "evidence_not_gate_verified")

    run_async(scenario)


def test_a_failed_or_stale_artifact_cited_by_a_reviewer_does_not_count(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path, with_reviewer=False)
        failed = await _evidence(t.env, _artifact_context(t.run.id), content=b'{"exit_code":1}')
        await t.add_reviewer(evidence=(failed,))
        expect(await t.decide(), Status.CHANGES_REQUIRED, "evidence_not_gate_verified")

    run_async(scenario)


def test_without_gate_verified_evidence_acceptance_fails_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path)
        for gate in (gate_of(), gate_of((VerificationStatus.FAIL, (t.evidence,)))):
            expect(await t.decide(gate=gate), Status.BLOCKED, "no_gate_verified_evidence")

    run_async(scenario)


def test_a_reference_the_host_could_not_authenticate_is_not_a_permitted_citation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        t = await team(tmp_path, with_reviewer=False)
        await t.add_reviewer(mutate=lambda b: b.update(unverified_reference_count=1))
        expect(await t.decide(), Status.CHANGES_REQUIRED, "evidence_not_gate_verified")

    run_async(scenario)


def test_the_legacy_authentic_run_evidence_mode_needs_an_explicit_opt_in(tmp_path: Path) -> None:
    from kaiwen_agent.workflow import EvidenceMode

    async def scenario() -> None:
        t = await team(tmp_path, with_reviewer=False)
        other = await _evidence(t.env, _artifact_context(t.run.id))
        await t.add_reviewer(evidence=(other,))
        nothing = gate_of()
        expect(await t.decide(gate=nothing), Status.BLOCKED, "no_gate_verified_evidence")  # default
        legacy = {"evidence_mode": EvidenceMode.AUTHENTIC_RUN_LEGACY}
        expect(await t.decide(gate=nothing, **legacy), Status.ACCEPTED, "accepted")
        # Still authenticated: an artifact of another run is no legacy evidence either.
        foreign = await team(tmp_path / "f", with_reviewer=False)
        stranger = await _evidence(foreign.env, _artifact_context("run-elsewhere"))
        await foreign.add_reviewer(evidence=(stranger,))
        expect(
            await foreign.decide(gate=nothing, **legacy),
            Status.UNAVAILABLE,
            "evidence_unverifiable",
        )
        assert (
            TeamAcceptancePolicy(
                reviewer_workers={"r": "r"}, required_tasks=frozenset({"x"})
            ).evidence_mode
            == EvidenceMode.GATE_VERIFIED
        )

    run_async(scenario)


def test_evidence_is_not_required_at_all_when_the_host_says_so(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path, with_reviewer=False)
        await t.add_reviewer(evidence=())
        expect(await t.decide(gate=gate_of(), require_evidence=False), Status.ACCEPTED, "accepted")

    run_async(scenario)


def test_the_controller_checks_the_second_gate_evaluation_against_the_reviewed_evidence(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        human = GatedHuman()
        harness = TeamHarness(tmp_path, human=human)
        run = await harness.flow.new_run()
        pending = asyncio.ensure_future(harness.execute(run))
        await human.started.wait()
        # The gate still passes, but what it now verifies is not what the review cited.
        harness.gate.refs = lambda: ["artifact_replaced_after_acceptance"]
        human.release.set()
        outcome = await pending
        blocked(outcome, "gate_evidence_changed_after_acceptance:evidence_not_gate_verified")
        assert await harness.status(run) is RuntimeRunStatus.BLOCKED

    run_async(scenario)


def test_unchanged_gate_evidence_after_a_human_approval_completes(tmp_path: Path) -> None:
    async def scenario() -> None:
        human = Human(
            __import__("kaiwen_agent.workflow", fromlist=["x"]).HumanApprovalStatus.APPROVED
        )
        harness = TeamHarness(tmp_path, human=human)
        run = await harness.flow.new_run()
        outcome = await harness.execute(run)
        assert outcome.status is WorkflowOutcomeStatus.COMPLETED
        assert outcome.team_acceptance is not None
        assert outcome.team_acceptance.evidence_refs == (harness.evidence_ref,)

    run_async(scenario)


def test_a_gate_that_passes_with_no_verified_evidence_blocks_the_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = TeamHarness(tmp_path)
        harness.gate.refs = lambda: []  # empty standards: the gate passes, nothing was verified
        run = await harness.flow.new_run()
        outcome = await harness.execute(run)
        blocked(outcome, "team_blocked:no_gate_verified_evidence")

    run_async(scenario)


# ----------------------------------------------------------------------------------- decision B


def test_an_empty_required_task_set_is_refused_unless_partial_review_is_explicit() -> None:
    with pytest.raises(ValueError, match="required_tasks"):
        TeamAcceptancePolicy(reviewer_workers={"reviewer": "reviewer"})
    with pytest.raises(ValueError, match="required_tasks"):
        TeamAcceptancePolicy(reviewer_workers={"reviewer": "reviewer"}, required_tasks=frozenset())
    named = TeamAcceptancePolicy(reviewer_workers={"reviewer": "reviewer"}, required_tasks={"a"})
    assert named.required_tasks == frozenset({"a"}) and named.allow_partial_review is False
    partial = TeamAcceptancePolicy(
        reviewer_workers={"reviewer": "reviewer"}, allow_partial_review=True
    )
    assert partial.required_tasks == frozenset() and partial.allow_partial_review is True


def test_a_nonempty_required_set_keeps_requiring_full_coverage(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path, with_reviewer=False)
        await t.add_reviewer(reviewed=("test",))
        expect(await t.decide(), Status.CHANGES_REQUIRED, "review_coverage_incomplete")

    run_async(scenario)


def test_an_explicit_partial_review_covers_only_what_was_reviewed(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path, with_reviewer=False)
        await t.add_reviewer(reviewed=("test",))
        decision = await t.decide(required_tasks=frozenset(), allow_partial_review=True)
        expect(decision, Status.ACCEPTED, "accepted")
        # Limited coverage, never certification of every task: the record names what was reviewed.
        assert decision.reviewer_task_ids == (t.reviewers[0].id,)

    run_async(scenario)


# ----------------------------------------------------------------------------------- decision C


def test_the_reviewer_mapping_is_immutable_after_validated_construction() -> None:
    source = {"reviewer": "reviewer"}
    policy = TeamAcceptancePolicy(reviewer_workers=source, required_tasks=frozenset({"a"}))
    with pytest.raises(TypeError):
        policy.reviewer_workers["ghost"] = "reviewer"  # type: ignore[index]
    with pytest.raises(TypeError):
        del policy.reviewer_workers["reviewer"]  # type: ignore[attr-defined]
    source["ghost"] = "reviewer"  # the caller's own dict no longer reaches the policy
    assert dict(policy.reviewer_workers) == {"reviewer": "reviewer"}


def test_replacing_the_evidence_signer_after_construction_blocks_acceptance(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = TeamHarness(tmp_path)
        acceptance = harness.acceptance()
        controller = harness.controller(team_acceptance=acceptance)  # composes at construction
        acceptance.assembler.evidence_signer = EvidenceSigner(
            key=b"another-signing-key-material-02"
        )
        run = await harness.flow.new_run()
        outcome = await harness.execute(run, controller)
        blocked(
            outcome,
            "team_acceptance_composition_changed:assembler_uses_a_different_evidence_signer",
        )
        assert await harness.status(run) is RuntimeRunStatus.BLOCKED

    run_async(scenario)


def test_replacing_the_artifact_manager_or_task_store_after_construction_blocks_acceptance(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        for attribute, replacement, issue in (
            ("artifacts", ArtifactManager(), "assembler_uses_different_artifacts"),
            ("store", object(), "assembler_reads_a_different_task_store"),
        ):
            harness = TeamHarness(tmp_path / attribute)
            acceptance = harness.acceptance()
            controller = harness.controller(team_acceptance=acceptance)
            setattr(acceptance.assembler, attribute, replacement)
            run = await harness.flow.new_run()
            blocked(
                await harness.execute(run, controller),
                f"team_acceptance_composition_changed:{issue}",
            )

    run_async(scenario)


def test_a_composition_change_during_a_pending_human_approval_blocks_completion(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        human = GatedHuman()
        harness = TeamHarness(tmp_path, human=human)
        acceptance = harness.acceptance()
        controller = harness.controller(team_acceptance=acceptance)
        run = await harness.flow.new_run()
        pending = asyncio.ensure_future(harness.execute(run, controller))
        await human.started.wait()
        # The task store is not used by evidence re-authentication, so only the composition
        # re-check (right before completing) can notice that it was replaced during the wait.
        acceptance.assembler.store = object()  # type: ignore[assignment]
        human.release.set()
        outcome = await pending
        blocked(
            outcome, "team_acceptance_composition_changed:assembler_reads_a_different_task_store"
        )
        assert await harness.status(run) is RuntimeRunStatus.BLOCKED

    run_async(scenario)


def test_replacing_the_acceptance_task_store_or_runs_after_construction_blocks_acceptance(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        # A frozen dataclass still lets a trusted host bypass it; the controller notices.
        for attribute, replacement, issue in (
            ("tasks", object(), "tasks_not_the_authoritative_store"),
            ("runs", object(), "runs_not_the_controllers"),
        ):
            harness = TeamHarness(tmp_path / attribute)
            acceptance = harness.acceptance()
            controller = harness.controller(team_acceptance=acceptance)
            object.__setattr__(acceptance, attribute, replacement)
            run = await harness.flow.new_run()
            blocked(
                await harness.execute(run, controller),
                f"team_acceptance_composition_changed:{issue}",
            )

    run_async(scenario)


def test_a_signer_replaced_during_a_pending_approval_fails_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        human = GatedHuman()
        harness = TeamHarness(tmp_path, human=human)
        acceptance = harness.acceptance()
        controller = harness.controller(team_acceptance=acceptance)
        run = await harness.flow.new_run()
        pending = asyncio.ensure_future(harness.execute(run, controller))
        await human.started.wait()
        acceptance.assembler.evidence_signer = EvidenceSigner(
            key=b"another-signing-key-material-02"
        )
        human.release.set()
        blocked(await pending, "team_unavailable:evidence_unverifiable")  # re-authentication fails
        assert await harness.status(run) is RuntimeRunStatus.BLOCKED

    run_async(scenario)


def test_an_unmodified_composition_still_completes(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = TeamHarness(tmp_path)
        run = await harness.flow.new_run()
        assert (await harness.execute(run)).status is WorkflowOutcomeStatus.COMPLETED

    run_async(scenario)


def _unused() -> tuple[Any, Any, Any]:  # keeps optional imports referenced for static checks
    return TaskContextAssembler, TeamAcceptance, (CompletionGateStatus, SIGNER)


# ------------------------------------------------------------- the real completion gate (A)


def _real_gate_standards() -> Any:
    from kaiwen_agent.agents import VersionedReference
    from kaiwen_agent.standards import (
        RequirementDefinition,
        RequirementObligation,
        RequirementSeverity,
        ResolvedRequirement,
        StandardResolution,
    )

    ref = VersionedReference(id="software", version=1)
    definition = RequirementDefinition(
        id="coding.tests.run",
        description="Tests pass",
        obligation=RequirementObligation.MUST,
        severity=RequirementSeverity.ERROR,
        acceptance_criteria=("Tests pass",),
        verifier_ref="coding.tests",
        evidence_types=frozenset({"test_run"}),
    )
    return StandardResolution(
        pack_refs=(ref,),
        requirements=(
            ResolvedRequirement(definition=definition, source_pack_refs=(ref,), applies=True),
        ),
        prompt_refs=(),
    )


async def _trusted_test_evidence(
    harness: TeamHarness, run_id: str, *, fingerprint: str = "fp-1"
) -> str:
    """A signed command-evidence artifact the real `coding.tests` verifier accepts."""

    import json

    from kaiwen_agent.artifacts import ArtifactCreate, ArtifactType

    context = _artifact_context(run_id)
    payload = json.dumps(
        {
            "origin": "trusted_sandbox",
            "run_id": run_id,
            "requirement_id": "coding.tests.run",
            "snapshot_fingerprint": fingerprint,
            "exit_code": 0,
            "timed_out": False,
            "cancelled": False,
            "snapshot_unchanged": True,
            "cleanup_failed": False,
        }
    ).encode()
    labels = {
        "evidence_origin": "trusted_sandbox",
        "requirement_id": "coding.tests.run",
        "evidence_type": "test_run",
        "input_fingerprint": fingerprint,
    }
    labels["evidence_mac"] = SIGNER.sign(
        tenant_id=context.tenant_id,
        owner_user_id=context.user_id,
        run_id=run_id,
        content=payload,
        labels=dict(labels),
    )
    metadata = await harness.artifacts.create(
        context,
        ArtifactCreate(
            type=ArtifactType.REPORT, mime_type="application/json", content=payload, labels=labels
        ),
    )
    return metadata.id


async def _execute_with_the_real_gate(harness: TeamHarness, run: Any) -> Any:
    from tests.test_agent_runtime_worker import owner

    delattr(harness.real_gate, "evaluate")  # the platform CompletionGate decides, not the script
    fresh = await harness.flow.runs.get(run_id=run.id, identity=owner())
    assert fresh is not None
    from tests.test_team_acceptance_e2e import LEAD_PLAN

    return await harness.controller().execute(
        run=fresh,
        identity=owner(),
        plan=LEAD_PLAN,
        standards=_real_gate_standards(),
        protected_input_fingerprint="objective-1",
    )


def test_with_the_real_gate_a_review_citing_the_gate_verified_artifact_completes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = TeamHarness(tmp_path)
        run = await harness.flow.new_run()
        evidence = await _trusted_test_evidence(harness, run.id)

        async def tester_artifact(task: Any) -> str:
            return evidence  # the tester's own evidence is what the gate verifies

        harness._artifact = tester_artifact  # type: ignore[method-assign]
        outcome = await _execute_with_the_real_gate(harness, run)
        assert outcome.status is WorkflowOutcomeStatus.COMPLETED, outcome.reason
        assert outcome.team_acceptance is not None
        assert outcome.team_acceptance.evidence_refs == (evidence,)

    run_async(scenario)


def test_with_the_real_gate_an_unrelated_authentic_citation_is_refused(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = TeamHarness(tmp_path)  # the reviewer cites the tester's generic artifact
        run = await harness.flow.new_run()
        await _trusted_test_evidence(harness, run.id)  # the gate verifies this one, not the cited
        outcome = await _execute_with_the_real_gate(harness, run)
        blocked(outcome, "team_changes_required:evidence_not_gate_verified")

    run_async(scenario)


def test_with_the_real_gate_and_empty_standards_acceptance_fails_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        from kaiwen_agent.standards import StandardResolution
        from tests.test_agent_runtime_worker import owner
        from tests.test_team_acceptance_e2e import LEAD_PLAN

        harness = TeamHarness(tmp_path)
        delattr(harness.real_gate, "evaluate")
        run = await harness.flow.new_run()
        fresh = await harness.flow.runs.get(run_id=run.id, identity=owner())
        assert fresh is not None
        outcome = await harness.controller().execute(
            run=fresh,
            identity=owner(),
            plan=LEAD_PLAN,
            standards=StandardResolution(pack_refs=(), requirements=(), prompt_refs=()),
            protected_input_fingerprint="objective-1",
        )
        blocked(outcome, "team_blocked:no_gate_verified_evidence")

    run_async(scenario)

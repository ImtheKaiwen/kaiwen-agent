from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from kaiwen_agent.agents import VersionedReference
from kaiwen_agent.artifacts import ArtifactContext, ArtifactCreate, ArtifactManager, ArtifactType
from kaiwen_agent.quality import (
    CompletionGate,
    CompletionGateStatus,
    RepairDecision,
    RepairLimits,
    RepairTracker,
    VerificationContext,
    VerificationResult,
    VerificationStatus,
    VerifierRegistry,
)
from kaiwen_agent.standards import (
    RequirementDefinition,
    RequirementObligation,
    RequirementSeverity,
    StandardsPack,
    StandardsRegistry,
    StandardsResolver,
)


def requirement(
    identifier: str = "quality.tests",
    *,
    obligation: RequirementObligation = RequirementObligation.MUST,
    applies_when: str | None = None,
) -> RequirementDefinition:
    return RequirementDefinition(
        id=identifier,
        description="Tests must pass",
        obligation=obligation,
        severity=RequirementSeverity.ERROR,
        applies_when=applies_when,
        acceptance_criteria=("Test command exits successfully",),
        verifier_ref="tests.verify",
        evidence_types=frozenset({"test_report"}),
    )


def pack(
    identifier: str = "software",
    *,
    item: RequirementDefinition | None = None,
) -> StandardsPack:
    return StandardsPack(
        id=identifier,
        version=1,
        requirements=(item or requirement(),),
    )


def verification_context(*, user_id: str = "user_1") -> VerificationContext:
    return VerificationContext(
        tenant_id="tenant_1",
        user_id=user_id,
        session_id="session_1",
        trace_id="trace_1",
        run_id="run_1",
        protected_input_fingerprint="fingerprint_1",
    )


class FixedVerifier:
    verifier_id = "tests.verify"

    def __init__(self, result: VerificationResult) -> None:
        self.result = result

    async def verify(self, requirement, *, context):
        del requirement, context
        return self.result


def test_standards_are_immutable_versioned_and_conditions_resolve_deterministically() -> None:
    conditional = requirement(
        "ci.execution",
        obligation=RequirementObligation.CONDITIONAL,
        applies_when="ci_available",
    )
    definition = pack(item=conditional)
    registry = StandardsRegistry([definition])
    resolver = StandardsResolver(registry)
    unresolved = resolver.resolve([VersionedReference(id="software", version=1)])
    assert unresolved.requirements[0].applies is None
    assert unresolved.unresolved_conditions == ("ci.execution",)
    disabled = resolver.resolve(
        [VersionedReference(id="software", version=1)],
        condition_facts={"ci_available": False},
    )
    assert disabled.requirements[0].applies is False
    with pytest.raises(ValueError, match="already registered"):
        registry.register(definition)
    with pytest.raises(ValidationError):
        definition.version = 2  # type: ignore[misc]


def test_conflicting_requirements_never_use_last_writer_wins() -> None:
    first = pack("first")
    second = pack(
        "second",
        item=requirement().model_copy(update={"description": "A different requirement"}),
    )
    resolution = StandardsResolver(StandardsRegistry([first, second])).resolve(
        [
            VersionedReference(id="first", version=1),
            VersionedReference(id="second", version=1),
        ]
    )
    assert not resolution.valid
    assert resolution.requirements == ()
    assert resolution.conflicts == ("Conflicting requirement: quality.tests",)


def test_completion_gate_requires_owned_fresh_typed_run_evidence() -> None:
    async def scenario() -> None:
        artifacts = ArtifactManager()
        context = verification_context()
        evidence = await artifacts.create(
            context.artifact_context(),
            ArtifactCreate(
                type=ArtifactType.REPORT,
                mime_type="application/json",
                content=b'{"passed":true}',
                labels={
                    "evidence_type": "test_report",
                    "input_fingerprint": context.protected_input_fingerprint,
                },
            ),
        )
        result = VerificationResult(
            requirement_id="quality.tests",
            status=VerificationStatus.PASS,
            evidence_refs=(evidence.id,),
            verifier_id="tests.verify",
        )
        verifiers = VerifierRegistry()
        verifiers.register(FixedVerifier(result))
        resolution = StandardsResolver(StandardsRegistry([pack()])).resolve(
            [VersionedReference(id="software", version=1)]
        )
        outcome = await CompletionGate(verifiers=verifiers, artifacts=artifacts).evaluate(
            resolution, context=context
        )
        assert outcome.status == CompletionGateStatus.PASS
        assert outcome.can_complete

        wrong_owner = await CompletionGate(verifiers=verifiers, artifacts=artifacts).evaluate(
            resolution, context=verification_context(user_id="user_2")
        )
        assert wrong_owner.status == CompletionGateStatus.BLOCKED
        assert wrong_owner.verifications[0].status == VerificationStatus.INCONCLUSIVE
        assert wrong_owner.verifications[0].findings == ("evidence_not_found_or_not_owned",)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("labels", "run_id", "finding"),
    [
        (
            {"evidence_type": "test_report", "input_fingerprint": "stale"},
            "run_1",
            "evidence_is_stale",
        ),
        (
            {"evidence_type": "wrong", "input_fingerprint": "fingerprint_1"},
            "run_1",
            "evidence_type_mismatch",
        ),
        (
            {"evidence_type": "test_report", "input_fingerprint": "fingerprint_1"},
            "other_run",
            "evidence_wrong_run",
        ),
    ],
)
def test_completion_gate_rejects_stale_wrong_type_or_cross_run_evidence(
    labels: dict[str, str], run_id: str, finding: str
) -> None:
    async def scenario() -> None:
        artifacts = ArtifactManager()
        context = verification_context()
        evidence = await artifacts.create(
            ArtifactContext(
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                session_id=context.session_id,
                trace_id=context.trace_id,
                run_id=run_id,
            ),
            ArtifactCreate(
                type=ArtifactType.REPORT,
                mime_type="application/json",
                content=b"{}",
                labels=labels,
            ),
        )
        verifiers = VerifierRegistry()
        verifiers.register(
            FixedVerifier(
                VerificationResult(
                    requirement_id="quality.tests",
                    status=VerificationStatus.PASS,
                    evidence_refs=(evidence.id,),
                    verifier_id="tests.verify",
                )
            )
        )
        resolution = StandardsResolver(StandardsRegistry([pack()])).resolve(
            [VersionedReference(id="software", version=1)]
        )
        outcome = await CompletionGate(verifiers=verifiers, artifacts=artifacts).evaluate(
            resolution, context=context
        )
        assert outcome.status == CompletionGateStatus.BLOCKED
        assert outcome.verifications[0].findings == (finding,)

    asyncio.run(scenario())


def test_missing_verifier_and_unknown_condition_are_inconclusive_not_success() -> None:
    async def scenario() -> None:
        registry = StandardsRegistry(
            [
                pack(),
                pack(
                    "conditional",
                    item=requirement(
                        "ci.execution",
                        obligation=RequirementObligation.CONDITIONAL,
                        applies_when="ci_available",
                    ),
                ),
            ]
        )
        resolution = StandardsResolver(registry).resolve(
            [
                VersionedReference(id="software", version=1),
                VersionedReference(id="conditional", version=1),
            ]
        )
        outcome = await CompletionGate(
            verifiers=VerifierRegistry(), artifacts=ArtifactManager()
        ).evaluate(resolution, context=verification_context())
        assert outcome.status == CompletionGateStatus.BLOCKED
        assert {item.findings[0] for item in outcome.verifications} == {
            "verifier_not_registered",
            "condition_unresolved",
        }

    asyncio.run(scenario())


def test_should_failure_warns_while_mandatory_approval_blocks_completion() -> None:
    async def scenario() -> None:
        should_pack = pack(item=requirement(obligation=RequirementObligation.SHOULD))
        resolution = StandardsResolver(StandardsRegistry([should_pack])).resolve(
            [VersionedReference(id="software", version=1)]
        )
        failed = VerifierRegistry()
        failed.register(
            FixedVerifier(
                VerificationResult(
                    requirement_id="quality.tests",
                    status=VerificationStatus.FAIL,
                    verifier_id="tests.verify",
                )
            )
        )
        warning = await CompletionGate(verifiers=failed, artifacts=ArtifactManager()).evaluate(
            resolution, context=verification_context()
        )
        assert warning.status == CompletionGateStatus.PASS
        assert warning.warnings == ("quality.tests: fail",)

        must_resolution = StandardsResolver(StandardsRegistry([pack()])).resolve(
            [VersionedReference(id="software", version=1)]
        )
        approval = VerifierRegistry()
        approval.register(
            FixedVerifier(
                VerificationResult(
                    requirement_id="quality.tests",
                    status=VerificationStatus.APPROVAL_REQUIRED,
                    verifier_id="tests.verify",
                )
            )
        )
        pending = await CompletionGate(verifiers=approval, artifacts=ArtifactManager()).evaluate(
            must_resolution, context=verification_context()
        )
        assert pending.status == CompletionGateStatus.APPROVAL_REQUIRED
        assert not pending.can_complete

    asyncio.run(scenario())


def test_repair_tracker_bounds_attempts_replans_and_repeated_failures() -> None:
    tracker = RepairTracker(
        RepairLimits(
            max_repairs_per_requirement=3,
            max_replans=1,
            stagnation_repetitions=2,
        )
    )
    assert tracker.record_failure("quality.tests", "failure-a") == RepairDecision.REPAIR
    assert tracker.record_failure("quality.tests", "failure-a") == RepairDecision.STAGNATED
    assert tracker.record_failure("quality.tests", "failure-b") == RepairDecision.REPAIR
    assert tracker.record_failure("quality.tests", "failure-c") == RepairDecision.EXHAUSTED
    assert tracker.request_replan()
    assert not tracker.request_replan()

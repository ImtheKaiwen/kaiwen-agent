from __future__ import annotations

import asyncio
import json

import pytest

from kaiwen_agent.agents import VersionedReference
from kaiwen_agent.artifacts import ArtifactCreate, ArtifactManager, ArtifactType
from kaiwen_agent.kits.coding import (
    coding_standards_packs,
    install_coding_standards,
    install_coding_verifiers,
)
from kaiwen_agent.quality import (
    CompletionGate,
    CompletionGateStatus,
    VerificationContext,
    VerifierRegistry,
)
from kaiwen_agent.standards import StandardsPack, StandardsRegistry, StandardsResolver


def _references(*identifiers: str) -> tuple[VersionedReference, ...]:
    return tuple(VersionedReference(id=identifier, version=1) for identifier in identifiers)


def _context(fingerprint: str = "current") -> VerificationContext:
    return VerificationContext(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        trace_id="trace-1",
        run_id="run-1",
        protected_input_fingerprint=fingerprint,
    )


def test_coding_standards_are_complete_conditional_and_conflict_safe() -> None:
    registry = StandardsRegistry()
    packs = install_coding_standards(registry)
    assert install_coding_standards(registry) == packs
    assert {pack.id for pack in packs} == {
        "coding.foundation",
        "coding.documentation",
        "coding.tests",
        "coding.architecture",
        "coding.security",
        "coding.performance",
        "coding.ci",
        "coding.data",
        "coding.api",
        "coding.ui-accessibility",
        "coding.ui-minimal-neutral",
    }

    conflicting = StandardsRegistry()
    first = coding_standards_packs()[0]
    conflicting.register(first.model_copy(update={"description": "conflict"}))
    with pytest.raises(ValueError, match="Conflicting coding standards"):
        install_coding_standards(conflicting)


def test_applicability_does_not_force_data_or_ui_rules_on_unrelated_projects() -> None:
    registry = StandardsRegistry(coding_standards_packs())
    resolver = StandardsResolver(registry)
    resolution = resolver.resolve(
        _references(
            "coding.tests",
            "coding.data",
            "coding.ui-accessibility",
        ),
        condition_facts={
            "has_executable_tests": True,
            "has_database": False,
            "has_ui": False,
        },
    )
    applies = {item.definition.id: item.applies for item in resolution.requirements}
    assert applies == {
        "coding.data.integrity": False,
        "coding.tests.run": True,
        "coding.ui.accessibility": False,
    }


def test_coding_verifier_and_completion_gate_require_current_observed_evidence() -> None:
    async def scenario() -> None:
        artifacts = ArtifactManager()
        context = _context()
        evidence = await artifacts.create(
            context.artifact_context(),
            ArtifactCreate(
                type=ArtifactType.REPORT,
                mime_type="application/json",
                content=json.dumps(
                    {"status": "pass", "exit_code": 0, "command_id": "pytest"}
                ).encode(),
                labels={
                    "requirement_id": "coding.tests.run",
                    "evidence_type": "test_run",
                    "input_fingerprint": context.protected_input_fingerprint,
                },
            ),
        )
        verifiers = VerifierRegistry()
        install_coding_verifiers(verifiers, artifacts)
        install_coding_verifiers(verifiers, artifacts)
        standards = StandardsRegistry(coding_standards_packs())
        resolution = StandardsResolver(standards).resolve(
            _references("coding.tests"),
            condition_facts={"has_executable_tests": True},
        )
        result = await CompletionGate(verifiers=verifiers, artifacts=artifacts).evaluate(
            resolution,
            context=context,
        )
        assert result.status == CompletionGateStatus.PASS
        assert result.verifications[0].evidence_refs == (evidence.id,)

        stale = await CompletionGate(verifiers=verifiers, artifacts=artifacts).evaluate(
            resolution,
            context=_context("changed"),
        )
        assert stale.status == CompletionGateStatus.BLOCKED
        assert stale.verifications[0].findings == ("current_evidence_not_found",)

    asyncio.run(scenario())


def test_failed_test_evidence_blocks_and_advisory_design_failure_warns() -> None:
    async def scenario() -> None:
        artifacts = ArtifactManager()
        context = _context()
        for requirement_id, evidence_type in (
            ("coding.tests.run", "test_run"),
            ("coding.ui.design_consistency", "design_check"),
        ):
            await artifacts.create(
                context.artifact_context(),
                ArtifactCreate(
                    type=ArtifactType.REPORT,
                    mime_type="application/json",
                    content=json.dumps(
                        {"status": "fail", "exit_code": 1, "summary": "observed failure"}
                    ).encode(),
                    labels={
                        "requirement_id": requirement_id,
                        "evidence_type": evidence_type,
                        "input_fingerprint": context.protected_input_fingerprint,
                    },
                ),
            )
        verifiers = VerifierRegistry()
        install_coding_verifiers(verifiers, artifacts)
        resolution = StandardsResolver(StandardsRegistry(coding_standards_packs())).resolve(
            _references("coding.tests", "coding.ui-minimal-neutral"),
            condition_facts={"has_executable_tests": True, "has_ui": True},
        )
        result = await CompletionGate(verifiers=verifiers, artifacts=artifacts).evaluate(
            resolution,
            context=context,
        )
        assert result.status == CompletionGateStatus.BLOCKED
        assert result.blockers == ("coding.tests.run: fail",)
        assert result.warnings == ("coding.ui.design_consistency: fail",)

    asyncio.run(scenario())


def test_coding_verifier_installer_rejects_conflicting_registration() -> None:
    class OtherVerifier:
        verifier_id = "coding.tests"

        async def verify(self, requirement, *, context):  # type: ignore[no-untyped-def]
            del requirement, context
            raise AssertionError

    registry = VerifierRegistry()
    registry.register(OtherVerifier())
    with pytest.raises(ValueError, match="Conflicting coding verifier"):
        install_coding_verifiers(registry, ArtifactManager())


def test_unknown_condition_remains_unresolved() -> None:
    resolution = StandardsResolver(StandardsRegistry(coding_standards_packs())).resolve(
        _references("coding.data")
    )
    assert resolution.unresolved_conditions == ("coding.data.integrity",)


def test_standards_pack_shape_remains_core_type() -> None:
    assert all(isinstance(pack, StandardsPack) for pack in coding_standards_packs())

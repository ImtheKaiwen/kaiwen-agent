from __future__ import annotations

from kaiwen_agent.standards import (
    RequirementDefinition,
    RequirementObligation,
    RequirementSeverity,
    StandardsPack,
    StandardsRegistry,
)


def coding_standards_packs() -> tuple[StandardsPack, ...]:
    return (
        _pack(
            "coding.foundation",
            "Core evidence-backed delivery requirements for coding tasks.",
            _requirement(
                "coding.foundation.acceptance",
                "The implemented behavior satisfies the current acceptance criteria.",
                verifier="coding.acceptance",
                evidence="acceptance_test_result",
            ),
        ),
        _pack(
            "coding.documentation",
            "Proportionate project documentation requirements.",
            _requirement(
                "coding.documentation.current",
                "Required project documentation exists and reflects current verified status.",
                verifier="coding.documentation",
                evidence="docs_audit",
                condition="requires_documentation",
            ),
        ),
        _pack(
            "coding.tests",
            "Executable tests must have current observed results.",
            _requirement(
                "coding.tests.run",
                "Applicable configured tests have a successful current run.",
                verifier="coding.tests",
                evidence="test_run",
                condition="has_executable_tests",
            ),
        ),
        _pack(
            "coding.architecture",
            "Architecture checks for multi-module projects.",
            _requirement(
                "coding.architecture.boundaries",
                "Applicable module boundaries and dependency rules are checked.",
                verifier="coding.architecture",
                evidence="architecture_check",
                condition="is_multi_module",
            ),
        ),
        _pack(
            "coding.security",
            "Security evidence for source and tool operations.",
            _requirement(
                "coding.security.secrets",
                "The current protected input has an observed sensitive-data scan result.",
                verifier="coding.security",
                evidence="secret_scan",
                severity=RequirementSeverity.CRITICAL,
            ),
        ),
        _pack(
            "coding.performance",
            "Measured performance requirements for explicitly performance-critical work.",
            _requirement(
                "coding.performance.target",
                "The declared performance target has reproducible benchmark evidence.",
                verifier="coding.performance",
                evidence="benchmark",
                condition="performance_critical",
            ),
        ),
        _pack(
            "coding.ci",
            "Continuous-integration configuration evidence.",
            _requirement(
                "coding.ci.configuration",
                "The selected CI configuration has a current static validation result.",
                verifier="coding.ci",
                evidence="ci_config_check",
                condition="uses_ci",
            ),
        ),
        _pack(
            "coding.data",
            "Data integrity requirements for projects with persistent storage.",
            _requirement(
                "coding.data.integrity",
                "Applicable schema, constraint, and migration behavior is verified.",
                verifier="coding.data",
                evidence="migration_check",
                condition="has_database",
            ),
        ),
        _pack(
            "coding.api",
            "Boundary checks for applications with network APIs.",
            _requirement(
                "coding.api.contract",
                "Applicable API schema, validation, error, and access behavior is verified.",
                verifier="coding.api",
                evidence="api_check",
                condition="has_network",
            ),
        ),
        _pack(
            "coding.ui-accessibility",
            "Executable accessibility evidence for user interfaces.",
            _requirement(
                "coding.ui.accessibility",
                "Representative interface flows have observed accessibility evidence.",
                verifier="coding.ui_accessibility",
                evidence="a11y_report",
                condition="has_ui",
            ),
        ),
        _pack(
            "coding.ui-minimal-neutral",
            "Advisory design consistency checks for the optional neutral preset.",
            _requirement(
                "coding.ui.design_consistency",
                "Applicable interface tokens and component states have review evidence.",
                verifier="coding.ui_design",
                evidence="design_check",
                condition="has_ui",
                obligation=RequirementObligation.SHOULD,
                severity=RequirementSeverity.WARNING,
            ),
        ),
    )


def install_coding_standards(registry: StandardsRegistry) -> tuple[StandardsPack, ...]:
    packs = coding_standards_packs()
    for pack in packs:
        try:
            existing = registry.get(pack.id, pack.version)
        except KeyError:
            registry.register(pack)
            continue
        if existing != pack:
            raise ValueError(f"Conflicting coding standards pack: {pack.id}@{pack.version}")
    return packs


def _pack(identifier: str, description: str, requirement: RequirementDefinition) -> StandardsPack:
    return StandardsPack(
        id=identifier,
        version=1,
        description=description,
        requirements=(requirement,),
    )


def _requirement(
    identifier: str,
    description: str,
    *,
    verifier: str,
    evidence: str,
    condition: str | None = None,
    obligation: RequirementObligation | None = None,
    severity: RequirementSeverity = RequirementSeverity.ERROR,
) -> RequirementDefinition:
    selected_obligation = obligation or (
        RequirementObligation.CONDITIONAL if condition else RequirementObligation.MUST
    )
    return RequirementDefinition(
        id=identifier,
        version=1,
        description=description,
        obligation=selected_obligation,
        severity=severity,
        applies_when=(
            condition if selected_obligation == RequirementObligation.CONDITIONAL else None
        ),
        acceptance_criteria=(description,),
        verifier_ref=verifier,
        evidence_types=frozenset({evidence}),
    )

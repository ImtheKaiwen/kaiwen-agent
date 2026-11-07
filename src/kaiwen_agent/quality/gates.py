from __future__ import annotations

import asyncio

from kaiwen_agent.artifacts import ArtifactManager, ArtifactNotFoundError
from kaiwen_agent.quality.models import (
    CompletionGateResult,
    CompletionGateStatus,
    VerificationContext,
    VerificationResult,
    VerificationStatus,
)
from kaiwen_agent.quality.verifiers import VerifierRegistry
from kaiwen_agent.standards import ResolvedRequirement, StandardResolution


class CompletionGate:
    def __init__(self, *, verifiers: VerifierRegistry, artifacts: ArtifactManager) -> None:
        self.verifiers = verifiers
        self.artifacts = artifacts

    async def evaluate(
        self,
        resolution: StandardResolution,
        *,
        context: VerificationContext,
    ) -> CompletionGateResult:
        if resolution.conflicts:
            return CompletionGateResult(
                status=CompletionGateStatus.CONFIGURATION_ERROR,
                verifications=(),
                blockers=resolution.conflicts,
            )
        verifications: list[VerificationResult] = []
        blockers: list[str] = []
        warnings: list[str] = []
        approval_required = False
        for requirement in resolution.requirements:
            if requirement.applies is False:
                continue
            result = await self._verify(requirement, context=context)
            verifications.append(result)
            message = f"{requirement.definition.id}: {result.status.value}"
            if result.status == VerificationStatus.APPROVAL_REQUIRED:
                if requirement.mandatory:
                    approval_required = True
                else:
                    warnings.append(message)
            elif result.status != VerificationStatus.PASS:
                if requirement.mandatory:
                    blockers.append(message)
                else:
                    warnings.append(message)

        if blockers:
            status = CompletionGateStatus.BLOCKED
        elif approval_required:
            status = CompletionGateStatus.APPROVAL_REQUIRED
        else:
            status = CompletionGateStatus.PASS
        return CompletionGateResult(
            status=status,
            verifications=tuple(verifications),
            blockers=tuple(blockers),
            warnings=tuple(warnings),
        )

    async def _verify(
        self,
        requirement: ResolvedRequirement,
        *,
        context: VerificationContext,
    ) -> VerificationResult:
        verifier_id = requirement.definition.verifier_ref
        if requirement.applies is None:
            return self._inconclusive(requirement, "condition_unresolved")
        verifier = self.verifiers.get(verifier_id)
        if verifier is None:
            return self._inconclusive(requirement, "verifier_not_registered")
        try:
            result = await verifier.verify(requirement, context=context)
        except asyncio.CancelledError:
            raise
        except Exception:
            return self._inconclusive(requirement, "verifier_failed")
        if result.requirement_id != requirement.definition.id or result.verifier_id != verifier_id:
            return self._inconclusive(requirement, "verifier_identity_mismatch")
        if len(result.evidence_refs) != len(set(result.evidence_refs)):
            return self._inconclusive(requirement, "duplicate_evidence_reference")
        if result.status == VerificationStatus.PASS and not result.evidence_refs:
            return self._inconclusive(requirement, "passing_result_requires_evidence")
        evidence_error = await self._validate_evidence(requirement, result, context=context)
        if evidence_error is not None:
            return self._inconclusive(requirement, evidence_error)
        return result

    async def _validate_evidence(
        self,
        requirement: ResolvedRequirement,
        result: VerificationResult,
        *,
        context: VerificationContext,
    ) -> str | None:
        for artifact_id in result.evidence_refs:
            try:
                artifact = await self.artifacts.read(context.artifact_context(), artifact_id)
            except ArtifactNotFoundError:
                return "evidence_not_found_or_not_owned"
            labels = artifact.metadata.labels
            if artifact.metadata.run_id != context.run_id:
                return "evidence_wrong_run"
            if labels.get("input_fingerprint") != context.protected_input_fingerprint:
                return "evidence_is_stale"
            if labels.get("evidence_type") not in requirement.definition.evidence_types:
                return "evidence_type_mismatch"
        return None

    @staticmethod
    def _inconclusive(requirement: ResolvedRequirement, finding: str) -> VerificationResult:
        return VerificationResult(
            requirement_id=requirement.definition.id,
            status=VerificationStatus.INCONCLUSIVE,
            findings=(finding,),
            verifier_id=requirement.definition.verifier_ref,
        )

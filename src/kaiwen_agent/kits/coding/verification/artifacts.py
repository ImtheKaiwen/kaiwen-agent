from __future__ import annotations

import json
from dataclasses import dataclass

from kaiwen_agent.artifacts import ArtifactManager, ArtifactMetadata
from kaiwen_agent.kits.coding.verification.evidence import EVIDENCE_ORIGIN, EvidenceSigner
from kaiwen_agent.quality import (
    VerificationContext,
    VerificationResult,
    VerificationStatus,
    VerifierRegistry,
)
from kaiwen_agent.standards import ResolvedRequirement


@dataclass(slots=True)
class JsonArtifactVerifier:
    """Evaluate current owner/run/fingerprint-scoped JSON evidence artifacts."""

    verifier_id: str
    artifacts: ArtifactManager
    require_zero_exit_code: bool = False
    # When set, only artifacts authenticated by the trusted execution path count, and
    # the verdict is derived from recorded execution facts, never from a status field.
    signer: EvidenceSigner | None = None

    async def verify(
        self,
        requirement: ResolvedRequirement,
        *,
        context: VerificationContext,
    ) -> VerificationResult:
        candidates = await self.artifacts.list(context.artifact_context(), limit=1_000)
        matches = [
            metadata
            for metadata in candidates
            if metadata.run_id == context.run_id
            and metadata.labels.get("requirement_id") == requirement.definition.id
            and metadata.labels.get("input_fingerprint") == context.protected_input_fingerprint
            and metadata.labels.get("evidence_type") in requirement.definition.evidence_types
        ]
        if not matches:
            return self._result(
                requirement,
                VerificationStatus.INCONCLUSIVE,
                findings=("current_evidence_not_found",),
            )
        matches.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        if self.signer is not None:
            return await self._verify_trusted(requirement, context, matches)
        metadata = matches[0]
        artifact = await self.artifacts.read(context.artifact_context(), metadata.id)
        try:
            payload = json.loads(artifact.content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._result(
                requirement,
                VerificationStatus.INCONCLUSIVE,
                evidence_refs=(metadata.id,),
                findings=("evidence_is_not_valid_json",),
            )
        if not isinstance(payload, dict):
            return self._result(
                requirement,
                VerificationStatus.INCONCLUSIVE,
                evidence_refs=(metadata.id,),
                findings=("evidence_payload_must_be_an_object",),
            )
        status = payload.get("status")
        if status in {"fail", "failed", "error"}:
            return self._result(
                requirement,
                VerificationStatus.FAIL,
                evidence_refs=(metadata.id,),
                findings=(str(payload.get("summary", "evidence_reported_failure"))[:1_000],),
            )
        if status not in {"pass", "passed", "success"}:
            return self._result(
                requirement,
                VerificationStatus.INCONCLUSIVE,
                evidence_refs=(metadata.id,),
                findings=("evidence_status_is_not_conclusive",),
            )
        if self.require_zero_exit_code and payload.get("exit_code") != 0:
            return self._result(
                requirement,
                VerificationStatus.FAIL,
                evidence_refs=(metadata.id,),
                findings=("evidence_exit_code_is_nonzero",),
            )
        return self._result(
            requirement,
            VerificationStatus.PASS,
            evidence_refs=(metadata.id,),
        )

    async def _verify_trusted(
        self,
        requirement: ResolvedRequirement,
        context: VerificationContext,
        matches: list[ArtifactMetadata],
    ) -> VerificationResult:
        assert self.signer is not None
        for metadata in matches:
            artifact = await self.artifacts.read(context.artifact_context(), metadata.id)
            if metadata.labels.get("evidence_origin") != EVIDENCE_ORIGIN or not self.signer.verify(
                tenant_id=metadata.tenant_id,
                owner_user_id=metadata.owner_user_id,
                run_id=metadata.run_id,
                content=artifact.content,
                labels=metadata.labels,
            ):
                continue
            try:
                payload = json.loads(artifact.content)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if (
                not isinstance(payload, dict)
                or payload.get("origin") != EVIDENCE_ORIGIN
                or payload.get("run_id") != context.run_id
                or payload.get("requirement_id") != requirement.definition.id
                or payload.get("snapshot_fingerprint") != context.protected_input_fingerprint
            ):
                continue
            if (
                payload.get("exit_code") == 0
                and payload.get("timed_out") is False
                and payload.get("cancelled") is False
                and payload.get("snapshot_unchanged") is True
            ):
                return self._result(
                    requirement, VerificationStatus.PASS, evidence_refs=(metadata.id,)
                )
            outcome = payload.get("outcome")
            reason = (
                outcome
                if outcome
                in {"failed", "timed_out", "cancelled", "snapshot_modified", "unverified"}
                else "failed"
            )
            return self._result(
                requirement,
                VerificationStatus.FAIL,
                evidence_refs=(metadata.id,),
                findings=(f"command_{reason}",),
            )
        return self._result(
            requirement, VerificationStatus.INCONCLUSIVE, findings=("evidence_not_trusted",)
        )

    def _result(
        self,
        requirement: ResolvedRequirement,
        status: VerificationStatus,
        *,
        evidence_refs: tuple[str, ...] = (),
        findings: tuple[str, ...] = (),
    ) -> VerificationResult:
        return VerificationResult(
            requirement_id=requirement.definition.id,
            status=status,
            evidence_refs=evidence_refs,
            findings=findings,
            verifier_id=self.verifier_id,
        )


def coding_verifiers(
    artifacts: ArtifactManager, *, signer: EvidenceSigner | None = None
) -> tuple[JsonArtifactVerifier, ...]:
    command_verifiers = {"coding.acceptance", "coding.tests"}
    identifiers = (
        "coding.acceptance",
        "coding.documentation",
        "coding.tests",
        "coding.architecture",
        "coding.security",
        "coding.performance",
        "coding.ci",
        "coding.data",
        "coding.api",
        "coding.ui_accessibility",
        "coding.ui_design",
    )
    return tuple(
        JsonArtifactVerifier(
            verifier_id=identifier,
            artifacts=artifacts,
            require_zero_exit_code=identifier in command_verifiers,
            signer=signer if identifier in command_verifiers else None,
        )
        for identifier in identifiers
    )


def install_coding_verifiers(
    registry: VerifierRegistry,
    artifacts: ArtifactManager,
    *,
    signer: EvidenceSigner | None = None,
) -> tuple[JsonArtifactVerifier, ...]:
    verifiers = coding_verifiers(artifacts, signer=signer)
    for verifier in verifiers:
        existing = registry.get(verifier.verifier_id)
        if existing is None:
            registry.register(verifier)
            continue
        if existing != verifier:
            raise ValueError(f"Conflicting coding verifier: {verifier.verifier_id}")
    return verifiers

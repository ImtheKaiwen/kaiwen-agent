from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kaiwen_agent.artifacts import ArtifactContext
from kaiwen_agent.types import utc_now


class VerificationStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    APPROVAL_REQUIRED = "approval_required"


class VerificationContext(BaseModel):
    """Trusted owner and protected-input identity for one gate evaluation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    protected_input_fingerprint: str = Field(min_length=1)

    def artifact_context(self) -> ArtifactContext:
        return ArtifactContext(
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            session_id=self.session_id,
            trace_id=self.trace_id,
            run_id=self.run_id,
        )


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_id: str = Field(min_length=1)
    status: VerificationStatus
    evidence_refs: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()
    verifier_id: str = Field(min_length=1)
    verified_at: datetime = Field(default_factory=utc_now)

    @field_validator("verified_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("verified_at must be timezone-aware")
        return value


class CompletionGateStatus(StrEnum):
    PASS = "pass"
    BLOCKED = "blocked"
    APPROVAL_REQUIRED = "approval_required"
    CONFIGURATION_ERROR = "configuration_error"


class CompletionGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: CompletionGateStatus
    verifications: tuple[VerificationResult, ...]
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def can_complete(self) -> bool:
        return self.status == CompletionGateStatus.PASS

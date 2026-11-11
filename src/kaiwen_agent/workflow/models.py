from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kaiwen_agent.quality import CompletionGateResult
from kaiwen_agent.runtime import RuntimeRunRecord
from kaiwen_agent.tasks import AgentTask


class WorkflowRetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_attempts: int = Field(default=1, ge=1, le=10)
    backoff_seconds: float = Field(default=0, ge=0, le=3_600)
    backoff_multiplier: float = Field(default=1, ge=1, le=10)
    retry_on: frozenset[str] = Field(default_factory=lambda: frozenset({"timeout"}))


class WorkflowTaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=4_000)
    worker_id: str = Field(min_length=1, max_length=200)
    dependencies: tuple[str, ...] = ()
    input: dict[str, Any] = Field(default_factory=dict)
    resource_keys: tuple[str, ...] = ()
    capabilities: frozenset[str] = Field(default_factory=frozenset)
    retry_policy: WorkflowRetryPolicy = Field(default_factory=WorkflowRetryPolicy)
    timeout_seconds: float | None = Field(default=None, gt=0, le=86_400)

    @model_validator(mode="after")
    def validate_sets(self) -> WorkflowTaskSpec:
        if len(self.dependencies) != len(set(self.dependencies)):
            raise ValueError("dependencies must not contain duplicates")
        if self.id in self.dependencies:
            raise ValueError("A workflow task cannot depend on itself")
        if len(self.resource_keys) != len(set(self.resource_keys)):
            raise ValueError("resource_keys must not contain duplicates")
        return self


class WorkflowPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(default=1, ge=1)
    objective: str = Field(min_length=1, max_length=10_000)
    tasks: tuple[WorkflowTaskSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_task_ids(self) -> WorkflowPlan:
        ids = [task.id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("Workflow task ids must be unique")
        return self


class WorkflowLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_tasks_per_plan: int = Field(default=50, ge=1, le=1_000)
    max_dependencies_per_task: int = Field(default=20, ge=0, le=1_000)
    max_input_bytes_per_task: int = Field(default=256_000, ge=1, le=10_000_000)
    max_cycles: int = Field(default=5, ge=1, le=100)


class WorkflowRevisionReason(StrEnum):
    EXECUTION_FAILED = "execution_failed"
    QUALITY_BLOCKED = "quality_blocked"
    # A host-controlled delegation source may propose more work at the cycle boundary (ADR 0015).
    # It is never reported as a task failure or a gate failure, and it carries neither.
    DELEGATION_REQUESTED = "delegation_requested"


class WorkflowTaskFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    logical_task_id: str
    error_code: str | None = None


class DelegationParent(BaseModel):
    """A task that completed in the cycle that just ended and so may be named as a parent.

    Every field is a host fact read from the controller's own task results.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    logical_task_id: str
    worker_id: str
    depth: int = Field(ge=0)


class DelegationBoundary(BaseModel):
    """What the controller tells a delegation source at the cycle boundary.

    The controller owns the run, the cycle and the eligible parents; a source can name only one
    of the listed parents and supplies untrusted task specs. Nothing here is authority.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    eligible_parents: tuple[DelegationParent, ...] = Field(default=(), max_length=1_000)
    # Delegation rounds already granted in this execution, and the most that may be granted.
    rounds_used: int = Field(default=0, ge=0)
    rounds_allowed: int = Field(default=1, ge=1)


class WorkflowRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    cycle: int = Field(ge=1)
    reason: WorkflowRevisionReason
    prior_plan: WorkflowPlan
    task_failures: tuple[WorkflowTaskFailure, ...] = ()
    quality_result: CompletionGateResult | None = None
    # Present exactly when the reason is DELEGATION_REQUESTED.
    delegation: DelegationBoundary | None = None

    @model_validator(mode="after")
    def delegation_is_never_a_failure(self) -> WorkflowRevisionRequest:
        delegating = self.reason is WorkflowRevisionReason.DELEGATION_REQUESTED
        if delegating != (self.delegation is not None):
            raise ValueError("delegation data belongs to a delegation request and only to it")
        if delegating and (self.task_failures or self.quality_result is not None):
            raise ValueError("A delegation request carries no task failure and no gate result")
        return self


class TeamAcceptanceStatus(StrEnum):
    """The host's decision about delivering a whole team's work (ADR 0017).

    Distinct from a task reporting completion, a reviewer's recommendation and the completion
    gate's verdict; only the host computes it, and only `ACCEPTED` lets the run complete.
    """

    ACCEPTED = "accepted"
    CHANGES_REQUIRED = "changes_required"
    APPROVAL_REQUIRED = "approval_required"
    BLOCKED = "blocked"
    # The host cannot establish the facts (missing or corrupted review, unreadable inventory,
    # no fingerprint, an unreachable approval source). Never counted as success.
    UNAVAILABLE = "unavailable"


class TeamAcceptanceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: TeamAcceptanceStatus
    code: str = Field(pattern=r"^[a-z_]{1,64}$")
    reasons: tuple[str, ...] = Field(default=(), max_length=20)
    reviewer_task_ids: tuple[str, ...] = Field(default=(), max_length=20)
    # What a human approval (when required) is bound to; absent when none was evaluated.
    subject_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @property
    def accepted(self) -> bool:
        return self.status is TeamAcceptanceStatus.ACCEPTED


class WorkflowOutcomeStatus(StrEnum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class WorkflowOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: WorkflowOutcomeStatus
    run: RuntimeRunRecord
    cycles: int = Field(ge=1)
    tasks: tuple[AgentTask, ...]
    quality_result: CompletionGateResult | None = None
    reason: str | None = None
    # The protected-input fingerprint the completion gate actually evaluated.
    verified_input_fingerprint: str | None = None
    # Present only when team acceptance is configured and was evaluated for this outcome.
    team_acceptance: TeamAcceptanceDecision | None = None

"""Shared, host-controlled delegation admission (ADR 0015, section 4).

`DelegationAdmission` answers one question: is a delegation of this shape admitted right now. It
is used by `SubagentDelegator` (job delegation) and by the workflow admission adapter. It adds no
policy engine: the decision comes from the central `PolicyEngine` through the `agent.delegate`
action, and only an explicit ALLOW admits.

What it is not:

- It does not submit, create, revise or run anything. It has no job manager, no task store and no
  scheduler. Authorization is separate from execution.
- Its answer is a preliminary, point-in-time evaluation. `AdmissionDecision.preliminary` is always
  true and `reserves_nothing` is always true: no counter or lock is held, a later submission is
  not guaranteed to stay authorized, and concurrent evaluations can all be admitted. An
  authoritative final admission, with fresh checks, belongs where work becomes executable.

Trust: a `DelegationClaim` is assembled by the host from trusted records (the caller's identity,
the parent job or task, counters read from the stores). Only `requested_capabilities`,
`resource_keys` and `delegate_id` come from a model-authored request, and each is checked against
host-supplied bounds. REQUIRE_APPROVAL, REVIEW and MODIFY never admit and never modify the request
(a MODIFY decision's modifications are discarded). A policy that raises fails closed with a stable,
sanitized outcome; its message is never returned or logged. Cancellation is not swallowed.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.policy.engine import PolicyEngine
from kaiwen_agent.policy.models import PolicyContext, PolicyDecisionType

logger = logging.getLogger(__name__)

_MAX_REASON = 300
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


class AdmissionOutcome(StrEnum):
    ADMITTED = "admitted"
    DENIED = "denied"
    APPROVAL_REQUIRED = "approval_required"


class AdmissionDecision(BaseModel):
    """A preliminary admission answer. `code` is stable; `reason` is deterministic text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: AdmissionOutcome
    # admitted, invalid_trusted_state, capability_escalation, capability_exceeds_parent,
    # resource_not_allowed, policy_denied, policy_unavailable, approval_required,
    # review_required, modification_required.
    code: str = Field(pattern=r"^[a-z_]{1,64}$")
    reason: str = Field(min_length=1, max_length=_MAX_REASON)
    # Host-derived: parent depth plus one. Only set when admitted.
    child_depth: int | None = Field(default=None, ge=1)
    policy_id: str | None = None
    rule_id: str | None = None
    # Always true: this is not a reservation and not a final authorization (ADR 0015, 4.2).
    preliminary: Literal[True] = True
    reserves_nothing: Literal[True] = True

    @property
    def admitted(self) -> bool:
        return self.outcome is AdmissionOutcome.ADMITTED


@dataclass(frozen=True, slots=True)
class DelegationClaim:
    """Host-assembled facts about one delegation. Never taken from model-authored fields.

    `parent_depth` is the depth of the delegating parent (what policies compare to a maximum);
    the child's depth is `parent_depth + 1`, computed here. `resource_allowlist=None` means
    resource keys are not enforced (the legacy job path); the workflow path always passes one.
    """

    tenant_id: str
    user_id: str
    session_id: str
    agent_id: str
    delegate_id: str
    parent_depth: int
    active_delegations: int
    delegations_in_run: int
    caller_capabilities: frozenset[str]
    requested_capabilities: frozenset[str]
    parent_capabilities: frozenset[str] | None = None
    resource_keys: tuple[str, ...] = ()
    resource_allowlist: frozenset[str] | None = None
    conversation_id: str | None = None
    run_id: str | None = None
    trace_id: str | None = None
    parent_id: str | None = None
    kind: Literal["job", "workflow"] = "job"


class DelegationAdmission:
    """Evaluates a `DelegationClaim` against host bounds and the central `PolicyEngine`."""

    def __init__(self, *, policy_engine: PolicyEngine) -> None:
        self.policy_engine = policy_engine

    async def evaluate(self, claim: DelegationClaim) -> AdmissionDecision:
        if not _valid(claim):
            return _deny("invalid_trusted_state", "Delegation facts are missing or malformed")
        if not claim.requested_capabilities <= claim.caller_capabilities:
            return _deny(
                "capability_escalation", "Requested capabilities exceed caller capabilities"
            )
        if (
            claim.parent_capabilities is not None
            and not claim.requested_capabilities <= claim.parent_capabilities
        ):
            return _deny(
                "capability_exceeds_parent", "Requested capabilities exceed parent capabilities"
            )
        if claim.resource_allowlist is not None and not set(claim.resource_keys) <= (
            claim.resource_allowlist
        ):
            return _deny("resource_not_allowed", "Requested resource keys are not allowlisted")
        failure: str | None = None
        try:
            decision = await self.policy_engine.evaluate(_policy_context(claim))
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - fail closed; the message is never used
            failure = type(error).__name__
        if failure is not None:  # handled outside the handler so nothing is chained
            logger.warning(
                "delegation policy failure error_type=%s agent=%s", failure, claim.agent_id
            )
            return _deny("policy_unavailable", "Delegation policy could not be evaluated")
        reason = _clean(decision.reason)
        policy_id, rule_id = decision.policy_id, decision.rule_id
        match decision.decision:
            case PolicyDecisionType.ALLOW:
                return AdmissionDecision(
                    outcome=AdmissionOutcome.ADMITTED,
                    code="admitted",
                    reason=reason,
                    child_depth=claim.parent_depth + 1,
                    policy_id=policy_id,
                    rule_id=rule_id,
                )
            case PolicyDecisionType.REQUIRE_APPROVAL:
                return _approval("approval_required", reason, policy_id, rule_id)
            case PolicyDecisionType.REVIEW:
                return _approval("review_required", reason, policy_id, rule_id)
            case PolicyDecisionType.MODIFY:
                # The modifications are intentionally discarded: policy never rewrites a request.
                return _approval("modification_required", reason, policy_id, rule_id)
            case _:
                return AdmissionDecision(
                    outcome=AdmissionOutcome.DENIED,
                    code="policy_denied",
                    reason=reason,
                    policy_id=policy_id,
                    rule_id=rule_id,
                )


def _policy_context(claim: DelegationClaim) -> PolicyContext:
    metadata: dict[str, object] = {
        "delegation_depth": claim.parent_depth,
        "parallel_jobs": claim.active_delegations,
        "jobs_in_run": claim.delegations_in_run,
        "requested_capabilities": sorted(claim.requested_capabilities),
    }
    if claim.kind == "job":
        metadata["parent_job_id"] = claim.parent_id
    else:
        metadata["parent_task_id"] = claim.parent_id
        metadata["delegation_kind"] = "workflow"
    return PolicyContext(
        tenant_id=claim.tenant_id,
        user_id=claim.user_id,
        agent_id=claim.agent_id,
        session_id=claim.session_id,
        conversation_id=claim.conversation_id,
        run_id=claim.run_id,
        trace_id=claim.trace_id or "delegation",
        action="agent.delegate",
        resource=claim.delegate_id,
        capabilities=claim.caller_capabilities,
        metadata=metadata,
    )


def _valid(claim: DelegationClaim) -> bool:
    """Whether every host-supplied fact is present and well formed (fail closed otherwise)."""

    strings = (
        claim.tenant_id,
        claim.user_id,
        claim.session_id,
        claim.agent_id,
        claim.delegate_id,
    )
    counters = (claim.parent_depth, claim.active_delegations, claim.delegations_in_run)
    sets = (claim.caller_capabilities, claim.requested_capabilities)
    return (
        all(isinstance(value, str) and value for value in strings)
        and all(type(value) is int and value >= 0 for value in counters)
        and all(
            isinstance(group, frozenset) and all(isinstance(item, str) for item in group)
            for group in sets
        )
        and (
            claim.parent_capabilities is None
            or (
                isinstance(claim.parent_capabilities, frozenset)
                and all(isinstance(item, str) for item in claim.parent_capabilities)
            )
        )
        and isinstance(claim.resource_keys, tuple)
        and all(isinstance(key, str) and key for key in claim.resource_keys)
        and (
            claim.resource_allowlist is None
            or (
                isinstance(claim.resource_allowlist, frozenset)
                and all(isinstance(item, str) for item in claim.resource_allowlist)
            )
        )
    )


def _deny(code: str, reason: str) -> AdmissionDecision:
    return AdmissionDecision(outcome=AdmissionOutcome.DENIED, code=code, reason=reason)


def _approval(code: str, reason: str, policy_id: str, rule_id: str) -> AdmissionDecision:
    return AdmissionDecision(
        outcome=AdmissionOutcome.APPROVAL_REQUIRED,
        code=code,
        reason=reason,
        policy_id=policy_id,
        rule_id=rule_id,
    )


def _clean(reason: str) -> str:
    """A host-authored policy reason as safe single-line text, bounded; never empty."""

    text = _CONTROL.sub(" ", reason).strip()[:_MAX_REASON]
    return text or "Delegation policy decision"

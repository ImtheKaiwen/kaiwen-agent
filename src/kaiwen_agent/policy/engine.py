from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable

from kaiwen_agent.audit import AuditRecord, AuditStore, RedactingAuditStore
from kaiwen_agent.events import AgentEvent, EventSink, EventType
from kaiwen_agent.policy.models import (
    PolicyContext,
    PolicyDecision,
    PolicyDecisionType,
)
from kaiwen_agent.policy.registry import PolicyRegistry, PolicyRule

_PRECEDENCE = {
    PolicyDecisionType.ALLOW: 1,
    PolicyDecisionType.MODIFY: 2,
    PolicyDecisionType.REVIEW: 3,
    PolicyDecisionType.REQUIRE_APPROVAL: 4,
    PolicyDecisionType.DENY: 5,
}

_EVENT_TYPES: dict[PolicyDecisionType, EventType] = {
    PolicyDecisionType.ALLOW: "policy.allowed",
    PolicyDecisionType.DENY: "policy.denied",
    PolicyDecisionType.REQUIRE_APPROVAL: "policy.approval_required",
    PolicyDecisionType.REVIEW: "policy.review_required",
    PolicyDecisionType.MODIFY: "policy.modified",
}

PolicyEventCallback = Callable[[EventType, dict[str, object]], Awaitable[None]]


class PolicyEngine:
    def __init__(
        self,
        *,
        registry: PolicyRegistry | None = None,
        audit_store: AuditStore | None = None,
        events: EventSink | None = None,
        default_decision: PolicyDecisionType = PolicyDecisionType.DENY,
    ) -> None:
        self.registry = registry or PolicyRegistry()
        self.audit_store = RedactingAuditStore(audit_store) if audit_store is not None else None
        self.events = events
        self.default_decision = default_decision

    async def evaluate(
        self,
        context: PolicyContext,
        *,
        extra_policies: Iterable[PolicyRule] = (),
        emit: PolicyEventCallback | None = None,
    ) -> PolicyDecision:
        decisions: list[PolicyDecision] = []
        for policy in (*self.registry.policies(), *tuple(extra_policies)):
            decision = await policy.evaluate(context)
            if decision is None:
                continue
            decisions.append(decision)
            await self._record(context, decision, emit=emit)
        if not decisions:
            decision = PolicyDecision(
                decision=self.default_decision,
                policy_id="policy-engine",
                rule_id="default",
                reason=f"Default policy decision: {self.default_decision.value}",
            )
            await self._record(context, decision, emit=emit)
            return decision
        return self._resolve(decisions)

    @staticmethod
    def _resolve(decisions: list[PolicyDecision]) -> PolicyDecision:
        selected = max(decisions, key=lambda item: _PRECEDENCE[item.decision])
        modifications: dict[str, object] = {}
        for decision in decisions:
            if decision.modifications:
                modifications.update(decision.modifications)
        if modifications:
            return selected.model_copy(update={"modifications": modifications})
        return selected

    async def _record(
        self,
        context: PolicyContext,
        decision: PolicyDecision,
        *,
        emit: PolicyEventCallback | None,
    ) -> None:
        details = {
            "policy_id": decision.policy_id,
            "rule_id": decision.rule_id,
            "decision": decision.decision.value,
            "reason": decision.reason,
            "agent_id": context.agent_id,
            "tenant_id": context.tenant_id,
            "user_id": context.user_id,
            "resource": context.resource,
            "run_id": context.run_id,
            "job_id": context.job_id,
            "modifications": decision.modifications,
            "approval_scope": decision.approval_scope,
        }
        if self.audit_store is not None:
            await self.audit_store.append_audit(
                AuditRecord(
                    trace_id=context.trace_id,
                    session_id=context.session_id,
                    action=f"policy:{context.action}",
                    outcome=(
                        "denied" if decision.decision == PolicyDecisionType.DENY else "succeeded"
                    ),
                    details=details,
                )
            )
        payload: dict[str, object] = {"action": context.action, **details}
        if emit is not None:
            await emit(_EVENT_TYPES[decision.decision], payload)
        elif self.events is not None:
            await self.events.append(
                AgentEvent(
                    type=_EVENT_TYPES[decision.decision],
                    trace_id=context.trace_id,
                    session_id=context.session_id,
                    tenant_id=context.tenant_id,
                    user_id=context.user_id,
                    payload=payload,
                )
            )

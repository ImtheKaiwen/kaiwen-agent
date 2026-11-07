from __future__ import annotations

from datetime import datetime
from typing import Any

from kaiwen_agent.budgets.models import (
    BudgetAction,
    BudgetContext,
    BudgetOutcome,
    BudgetRule,
    BudgetScope,
    BudgetUsage,
)
from kaiwen_agent.budgets.store import (
    BudgetReservationRequest,
    BudgetStore,
    InMemoryBudgetStore,
)
from kaiwen_agent.model_routing import ModelTier
from kaiwen_agent.policy import PolicyContext, PolicyDecisionType, PolicyEngine
from kaiwen_agent.types import utc_now


class BudgetEngine:
    def __init__(
        self,
        *,
        rules: list[BudgetRule],
        policy_engine: PolicyEngine,
        store: BudgetStore | None = None,
    ) -> None:
        ids = [rule.id for rule in rules]
        if len(ids) != len(set(ids)):
            raise ValueError("Budget rule ids must be unique")
        self.rules = tuple(rules)
        self.policy_engine = policy_engine
        self.store = store or InMemoryBudgetStore()

    async def consume(
        self,
        context: BudgetContext,
        delta: BudgetUsage,
        *,
        now: datetime | None = None,
    ) -> BudgetOutcome:
        observed_at = now or utc_now()
        requests = tuple(
            BudgetReservationRequest(key=_scope_key(rule.scope, context, observed_at), rule=rule)
            for rule in self.rules
        )
        reservation = await self.store.reserve(requests, delta)
        if not reservation.reserved:
            by_id = {rule.id: rule for rule in self.rules}
            exceeded = [by_id[rule_id] for rule_id in reservation.exceeded_rule_ids]
            decision = await self.policy_engine.evaluate(
                PolicyContext(
                    tenant_id=context.tenant_id,
                    user_id=context.user_id,
                    agent_id=context.agent_id,
                    session_id=context.session_id,
                    conversation_id=context.conversation_id,
                    run_id=context.run_id,
                    job_id=context.job_id,
                    trace_id=context.trace_id,
                    action="budget.exceeded",
                    resource=exceeded[0].id,
                    metadata={
                        "exceeded_rule_ids": [rule.id for rule in exceeded],
                        "delta": delta.model_dump(),
                    },
                )
            )
            return _outcome(decision.decision, decision.modifications, exceeded)
        return BudgetOutcome(allowed=True, action=BudgetAction.ALLOW)


def _scope_key(scope: BudgetScope, context: BudgetContext, now: datetime) -> str:
    values = {
        BudgetScope.RUN: context.run_id,
        BudgetScope.JOB: context.job_id,
        BudgetScope.AGENT: context.agent_id,
        BudgetScope.USER: context.user_id,
        BudgetScope.TENANT: context.tenant_id,
        BudgetScope.DAY: now.date().isoformat(),
    }
    value = values[scope]
    if value is None:
        raise ValueError(f"Budget scope {scope.value} requires its trusted identifier")
    return f"{context.tenant_id}:{scope.value}:{value}"


def _outcome(
    decision: PolicyDecisionType,
    modifications: dict[str, Any] | None,
    exceeded: list[BudgetRule],
) -> BudgetOutcome:
    ids = [rule.id for rule in exceeded]
    if decision == PolicyDecisionType.MODIFY:
        tiers = (modifications or {}).get("allowed_model_tiers")
        if isinstance(tiers, list) and all(isinstance(item, str) for item in tiers):
            try:
                allowed_tiers = frozenset(ModelTier(item) for item in tiers)
            except ValueError:
                pass
            else:
                if allowed_tiers:
                    return BudgetOutcome(
                        allowed=False,
                        action=BudgetAction.DEGRADE_MODEL,
                        exceeded_rule_ids=ids,
                        allowed_model_tiers=allowed_tiers,
                    )
    if decision in {PolicyDecisionType.REQUIRE_APPROVAL, PolicyDecisionType.REVIEW}:
        return BudgetOutcome(
            allowed=False,
            action=(
                BudgetAction.REQUEST_APPROVAL
                if decision == PolicyDecisionType.REQUIRE_APPROVAL
                else BudgetAction.PAUSE
            ),
            exceeded_rule_ids=ids,
        )
    return BudgetOutcome(
        allowed=False,
        action=BudgetAction.DENY,
        exceeded_rule_ids=ids,
    )

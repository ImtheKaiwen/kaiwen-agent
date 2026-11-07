from __future__ import annotations

from pydantic import TypeAdapter, ValidationError

from kaiwen_agent.policy.models import (
    PolicyContext,
    PolicyDecision,
    PolicyDecisionType,
)

_NON_NEGATIVE_INT = TypeAdapter(int)


class RuntimePolicy:
    def __init__(
        self,
        *,
        max_actions_per_cycle: int,
        policy_id: str = "runtime",
    ) -> None:
        if max_actions_per_cycle < 1:
            raise ValueError("max_actions_per_cycle must be at least 1")
        self.policy_id = policy_id
        self.max_actions_per_cycle = max_actions_per_cycle

    async def evaluate(self, context: PolicyContext) -> PolicyDecision | None:
        if context.action != "runtime.action":
            return None
        try:
            actions = _NON_NEGATIVE_INT.validate_python(
                context.metadata.get("actions_in_cycle", 0), strict=True
            )
        except ValidationError:
            return PolicyDecision(
                decision=PolicyDecisionType.DENY,
                policy_id=self.policy_id,
                rule_id="actions-per-cycle",
                reason="Runtime action count is invalid",
            )
        if actions < 0:
            return PolicyDecision(
                decision=PolicyDecisionType.DENY,
                policy_id=self.policy_id,
                rule_id="actions-per-cycle",
                reason="Runtime action count cannot be negative",
            )
        if actions >= self.max_actions_per_cycle:
            return PolicyDecision(
                decision=PolicyDecisionType.DENY,
                policy_id=self.policy_id,
                rule_id="actions-per-cycle",
                reason="Maximum actions per lifecycle cycle reached",
            )
        return PolicyDecision(
            decision=PolicyDecisionType.ALLOW,
            policy_id=self.policy_id,
            rule_id="actions-per-cycle",
            reason="Runtime action is within the configured limit",
        )

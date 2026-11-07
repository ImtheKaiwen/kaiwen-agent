from __future__ import annotations

from collections.abc import Mapping

from kaiwen_agent.policy.models import (
    PolicyContext,
    PolicyDecision,
    PolicyDecisionType,
)


class CapabilityPolicy:
    def __init__(
        self,
        requirements: Mapping[str, frozenset[str] | set[str]],
        *,
        policy_id: str = "capability",
    ) -> None:
        self.policy_id = policy_id
        self.requirements = {
            action: frozenset(capabilities) for action, capabilities in requirements.items()
        }

    async def evaluate(self, context: PolicyContext) -> PolicyDecision | None:
        required = self.requirements.get(context.action)
        if required is None:
            required = self.requirements.get("*")
        if required is None:
            return None
        missing = required - context.capabilities
        if missing:
            return PolicyDecision(
                decision=PolicyDecisionType.DENY,
                policy_id=self.policy_id,
                rule_id="required-capabilities",
                reason=f"Missing capabilities: {', '.join(sorted(missing))}",
            )
        return PolicyDecision(
            decision=PolicyDecisionType.ALLOW,
            policy_id=self.policy_id,
            rule_id="required-capabilities",
            reason="All required capabilities are present",
        )

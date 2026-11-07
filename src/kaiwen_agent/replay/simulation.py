from __future__ import annotations

from kaiwen_agent.policy import PolicyContext, PolicyDecision, PolicyDecisionType


class SimulationSideEffectPolicy:
    """Fail-closed policy for replay/what-if runs.

    Register it in the central PolicyEngine and use environment="simulation".
    """

    policy_id = "simulation-side-effects"

    async def evaluate(self, context: PolicyContext) -> PolicyDecision | None:
        if context.environment != "simulation":
            return None
        side_effect = context.metadata.get("side_effect")
        external_write = context.metadata.get("external_write") is True
        if side_effect in {"reversible", "destructive"} or external_write:
            return PolicyDecision(
                decision=PolicyDecisionType.DENY,
                policy_id=self.policy_id,
                rule_id="deny-side-effect",
                reason="Simulation cannot execute external side effects",
            )
        return None

from __future__ import annotations

from kaiwen_agent.policy.models import (
    PolicyContext,
    PolicyDecision,
    PolicyDecisionType,
)


class ToolPermissionPolicy:
    policy_id = "tool-permissions"

    def __init__(self, required: frozenset[str]) -> None:
        self.required = required

    async def evaluate(self, context: PolicyContext) -> PolicyDecision:
        missing = self.required - context.capabilities
        if missing:
            return PolicyDecision(
                decision=PolicyDecisionType.DENY,
                policy_id=self.policy_id,
                rule_id="required-permissions",
                reason=f"Missing permissions: {', '.join(sorted(missing))}",
            )
        return PolicyDecision(
            decision=PolicyDecisionType.ALLOW,
            policy_id=self.policy_id,
            rule_id="required-permissions",
            reason="All tool permissions are present",
        )


class ToolApprovalPolicy:
    policy_id = "tool-approval"

    def __init__(self, *, required: bool, approval_scope: str | None = None) -> None:
        self.required = required
        self.approval_scope = approval_scope

    async def evaluate(self, context: PolicyContext) -> PolicyDecision:
        del context
        if self.required:
            return PolicyDecision(
                decision=PolicyDecisionType.REQUIRE_APPROVAL,
                policy_id=self.policy_id,
                rule_id="tool-definition",
                reason="Tool definition requires approval",
                approval_scope=self.approval_scope,
            )
        return PolicyDecision(
            decision=PolicyDecisionType.ALLOW,
            policy_id=self.policy_id,
            rule_id="tool-definition",
            reason="Tool definition does not require approval",
        )

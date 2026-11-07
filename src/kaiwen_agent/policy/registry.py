from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from kaiwen_agent.policy.models import PolicyContext, PolicyDecision


class PolicyRule(Protocol):
    policy_id: str

    async def evaluate(self, context: PolicyContext) -> PolicyDecision | None: ...


class PolicyRegistry:
    def __init__(self, policies: Iterable[PolicyRule] = ()) -> None:
        self._policies: dict[str, PolicyRule] = {}
        for policy in policies:
            self.register(policy)

    def register(self, policy: PolicyRule) -> None:
        if policy.policy_id in self._policies:
            raise ValueError(f"Policy already registered: {policy.policy_id}")
        self._policies[policy.policy_id] = policy

    def get(self, policy_id: str) -> PolicyRule:
        try:
            return self._policies[policy_id]
        except KeyError as error:
            raise KeyError(f"Unknown policy: {policy_id}") from error

    def policies(self) -> tuple[PolicyRule, ...]:
        return tuple(self._policies.values())

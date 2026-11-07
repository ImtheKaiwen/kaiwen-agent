from __future__ import annotations

from pydantic import TypeAdapter, ValidationError

from kaiwen_agent.policy.models import (
    PolicyContext,
    PolicyDecision,
    PolicyDecisionType,
)

_INTEGER = TypeAdapter(int)


class DelegationPolicy:
    def __init__(
        self,
        *,
        max_depth: int,
        max_parallel_jobs: int,
        max_jobs_per_run: int | None = None,
        allowed_delegates: frozenset[str] | set[str] = frozenset(),
        policy_id: str = "delegation",
    ) -> None:
        if max_depth < 0:
            raise ValueError("max_depth cannot be negative")
        if max_parallel_jobs < 1:
            raise ValueError("max_parallel_jobs must be at least 1")
        if max_jobs_per_run is not None and max_jobs_per_run < 1:
            raise ValueError("max_jobs_per_run must be at least 1")
        self.policy_id = policy_id
        self.max_depth = max_depth
        self.max_parallel_jobs = max_parallel_jobs
        self.max_jobs_per_run = max_jobs_per_run
        self.allowed_delegates = frozenset(allowed_delegates)

    async def evaluate(self, context: PolicyContext) -> PolicyDecision | None:
        if context.action != "agent.delegate":
            return None
        try:
            depth = _INTEGER.validate_python(context.metadata.get("delegation_depth"), strict=True)
            parallel = _INTEGER.validate_python(
                context.metadata.get("parallel_jobs", 0), strict=True
            )
            jobs_in_run = _INTEGER.validate_python(
                context.metadata.get("jobs_in_run", 0), strict=True
            )
        except ValidationError:
            return self._deny("Delegation metadata is invalid")
        if depth < 0 or parallel < 0 or jobs_in_run < 0:
            return self._deny("Delegation counters cannot be negative")
        delegate = context.resource
        if delegate is None or delegate not in self.allowed_delegates:
            return self._deny("Delegate is not allowlisted")
        if depth >= self.max_depth:
            return self._deny("Maximum delegation depth reached")
        if parallel >= self.max_parallel_jobs:
            return self._deny("Maximum parallel delegated jobs reached")
        if self.max_jobs_per_run is not None and jobs_in_run >= self.max_jobs_per_run:
            return self._deny("Maximum delegated jobs per run reached")
        return PolicyDecision(
            decision=PolicyDecisionType.ALLOW,
            policy_id=self.policy_id,
            rule_id="delegation-limits",
            reason="Delegation is within configured limits",
        )

    def _deny(self, reason: str) -> PolicyDecision:
        return PolicyDecision(
            decision=PolicyDecisionType.DENY,
            policy_id=self.policy_id,
            rule_id="delegation-limits",
            reason=reason,
        )

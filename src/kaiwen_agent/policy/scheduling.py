from __future__ import annotations

from pydantic import TypeAdapter, ValidationError

from kaiwen_agent.policy.models import (
    PolicyContext,
    PolicyDecision,
    PolicyDecisionType,
)

_NUMBER = TypeAdapter(float)
_INTEGER = TypeAdapter(int)


class SchedulingPolicy:
    def __init__(
        self,
        *,
        min_interval_seconds: float,
        max_interval_seconds: float,
        max_wakeups_per_hour: int,
        policy_id: str = "scheduling",
    ) -> None:
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds cannot be negative")
        if max_interval_seconds < min_interval_seconds:
            raise ValueError("max_interval_seconds must be at least the minimum")
        if max_wakeups_per_hour < 1:
            raise ValueError("max_wakeups_per_hour must be at least 1")
        self.policy_id = policy_id
        self.min_interval_seconds = min_interval_seconds
        self.max_interval_seconds = max_interval_seconds
        self.max_wakeups_per_hour = max_wakeups_per_hour

    async def evaluate(self, context: PolicyContext) -> PolicyDecision | None:
        if context.action != "runtime.schedule_wakeup":
            return None
        try:
            interval = _NUMBER.validate_python(
                context.metadata.get("interval_seconds"), strict=True
            )
            wakeups = _INTEGER.validate_python(
                context.metadata.get("wakeups_last_hour", 0), strict=True
            )
        except ValidationError:
            return self._deny("Scheduling metadata is invalid")
        if wakeups < 0:
            return self._deny("Hourly wakeup count cannot be negative")
        if wakeups >= self.max_wakeups_per_hour:
            return self._deny("Hourly wakeup limit reached")
        clamped = min(max(interval, self.min_interval_seconds), self.max_interval_seconds)
        if clamped != interval:
            return PolicyDecision(
                decision=PolicyDecisionType.MODIFY,
                policy_id=self.policy_id,
                rule_id="schedule-bounds",
                reason="Wakeup interval was clamped to policy bounds",
                modifications={"interval_seconds": clamped},
            )
        return PolicyDecision(
            decision=PolicyDecisionType.ALLOW,
            policy_id=self.policy_id,
            rule_id="schedule-bounds",
            reason="Wakeup schedule is within policy bounds",
        )

    def _deny(self, reason: str) -> PolicyDecision:
        return PolicyDecision(
            decision=PolicyDecisionType.DENY,
            policy_id=self.policy_id,
            rule_id="schedule-bounds",
            reason=reason,
        )

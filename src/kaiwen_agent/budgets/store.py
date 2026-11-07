from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from kaiwen_agent.budgets.models import BudgetRule, BudgetUsage


@dataclass(frozen=True, slots=True)
class BudgetReservationRequest:
    key: str
    rule: BudgetRule


@dataclass(frozen=True, slots=True)
class BudgetReservationResult:
    reserved: bool
    exceeded_rule_ids: tuple[str, ...] = ()


class BudgetStore(Protocol):
    async def get_usage(self, key: str) -> BudgetUsage: ...

    async def save_usage(self, key: str, usage: BudgetUsage) -> None: ...

    async def reserve(
        self,
        requests: tuple[BudgetReservationRequest, ...],
        delta: BudgetUsage,
    ) -> BudgetReservationResult: ...


class InMemoryBudgetStore:
    def __init__(self) -> None:
        self._usage: dict[str, BudgetUsage] = {}
        self._lock = asyncio.Lock()

    async def get_usage(self, key: str) -> BudgetUsage:
        async with self._lock:
            return self._usage.get(key, BudgetUsage()).model_copy(deep=True)

    async def save_usage(self, key: str, usage: BudgetUsage) -> None:
        async with self._lock:
            self._usage[key] = usage.model_copy(deep=True)

    async def reserve(
        self,
        requests: tuple[BudgetReservationRequest, ...],
        delta: BudgetUsage,
    ) -> BudgetReservationResult:
        async with self._lock:
            projected = {
                request.key: self._usage.get(request.key, BudgetUsage()).plus(delta)
                for request in requests
            }
            exceeded = tuple(
                request.rule.id
                for request in requests
                if exceeds_budget(projected[request.key], request.rule)
            )
            if exceeded:
                return BudgetReservationResult(
                    reserved=False,
                    exceeded_rule_ids=exceeded,
                )
            for key, usage in projected.items():
                self._usage[key] = usage.model_copy(deep=True)
            return BudgetReservationResult(reserved=True)


def exceeds_budget(usage: BudgetUsage, rule: BudgetRule) -> bool:
    limits = rule.limits
    checks = (
        (usage.input_tokens, limits.max_input_tokens),
        (usage.output_tokens, limits.max_output_tokens),
        (usage.tool_calls, limits.max_tool_calls),
        (usage.cost_usd, limits.max_cost_usd),
        (usage.jobs, limits.max_jobs),
    )
    return any(limit is not None and value > limit for value, limit in checks)

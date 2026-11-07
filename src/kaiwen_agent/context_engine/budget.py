from __future__ import annotations

from collections import defaultdict

from kaiwen_agent.context_engine.models import (
    BuiltContext,
    ContextBudget,
    ContextItem,
    ContextKind,
)
from kaiwen_agent.context_engine.token_counter import EstimatedTokenCounter, TokenCounter


class ContextBudgetAllocator:
    def __init__(self, token_counter: TokenCounter | None = None) -> None:
        self.token_counter = token_counter or EstimatedTokenCounter()

    def allocate(self, items: list[ContextItem], budget: ContextBudget) -> BuiltContext:
        ordered = sorted(
            items,
            key=lambda item: (
                not item.required,
                -item.priority,
                -item.relevance,
            ),
        )
        used_by_kind: defaultdict[ContextKind, int] = defaultdict(int)
        total = 0
        accepted: list[ContextItem] = []
        truncated = False

        for item in ordered:
            source_remaining = budget.limit_for(item.kind) - used_by_kind[item.kind]
            total_remaining = budget.max_total_tokens - total
            available = min(source_remaining, total_remaining)
            if available <= 0:
                truncated = True
                continue
            token_count = self.token_counter.count(item.content)
            content = item.content
            if token_count > available:
                content = self.token_counter.truncate(content, available)
                token_count = self.token_counter.count(content) if content else 0
                truncated = True
            if not content:
                continue
            accepted.append(item.model_copy(update={"content": content}))
            used_by_kind[item.kind] += token_count
            total += token_count

        return BuiltContext(
            items=accepted,
            estimated_tokens=total,
            truncated=truncated,
        )

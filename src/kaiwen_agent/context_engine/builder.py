from __future__ import annotations

import asyncio

from kaiwen_agent.context_engine.budget import ContextBudgetAllocator
from kaiwen_agent.context_engine.models import (
    BuiltContext,
    ContextBudget,
    ContextBuildRequest,
    ContextItem,
    ContextKind,
)
from kaiwen_agent.context_engine.sources import ContextSource


class ContextSourceError(RuntimeError):
    pass


class ContextBuilder:
    def __init__(
        self,
        sources: list[ContextSource] | tuple[ContextSource, ...] = (),
        *,
        allocator: ContextBudgetAllocator | None = None,
    ) -> None:
        names = [source.name for source in sources]
        if len(names) != len(set(names)):
            raise ValueError("Context source names must be unique")
        self.sources = tuple(sources)
        self.allocator = allocator or ContextBudgetAllocator()

    async def build(
        self,
        request: ContextBuildRequest,
        *,
        budget: ContextBudget | None = None,
    ) -> BuiltContext:
        results = await asyncio.gather(
            *(source.collect(request) for source in self.sources),
            return_exceptions=True,
        )
        items = [
            ContextItem(
                source="current_input",
                kind=ContextKind.CURRENT_INPUT,
                content=request.query,
                priority=900,
                required=True,
            )
        ]
        omitted: list[str] = []
        for source, result in zip(self.sources, results, strict=True):
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if not isinstance(result, Exception):
                    raise result
                if source.required:
                    raise ContextSourceError(
                        f"Required context source failed: {source.name}"
                    ) from result
                omitted.append(source.name)
                continue
            items.extend(result)
        built = self.allocator.allocate(items, budget or ContextBudget())
        return built.model_copy(update={"omitted_sources": omitted})

from __future__ import annotations

from collections import Counter

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.memory.long_term import LongTermMemoryManager
from kaiwen_agent.memory.models import MemoryItem, MemoryOperationContext, MemoryStatus


class MemoryAdminOverview(BaseModel):
    """Framework-neutral contract for a product-owned memory administration UI."""

    model_config = ConfigDict(extra="forbid")

    total: int = Field(ge=0)
    pending_review: int = Field(ge=0)
    by_status: dict[str, int]
    by_namespace: dict[str, int]
    recent: list[MemoryItem]


class MemoryAdminService:
    def __init__(self, *, manager: LongTermMemoryManager) -> None:
        self.manager = manager

    async def overview(
        self,
        context: MemoryOperationContext,
        *,
        namespace: str,
        limit: int = 100,
    ) -> MemoryAdminOverview:
        items = await self.manager.list_memories(
            context,
            namespace=namespace,
            limit=limit,
        )
        return MemoryAdminOverview(
            total=len(items),
            pending_review=sum(item.status == MemoryStatus.PENDING_REVIEW for item in items),
            by_status=dict(Counter(item.status.value for item in items)),
            by_namespace=dict(Counter(item.namespace for item in items)),
            recent=items[:20],
        )

    async def review_inbox(
        self,
        context: MemoryOperationContext,
        *,
        namespace: str,
        limit: int = 100,
    ) -> list[MemoryItem]:
        return await self.manager.list_memories(
            context,
            namespace=namespace,
            status=MemoryStatus.PENDING_REVIEW,
            limit=limit,
        )

    async def approve(self, context: MemoryOperationContext, *, memory_id: str) -> MemoryItem:
        return await self.manager.approve_review(context, memory_id=memory_id)

    async def edit(
        self,
        context: MemoryOperationContext,
        *,
        memory_id: str,
        content: str,
    ) -> MemoryItem:
        return await self.manager.edit(context, memory_id=memory_id, content=content)

    async def forget(self, context: MemoryOperationContext, *, memory_id: str) -> bool:
        return await self.manager.forget(context, memory_id=memory_id)

    async def reject(self, context: MemoryOperationContext, *, memory_id: str) -> bool:
        return await self.manager.forget(context, memory_id=memory_id)

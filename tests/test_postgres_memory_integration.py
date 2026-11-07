from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest

from kaiwen_agent.memory import (
    LongTermMemoryManager,
    MemoryAuthorization,
    MemoryCandidate,
    MemoryOperationContext,
    MemoryType,
)
from kaiwen_agent.persistence import PostgreSQLMemoryStore


class AllowMemory:
    async def authorize(
        self, context: MemoryOperationContext, *, action: str, namespace: str
    ) -> MemoryAuthorization:
        del context, action, namespace
        return MemoryAuthorization(allowed=True, reason="integration test")


class TwoDimensionEmbeddings:
    async def embed(self, text: str) -> list[float]:
        return [float("preview" in text.casefold()), 1.0]


@pytest.mark.skipif(
    not os.getenv("KAIWEN_TEST_POSTGRES_DSN"),
    reason="KAIWEN_TEST_POSTGRES_DSN is not configured",
)
def test_postgres_pgvector_round_trip() -> None:
    async def scenario() -> None:
        suffix = uuid4().hex
        user_id = f"user-{suffix}"
        context = MemoryOperationContext(
            tenant_id=f"tenant-{suffix}",
            user_id=user_id,
            agent_id="integration-agent",
            session_id=f"session-{suffix}",
            trace_id=f"trace-{suffix}",
        )
        store = PostgreSQLMemoryStore(
            os.environ["KAIWEN_TEST_POSTGRES_DSN"], embedding_dimensions=2
        )
        await store.open()
        try:
            manager = LongTermMemoryManager(
                store=store,
                authorizer=AllowMemory(),
                embeddings=TwoDimensionEmbeddings(),
            )
            written = await manager.remember(
                context,
                MemoryCandidate(
                    namespace=f"user:{user_id}",
                    type=MemoryType.USER,
                    subject="deployment",
                    key="preview",
                    content="Use a preview before deployment.",
                    confidence=1,
                    importance=0.9,
                    future_utility=1,
                    stability=1,
                    explicitness=1,
                    scope_relevance=1,
                ),
            )
            assert written.item is not None
            recalled = await manager.recall(
                context,
                text="preview deployment",
                namespaces=frozenset({f"user:{user_id}"}),
            )
            assert recalled and recalled[0].item.id == written.item.id
            assert await manager.forget(context, memory_id=written.item.id) is True
        finally:
            await store.close()

    asyncio.run(scenario())

from __future__ import annotations

import asyncio

import pytest

from kaiwen_agent.context_engine import ContextBuildRequest, LongTermMemorySource
from kaiwen_agent.memory import (
    InMemoryLongTermMemoryStore,
    LongTermMemoryManager,
    MemoryAdminService,
    MemoryAdmissionDecision,
    MemoryAuthorization,
    MemoryCandidate,
    MemoryOperationContext,
    MemorySensitivity,
    MemoryStatus,
    MemoryType,
)


class AllowAuthorizer:
    async def authorize(
        self, context: MemoryOperationContext, *, action: str, namespace: str
    ) -> MemoryAuthorization:
        del context, action, namespace
        return MemoryAuthorization(allowed=True, reason="test policy")


class DenyAuthorizer:
    async def authorize(
        self, context: MemoryOperationContext, *, action: str, namespace: str
    ) -> MemoryAuthorization:
        del context, action, namespace
        return MemoryAuthorization(allowed=False, reason="denied by test policy")


class RecordingEmbeddings:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.inputs.append(text)
        return [float("preview" in text), 1.0]


def context(*, tenant_id: str = "tenant-1", user_id: str = "user-1") -> MemoryOperationContext:
    return MemoryOperationContext(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id="agent-1",
        session_id="session-1",
        trace_id="trace-1",
    )


def candidate(
    content: str = "User prefers a preview before production deploys.",
    **updates: object,
) -> MemoryCandidate:
    values: dict[str, object] = {
        "namespace": "user:user-1",
        "type": MemoryType.USER,
        "subject": "deployment",
        "key": "preview_preference",
        "content": content,
        "confidence": 0.95,
        "importance": 0.8,
        "future_utility": 0.95,
        "stability": 0.9,
        "explicitness": 1.0,
        "scope_relevance": 1.0,
    }
    values.update(updates)
    return MemoryCandidate.model_validate(values)


def test_secret_is_discarded_before_policy_or_embedding() -> None:
    async def scenario() -> None:
        embeddings = RecordingEmbeddings()
        manager = LongTermMemoryManager(
            store=InMemoryLongTermMemoryStore(),
            authorizer=DenyAuthorizer(),
            embeddings=embeddings,
        )
        result = await manager.remember(
            context(),
            candidate("api_key=sk-proj-abcdefghijklmnopqrstuvwxyz"),
        )
        assert result.admission.decision == MemoryAdmissionDecision.DISCARD
        assert result.admission.reasons == ["secret_or_credential_detected"]
        assert result.item is None
        assert embeddings.inputs == []

    asyncio.run(scenario())


def test_structured_secret_key_is_discarded_before_embedding() -> None:
    async def scenario() -> None:
        embeddings = RecordingEmbeddings()
        manager = LongTermMemoryManager(
            store=InMemoryLongTermMemoryStore(),
            authorizer=AllowAuthorizer(),
            embeddings=embeddings,
        )
        result = await manager.remember(
            context(), candidate(structured_data={"nested": {"access_token": "value"}})
        )
        assert result.item is None
        assert result.admission.reasons == ["secret_or_credential_detected"]
        assert embeddings.inputs == []

    asyncio.run(scenario())


def test_policy_denial_prevents_storage() -> None:
    async def scenario() -> None:
        store = InMemoryLongTermMemoryStore()
        manager = LongTermMemoryManager(store=store, authorizer=DenyAuthorizer())
        result = await manager.remember(context(), candidate())
        assert result.admission.decision == MemoryAdmissionDecision.DISCARD
        assert await store.list_memories(tenant_id="tenant-1", user_id="user-1") == []

    asyncio.run(scenario())


def test_owner_scope_blocks_cross_user_namespace() -> None:
    async def scenario() -> None:
        manager = LongTermMemoryManager(
            store=InMemoryLongTermMemoryStore(), authorizer=AllowAuthorizer()
        )
        with pytest.raises(PermissionError):
            await manager.remember(
                context(user_id="other-user"),
                candidate(),
            )

    asyncio.run(scenario())


def test_duplicate_and_conflict_supersession() -> None:
    async def scenario() -> None:
        store = InMemoryLongTermMemoryStore()
        manager = LongTermMemoryManager(store=store, authorizer=AllowAuthorizer())
        first = await manager.remember(context(), candidate())
        duplicate = await manager.remember(context(), candidate())
        replacement = await manager.remember(
            context(), candidate("User no longer wants a preview before deploys.")
        )
        assert first.item is not None
        assert duplicate.duplicate is True
        assert duplicate.item is not None and duplicate.item.id == first.item.id
        assert replacement.item is not None
        assert replacement.item.supersedes_id == first.item.id
        old = await store.get_memory(
            tenant_id="tenant-1", user_id="user-1", memory_id=first.item.id
        )
        assert old is not None and old.status == MemoryStatus.SUPERSEDED

    asyncio.run(scenario())


def test_concurrent_duplicate_writes_keep_one_active_memory() -> None:
    async def scenario() -> None:
        store = InMemoryLongTermMemoryStore()
        manager = LongTermMemoryManager(store=store, authorizer=AllowAuthorizer())
        results = await asyncio.gather(
            manager.remember(context(), candidate()),
            manager.remember(context(), candidate()),
        )
        active = await store.list_memories(
            tenant_id="tenant-1", user_id="user-1", status=MemoryStatus.ACTIVE
        )
        assert len(active) == 1
        assert {result.item.id for result in results if result.item is not None} == {active[0].id}

    asyncio.run(scenario())


def test_sensitive_memory_waits_for_review_before_embedding_and_supersession() -> None:
    async def scenario() -> None:
        store = InMemoryLongTermMemoryStore()
        embeddings = RecordingEmbeddings()
        manager = LongTermMemoryManager(
            store=store, authorizer=AllowAuthorizer(), embeddings=embeddings
        )
        first = await manager.remember(context(), candidate())
        assert first.item is not None
        review = await manager.remember(
            context(),
            candidate(
                "Sensitive replacement preference.",
                sensitivity=MemorySensitivity.SENSITIVE,
            ),
        )
        assert review.item is not None
        assert review.item.status == MemoryStatus.PENDING_REVIEW
        assert embeddings.inputs == [first.item.content]
        edited = await manager.edit(
            context(), memory_id=review.item.id, content="Edited sensitive replacement."
        )
        assert edited.embedding is None
        assert embeddings.inputs == [first.item.content]
        old_before = await store.get_memory(
            tenant_id="tenant-1", user_id="user-1", memory_id=first.item.id
        )
        assert old_before is not None and old_before.status == MemoryStatus.ACTIVE
        approved = await manager.approve_review(context(), memory_id=review.item.id)
        assert approved.status == MemoryStatus.ACTIVE
        assert embeddings.inputs[-1] == approved.content
        old_after = await store.get_memory(
            tenant_id="tenant-1", user_id="user-1", memory_id=first.item.id
        )
        assert old_after is not None and old_after.status == MemoryStatus.SUPERSEDED

    asyncio.run(scenario())


def test_recall_is_bounded_and_owner_scoped() -> None:
    async def scenario() -> None:
        store = InMemoryLongTermMemoryStore()
        embeddings = RecordingEmbeddings()
        manager = LongTermMemoryManager(
            store=store, authorizer=AllowAuthorizer(), embeddings=embeddings
        )
        await manager.remember(context(), candidate())
        await manager.remember(
            context(tenant_id="tenant-2"),
            candidate(namespace="user:user-1", key="other", subject="other"),
        )
        results = await manager.recall(
            context(),
            text="preview deployment",
            namespaces=frozenset({"user:user-1"}),
            top_k=1,
        )
        assert len(results) == 1
        assert results[0].item.tenant_id == "tenant-1"

    asyncio.run(scenario())


def test_forget_redacts_payload_and_cross_owner_lookup_is_not_found() -> None:
    async def scenario() -> None:
        store = InMemoryLongTermMemoryStore()
        manager = LongTermMemoryManager(store=store, authorizer=AllowAuthorizer())
        written = await manager.remember(context(), candidate())
        assert written.item is not None
        with pytest.raises(KeyError):
            await manager.forget(context(user_id="user-2"), memory_id=written.item.id)
        assert await manager.forget(context(), memory_id=written.item.id) is True
        forgotten = await store.get_memory(
            tenant_id="tenant-1", user_id="user-1", memory_id=written.item.id
        )
        assert forgotten is not None
        assert forgotten.status == MemoryStatus.DELETED
        assert forgotten.content == "[forgotten]"

    asyncio.run(scenario())


def test_admin_contract_and_context_source_use_manager_authorization() -> None:
    async def scenario() -> None:
        manager = LongTermMemoryManager(
            store=InMemoryLongTermMemoryStore(), authorizer=AllowAuthorizer()
        )
        written = await manager.remember(context(), candidate())
        assert written.item is not None
        admin = MemoryAdminService(manager=manager)
        overview = await admin.overview(context(), namespace="user:user-1")
        assert overview.total == 1

        source = LongTermMemorySource(manager, top_k=4)
        items = await source.collect(
            ContextBuildRequest(
                agent_id="agent-1",
                tenant_id="tenant-1",
                user_id="user-1",
                session_id="session-1",
                query="preview deploy",
                capability="answer",
            )
        )
        assert [item.metadata["memory_id"] for item in items] == [written.item.id]

    asyncio.run(scenario())

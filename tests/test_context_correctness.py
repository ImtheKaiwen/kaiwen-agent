"""Regression tests for M6.1: latest-window selection, budget priority, compaction cursors."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from kaiwen_agent.context_engine import (
    CompactionDraft,
    CompactionPolicy,
    CompactionRequest,
    CompactionStatus,
    ContextBudget,
    ContextBuilder,
    ContextBuildRequest,
    ContextCompactionManager,
    ContextKind,
    ContextRenderer,
    StaticContextSource,
)
from kaiwen_agent.context_engine.sources import RecentConversationSource
from kaiwen_agent.memory import InMemorySessionMemoryStore, SessionMemoryManager
from kaiwen_agent.persistence import SQLitePersistence
from kaiwen_agent.realtime import (
    ConversationMessage,
    ConversationModality,
    ConversationRole,
    ConversationStore,
    InMemoryConversationStore,
)

TENANT = "tenant_1"
USER = "user_1"
CONVERSATION = "conversation_1"


def message(
    index: int,
    *,
    text: str | None = None,
    tenant: str = TENANT,
    user: str = USER,
    conversation: str = CONVERSATION,
    prefix: str = "m",
) -> ConversationMessage:
    return ConversationMessage(
        id=f"{prefix}{index:05d}",
        tenant_id=tenant,
        user_id=user,
        conversation_id=conversation,
        role=ConversationRole.USER if index % 2 == 0 else ConversationRole.ASSISTANT,
        modality=ConversationModality.TEXT,
        text=text if text is not None else f"message number {index}",
    )


def build_request() -> ContextBuildRequest:
    return ContextBuildRequest(
        agent_id="agent",
        tenant_id=TENANT,
        user_id=USER,
        session_id="session_1",
        conversation_id=CONVERSATION,
        query="current request",
        capability="chat",
    )


class LegacyOnlyStore:
    """A custom store implementing only the original ConversationStore protocol."""

    def __init__(self, inner: InMemoryConversationStore) -> None:
        self.inner = inner

    async def append_message(self, item: ConversationMessage) -> None:
        await self.inner.append_message(item)

    async def list_messages(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        after_id: str | None = None,
        limit: int = 100,
    ) -> list[ConversationMessage]:
        return await self.inner.list_messages(
            tenant_id=tenant_id, conversation_id=conversation_id, after_id=after_id, limit=limit
        )


StoreFactory = Callable[[Path], ConversationStore]
STORES: dict[str, StoreFactory] = {
    "memory": lambda tmp: InMemoryConversationStore(),
    "sqlite": lambda tmp: SQLitePersistence(tmp / "conversation.sqlite3"),
    "legacy": lambda tmp: LegacyOnlyStore(InMemoryConversationStore()),
}


def run_with_store(
    kind: str, tmp_path: Path, scenario: Callable[[ConversationStore], Awaitable[None]]
) -> None:
    asyncio.run(scenario(STORES[kind](tmp_path)))


@pytest.mark.parametrize("kind", ["memory", "sqlite", "legacy"])
def test_recent_source_selects_actual_latest_messages_beyond_1000(
    kind: str, tmp_path: Path
) -> None:
    async def scenario(store: ConversationStore) -> None:
        for index in range(1_250):
            await store.append_message(message(index))
        items = await RecentConversationSource(store, message_limit=5).collect(build_request())
        assert [item.metadata["message_id"] for item in items] == [
            f"m{index:05d}" for index in range(1_245, 1_250)
        ]

    run_with_store(kind, tmp_path, scenario)


@pytest.mark.parametrize("kind", ["memory", "sqlite", "legacy"])
def test_recent_source_isolates_owner_conversation_and_tenant(kind: str, tmp_path: Path) -> None:
    async def scenario(store: ConversationStore) -> None:
        # Foreign messages must neither leak nor crowd out the owner's messages.
        for index in range(1_100):
            await store.append_message(message(index, user="user_2", prefix="other_user_"))
        for index in range(30):
            await store.append_message(message(index))
        for index in range(20):
            await store.append_message(
                message(index, conversation="conversation_2", prefix="other_conv_")
            )
            await store.append_message(message(index, tenant="tenant_2", prefix="other_tenant_"))
        items = await RecentConversationSource(store, message_limit=4).collect(build_request())
        assert [item.metadata["message_id"] for item in items] == [
            f"m{index:05d}" for index in range(26, 30)
        ]

    run_with_store(kind, tmp_path, scenario)


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_list_latest_messages_contract(kind: str, tmp_path: Path) -> None:
    async def scenario(store: ConversationStore) -> None:
        for index in range(12):
            await store.append_message(message(index))
            await store.append_message(message(index, user="user_2", prefix="x"))
        latest = await store.list_latest_messages(  # type: ignore[attr-defined]
            tenant_id=TENANT, conversation_id=CONVERSATION, user_id=USER, limit=3
        )
        assert [item.id for item in latest] == ["m00009", "m00010", "m00011"]
        anyone = await store.list_latest_messages(  # type: ignore[attr-defined]
            tenant_id=TENANT, conversation_id=CONVERSATION, limit=2
        )
        assert [item.id for item in anyone] == ["m00011", "x00011"]
        with pytest.raises(ValueError):
            await store.list_latest_messages(  # type: ignore[attr-defined]
                tenant_id=TENANT, conversation_id=CONVERSATION, limit=0
            )
        with pytest.raises(ValueError):
            await store.list_latest_messages(  # type: ignore[attr-defined]
                tenant_id=TENANT, conversation_id=CONVERSATION, limit=1_001
            )

    run_with_store(kind, tmp_path, scenario)


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_list_messages_after_id_contract_is_unchanged(kind: str, tmp_path: Path) -> None:
    async def scenario(store: ConversationStore) -> None:
        for index in range(1_100):
            await store.append_message(message(index))
        first = await store.list_messages(tenant_id=TENANT, conversation_id=CONVERSATION, limit=3)
        assert [item.id for item in first] == ["m00000", "m00001", "m00002"]
        page = await store.list_messages(
            tenant_id=TENANT, conversation_id=CONVERSATION, after_id="m00998", limit=1_000
        )
        assert [item.id for item in page] == [f"m{index:05d}" for index in range(999, 1_100)]
        missing = await store.list_messages(
            tenant_id=TENANT, conversation_id=CONVERSATION, after_id="nope"
        )
        assert missing == []

    run_with_store(kind, tmp_path, scenario)


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_budget_pressure_keeps_newest_turns_in_chronological_order(
    kind: str, tmp_path: Path
) -> None:
    async def scenario(store: ConversationStore) -> None:
        for index in range(20):
            await store.append_message(message(index, text=f"turn{index:02d} " + "word " * 1_500))
        builder = ContextBuilder(
            [
                StaticContextSource(name="system", content="SYSTEM RULES", kind=ContextKind.SYSTEM),
                RecentConversationSource(store, message_limit=20),
            ]
        )
        budget = ContextBudget(max_total_tokens=30_000, recent_conversation=6_000)
        built = await builder.build(build_request(), budget=budget)
        recent = [
            item.metadata["message_id"]
            for item in built.items
            if item.kind == ContextKind.RECENT_CONVERSATION
        ]
        assert recent and recent[-1] == "m00019"
        assert recent == sorted(recent), "conversation must stay chronological"
        assert recent == [f"m{index:05d}" for index in range(20 - len(recent), 20)]
        assert any(item.kind == ContextKind.SYSTEM for item in built.items)
        assert any(item.kind == ContextKind.CURRENT_INPUT for item in built.items)
        rendered = ContextRenderer().render(built)
        assert rendered.index("turn19") > rendered.index(f"turn{20 - len(recent):02d}")
        # Deterministic: same input yields the same selection.
        again = await builder.build(build_request(), budget=budget)
        assert [item.content for item in again.items] == [item.content for item in built.items]

    run_with_store(kind, tmp_path, scenario)


def test_required_context_and_current_input_survive_pressure() -> None:
    async def scenario() -> None:
        store = InMemoryConversationStore()
        for index in range(10):
            await store.append_message(message(index, text="chat " * 400))
        builder = ContextBuilder(
            [
                StaticContextSource(
                    name="policy", content="POLICY " * 50, kind=ContextKind.POLICY, required=True
                ),
                RecentConversationSource(store, message_limit=10),
            ]
        )
        budget = ContextBudget(max_total_tokens=1_200, recent_conversation=1_000)
        built = await builder.build(build_request(), budget=budget)
        kinds = [item.kind for item in built.items]
        assert ContextKind.POLICY in kinds
        assert ContextKind.CURRENT_INPUT in kinds
        assert built.estimated_tokens <= 1_200
        recent = [
            item.metadata["message_id"]
            for item in built.items
            if item.kind == ContextKind.RECENT_CONVERSATION
        ]
        assert recent == sorted(recent)
        assert not recent or recent[-1] == "m00009"

    asyncio.run(scenario())


class SequentialSummaryGenerator:
    """Deterministic generator that preserves prior goal, decisions and constraints."""

    def __init__(self) -> None:
        self.calls: list[CompactionRequest] = []

    async def generate(self, request: CompactionRequest) -> CompactionDraft:
        self.calls.append(request)
        previous = request.previous_summary
        return CompactionDraft(
            summary=f"summarized through {request.messages[-1].id}",
            goal=previous.goal if previous else "goal",
            decisions=tuple(previous.decisions) if previous else ("decision",),
            known_constraints=tuple(previous.known_constraints) if previous else ("constraint",),
            source_message_ids=tuple(item.id for item in request.messages),
        )


def compaction_manager(
    conversations: ConversationStore,
    memory: SessionMemoryManager,
    generator: SequentialSummaryGenerator,
    *,
    message_limit: int = 1_000,
) -> ContextCompactionManager:
    return ContextCompactionManager(
        conversations=conversations,
        memory=memory,
        generator=generator,
        policy=CompactionPolicy(
            max_context_tokens=100,
            soft_compaction_ratio=0.1,
            recent_messages=5,
            message_limit=message_limit,
        ),
    )


async def compact(manager: ContextCompactionManager) -> Any:
    return await manager.compact_if_needed(
        tenant_id=TENANT,
        user_id=USER,
        conversation_id=CONVERSATION,
        current_input="next",
    )


@pytest.mark.parametrize("kind", ["memory", "sqlite", "legacy"])
def test_compaction_advances_past_1000_messages_without_gaps(kind: str, tmp_path: Path) -> None:
    async def scenario(store: ConversationStore) -> None:
        total = 1_300
        for index in range(total):
            await store.append_message(message(index, text=f"content {index} " + "x" * 40))
        memory = SessionMemoryManager(InMemorySessionMemoryStore())
        generator = SequentialSummaryGenerator()
        manager = compaction_manager(store, memory, generator)
        statuses = []
        for _ in range(6):
            result = await compact(manager)
            statuses.append(result.status)
            if result.status != CompactionStatus.COMPACTED:
                break
        assert statuses[:2] == [CompactionStatus.COMPACTED, CompactionStatus.COMPACTED]
        summary = await memory.get_session_summary(
            tenant_id=TENANT, user_id=USER, conversation_id=CONVERSATION
        )
        assert summary is not None
        expected = [f"m{index:05d}" for index in range(total - 5)]
        assert summary.source_message_ids == expected
        assert summary.source_message_id == f"m{total - 6:05d}"
        # Every generator batch continues exactly where the previous one stopped.
        batches = [[item.id for item in call.messages] for call in generator.calls]
        assert [identifier for batch in batches for identifier in batch] == expected
        assert all(len(batch) <= 1_000 for batch in batches)

    run_with_store(kind, tmp_path, scenario)


def test_compaction_cursor_survives_restart_with_sqlite(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "state.sqlite3"
        first = SQLitePersistence(path)
        for index in range(1_100):
            await first.append_message(message(index, text=f"content {index} " + "x" * 40))
        memory = SessionMemoryManager(first)
        generator = SequentialSummaryGenerator()
        result = await compact(compaction_manager(first, memory, generator))
        assert result.status == CompactionStatus.COMPACTED
        for index in range(1_100, 1_150):
            await first.append_message(message(index, text=f"content {index} " + "x" * 40))

        restarted = SQLitePersistence(path)
        restarted_generator = SequentialSummaryGenerator()
        restarted_memory = SessionMemoryManager(restarted)
        again = await compact(compaction_manager(restarted, restarted_memory, restarted_generator))
        assert again.status == CompactionStatus.COMPACTED
        first_ids = [item.id for item in generator.calls[0].messages]
        second_ids = [item.id for item in restarted_generator.calls[0].messages]
        assert first_ids + second_ids == [f"m{index:05d}" for index in range(1_145)]

    asyncio.run(scenario())


def test_compaction_rejects_missing_cursor_and_ignores_foreign_messages() -> None:
    async def scenario() -> None:
        store = InMemoryConversationStore()
        for index in range(30):
            await store.append_message(message(index, text="content " * 20))
            await store.append_message(message(index, user="user_2", prefix="x", text="secret"))
        memory = SessionMemoryManager(InMemorySessionMemoryStore())
        generator = SequentialSummaryGenerator()
        manager = compaction_manager(store, memory, generator)
        result = await compact(manager)
        assert result.status == CompactionStatus.COMPACTED
        assert all(item.user_id == USER for call in generator.calls for item in call.messages)
        summary = await memory.get_session_summary(
            tenant_id=TENANT, user_id=USER, conversation_id=CONVERSATION
        )
        assert summary is not None
        # A cursor that no longer exists in the canonical store fails closed.
        other_store = InMemoryConversationStore()
        for index in range(30):
            await other_store.append_message(message(index, text="content " * 20, prefix="z"))
        rejected = await compact(compaction_manager(other_store, memory, generator))
        assert rejected.status == CompactionStatus.REJECTED
        assert rejected.reason == "source_cursor_missing"

    asyncio.run(scenario())

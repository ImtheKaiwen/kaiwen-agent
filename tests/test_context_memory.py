import asyncio
from datetime import datetime, timedelta

import pytest

from kaiwen_agent.context_engine import (
    ContextBudget,
    ContextBuilder,
    ContextBuildRequest,
    ContextKind,
    ContextSourceError,
    RecentConversationSource,
    SessionSummarySource,
    StaticContextSource,
)
from kaiwen_agent.memory import (
    InMemorySessionMemoryStore,
    SessionMemoryManager,
    SessionSummary,
)
from kaiwen_agent.persistence import SQLitePersistence
from kaiwen_agent.realtime import (
    ConversationMessage,
    ConversationModality,
    ConversationRole,
    InMemoryConversationStore,
)
from kaiwen_agent.types import utc_now


def request() -> ContextBuildRequest:
    return ContextBuildRequest(
        agent_id="admin-agent",
        tenant_id="tenant_1",
        user_id="user_1",
        session_id="session_1",
        conversation_id="conversation_1",
        query="current request",
        capability="conversation",
    )


def test_context_budget_is_bounded_and_preserves_required_input() -> None:
    async def scenario() -> None:
        builder = ContextBuilder([StaticContextSource(name="system", content="s" * 200)])
        built = await builder.build(
            request(),
            budget=ContextBudget(
                max_total_tokens=20,
                system=10,
                current_input=10,
                recent_conversation=0,
                session_summary=0,
                memory=0,
                knowledge=0,
                task=0,
                job=0,
                objective=0,
                system_state=0,
                policy=0,
            ),
        )
        assert built.estimated_tokens <= 20
        assert built.truncated is True
        assert {item.source for item in built.items} == {"current_input", "system"}
        assert built.items[0].source == "system"

    asyncio.run(scenario())


def test_optional_source_failure_is_omitted_and_required_failure_stops() -> None:
    class BrokenSource:
        name = "broken"

        def __init__(self, required: bool) -> None:
            self.required = required

        async def collect(self, context):
            del context
            raise RuntimeError("unavailable")

    async def scenario() -> None:
        optional = await ContextBuilder([BrokenSource(False)]).build(request())
        assert optional.omitted_sources == ["broken"]
        with pytest.raises(ContextSourceError):
            await ContextBuilder([BrokenSource(True)]).build(request())

    asyncio.run(scenario())


def test_recent_conversation_source_enforces_user_scope() -> None:
    async def scenario() -> None:
        store = InMemoryConversationStore()
        for user_id, text in [
            ("user_1", "allowed"),
            ("user_2", "must not leak"),
        ]:
            await store.append_message(
                ConversationMessage(
                    tenant_id="tenant_1",
                    user_id=user_id,
                    conversation_id="conversation_1",
                    role=ConversationRole.USER,
                    modality=ConversationModality.TEXT,
                    text=text,
                )
            )
        items = await RecentConversationSource(store).collect(request())
        assert [item.content for item in items] == ["user: allowed"]

    asyncio.run(scenario())


def test_session_summary_retention_forget_and_context_source() -> None:
    async def scenario() -> None:
        store = InMemorySessionMemoryStore()
        manager = SessionMemoryManager(store, max_summary_tokens=20, retention=timedelta(days=1))
        saved = await manager.save_session_summary(
            SessionSummary(
                tenant_id="tenant_1",
                user_id="user_1",
                conversation_id="conversation_1",
                goal="Build the runtime",
                summary="x" * 200,
                decisions=["Keep policy server-side"],
            )
        )
        assert len(saved.render()) <= 80
        items = await SessionSummarySource(manager).collect(request())
        assert len(items) == 1
        assert items[0].kind == ContextKind.SESSION_SUMMARY

        await manager.forget_session(
            tenant_id="tenant_1",
            user_id="user_1",
            conversation_id="conversation_1",
        )
        assert (
            await manager.get_session_summary(
                tenant_id="tenant_1",
                user_id="user_1",
                conversation_id="conversation_1",
            )
            is None
        )

        await store.save_session_summary(
            saved.model_copy(update={"expires_at": utc_now() - timedelta(seconds=1)})
        )
        assert (
            await manager.get_session_summary(
                tenant_id="tenant_1",
                user_id="user_1",
                conversation_id="conversation_1",
            )
            is None
        )

    asyncio.run(scenario())


def test_sqlite_session_summary_is_durable_and_identity_scoped(tmp_path) -> None:
    database = tmp_path / "memory.sqlite3"

    async def scenario() -> None:
        manager = SessionMemoryManager(SQLitePersistence(database))
        summary = await manager.save_session_summary(
            SessionSummary(
                tenant_id="tenant_1",
                user_id="user_1",
                conversation_id="conversation_1",
                summary="Durable summary",
            )
        )
        restarted = SessionMemoryManager(SQLitePersistence(database))
        loaded = await restarted.get_session_summary(
            tenant_id="tenant_1",
            user_id="user_1",
            conversation_id="conversation_1",
        )
        assert loaded is not None and loaded.id == summary.id
        assert (
            await restarted.get_session_summary(
                tenant_id="tenant_1",
                user_id="user_2",
                conversation_id="conversation_1",
            )
            is None
        )

    asyncio.run(scenario())


def test_session_summary_rejects_naive_expiry() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        SessionSummary(
            tenant_id="tenant_1",
            user_id="user_1",
            conversation_id="conversation_1",
            summary="Invalid expiry",
            expires_at=datetime.now(),
        )

from __future__ import annotations

from datetime import timedelta
from typing import Protocol

from kaiwen_agent.memory.models import SessionSummary
from kaiwen_agent.memory.store import SessionMemoryStore
from kaiwen_agent.tokens import estimate_tokens, truncate_to_tokens
from kaiwen_agent.types import utc_now


class MemoryManager(Protocol):
    async def get_session_summary(
        self, *, tenant_id: str, user_id: str, conversation_id: str
    ) -> SessionSummary | None: ...

    async def save_session_summary(self, summary: SessionSummary) -> SessionSummary: ...

    async def forget_session(
        self, *, tenant_id: str, user_id: str, conversation_id: str
    ) -> None: ...


class SessionMemoryManager:
    def __init__(
        self,
        store: SessionMemoryStore,
        *,
        max_summary_tokens: int = 1500,
        retention: timedelta = timedelta(days=30),
    ) -> None:
        if max_summary_tokens < 1:
            raise ValueError("max_summary_tokens must be at least 1")
        if retention <= timedelta(0):
            raise ValueError("retention must be positive")
        self.store = store
        self.max_summary_tokens = max_summary_tokens
        self.retention = retention

    async def get_session_summary(
        self, *, tenant_id: str, user_id: str, conversation_id: str
    ) -> SessionSummary | None:
        summary = await self.store.get_session_summary(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        if summary is None:
            return None
        if summary.expires_at is not None and summary.expires_at <= utc_now():
            await self.forget_session(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            return None
        return summary

    async def save_session_summary(self, summary: SessionSummary) -> SessionSummary:
        rendered = summary.render()
        if estimate_tokens(rendered) > self.max_summary_tokens:
            structured = summary.model_copy(update={"summary": ""})
            structured_tokens = estimate_tokens(structured.render())
            if structured_tokens > self.max_summary_tokens:
                raise ValueError("Structured session summary exceeds the configured token limit")
            available = self.max_summary_tokens - structured_tokens
            if summary.summary and available > 0:
                available = max(0, available - estimate_tokens("Summary: "))
            summary = summary.model_copy(
                update={"summary": truncate_to_tokens(summary.summary, available)}
            )
            while summary.summary and estimate_tokens(summary.render()) > self.max_summary_tokens:
                reduced = max(0, estimate_tokens(summary.summary) - 1)
                summary = summary.model_copy(
                    update={"summary": truncate_to_tokens(summary.summary, reduced)}
                )
            if estimate_tokens(summary.render()) > self.max_summary_tokens:
                raise ValueError("Session summary exceeds the configured token limit")
        now = utc_now()
        stored = summary.model_copy(
            update={
                "updated_at": now,
                "expires_at": summary.expires_at or now + self.retention,
            }
        )
        await self.store.save_session_summary(stored)
        return stored.model_copy(deep=True)

    async def forget_session(self, *, tenant_id: str, user_id: str, conversation_id: str) -> None:
        await self.store.delete_session_summary(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )

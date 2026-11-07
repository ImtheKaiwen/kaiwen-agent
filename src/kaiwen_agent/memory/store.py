from __future__ import annotations

import asyncio
from typing import Protocol

from kaiwen_agent.memory.models import SessionSummary


class SessionMemoryStore(Protocol):
    async def save_session_summary(self, summary: SessionSummary) -> None: ...

    async def get_session_summary(
        self, *, tenant_id: str, user_id: str, conversation_id: str
    ) -> SessionSummary | None: ...

    async def delete_session_summary(
        self, *, tenant_id: str, user_id: str, conversation_id: str
    ) -> None: ...


class InMemorySessionMemoryStore:
    def __init__(self) -> None:
        self._summaries: dict[tuple[str, str, str], SessionSummary] = {}
        self._lock = asyncio.Lock()

    async def save_session_summary(self, summary: SessionSummary) -> None:
        key = (summary.tenant_id, summary.user_id, summary.conversation_id)
        async with self._lock:
            self._summaries[key] = summary.model_copy(deep=True)

    async def get_session_summary(
        self, *, tenant_id: str, user_id: str, conversation_id: str
    ) -> SessionSummary | None:
        async with self._lock:
            summary = self._summaries.get((tenant_id, user_id, conversation_id))
            return summary.model_copy(deep=True) if summary else None

    async def delete_session_summary(
        self, *, tenant_id: str, user_id: str, conversation_id: str
    ) -> None:
        async with self._lock:
            self._summaries.pop((tenant_id, user_id, conversation_id), None)

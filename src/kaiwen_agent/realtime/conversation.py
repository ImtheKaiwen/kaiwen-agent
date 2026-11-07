from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.types import utc_now


class ConversationRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    DEVELOPER = "developer"
    TOOL = "tool"


class ConversationModality(StrEnum):
    TEXT = "text"
    AUDIO = "audio"


class ConversationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"msg_{uuid4().hex}")
    conversation_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    realtime_session_id: str | None = None
    role: ConversationRole
    modality: ConversationModality
    text: str = Field(min_length=1)
    provider_event_ids: list[str] = Field(default_factory=list)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class ConversationStore(Protocol):
    async def append_message(self, message: ConversationMessage) -> None: ...

    async def list_messages(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        after_id: str | None = None,
        limit: int = 100,
    ) -> list[ConversationMessage]: ...


@runtime_checkable
class ConversationWindowStore(Protocol):
    """Optional capability: bounded newest-window reads and cursor existence checks.

    `list_messages` keeps its oldest-first, `after_id` incremental contract. Stores that
    also implement this protocol let context code read the newest messages and verify a
    cursor without scanning the conversation from its start.
    """

    async def list_latest_messages(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        user_id: str | None = None,
        limit: int = 100,
    ) -> list[ConversationMessage]:
        """Return the newest `limit` matching messages in chronological order."""
        ...

    async def message_exists(
        self, *, tenant_id: str, conversation_id: str, message_id: str
    ) -> bool: ...


async def latest_messages(
    store: ConversationStore,
    *,
    tenant_id: str,
    conversation_id: str,
    user_id: str | None = None,
    limit: int,
) -> list[ConversationMessage]:
    """Newest `limit` messages for one owner scope, oldest first.

    Uses the store's window capability when present. Otherwise it pages forward with the
    standard `after_id` contract, keeping only a bounded tail; that is correct for any
    store but reads the whole conversation.
    """

    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    if isinstance(store, ConversationWindowStore):
        return await store.list_latest_messages(
            tenant_id=tenant_id, conversation_id=conversation_id, user_id=user_id, limit=limit
        )
    tail: deque[ConversationMessage] = deque(maxlen=limit)
    after_id: str | None = None
    while True:
        page = await store.list_messages(
            tenant_id=tenant_id, conversation_id=conversation_id, after_id=after_id, limit=1000
        )
        if not page:
            return list(tail)
        tail.extend(item for item in page if user_id is None or item.user_id == user_id)
        after_id = page[-1].id


class InMemoryConversationStore:
    def __init__(self) -> None:
        self._messages: list[ConversationMessage] = []
        self._lock = asyncio.Lock()

    async def append_message(self, message: ConversationMessage) -> None:
        async with self._lock:
            if any(item.id == message.id for item in self._messages):
                return
            self._messages.append(message.model_copy(deep=True))

    async def list_messages(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        after_id: str | None = None,
        limit: int = 100,
    ) -> list[ConversationMessage]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        async with self._lock:
            messages = [
                item
                for item in self._messages
                if item.tenant_id == tenant_id and item.conversation_id == conversation_id
            ]
            if after_id is not None:
                index = next(
                    (index for index, item in enumerate(messages) if item.id == after_id),
                    len(messages),
                )
                messages = messages[index + 1 :]
            return [item.model_copy(deep=True) for item in messages[:limit]]

    async def list_latest_messages(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        user_id: str | None = None,
        limit: int = 100,
    ) -> list[ConversationMessage]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        async with self._lock:
            selected: list[ConversationMessage] = []
            for item in reversed(self._messages):
                if (
                    item.tenant_id == tenant_id
                    and item.conversation_id == conversation_id
                    and (user_id is None or item.user_id == user_id)
                ):
                    selected.append(item.model_copy(deep=True))
                    if len(selected) == limit:
                        break
            selected.reverse()
            return selected

    async def message_exists(
        self, *, tenant_id: str, conversation_id: str, message_id: str
    ) -> bool:
        async with self._lock:
            return any(
                item.id == message_id
                and item.tenant_id == tenant_id
                and item.conversation_id == conversation_id
                for item in self._messages
            )

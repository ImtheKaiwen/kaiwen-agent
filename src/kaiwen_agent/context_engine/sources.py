from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from kaiwen_agent.context_engine.models import (
    ContextBuildRequest,
    ContextItem,
    ContextKind,
)
from kaiwen_agent.memory.long_term import LongTermMemoryManager
from kaiwen_agent.memory.manager import MemoryManager
from kaiwen_agent.memory.models import MemoryOperationContext
from kaiwen_agent.realtime.conversation import ConversationStore


class ContextSource(Protocol):
    name: str
    required: bool

    async def collect(self, request: ContextBuildRequest) -> list[ContextItem]: ...


class StaticContextSource:
    def __init__(
        self,
        *,
        name: str,
        content: str,
        kind: ContextKind = ContextKind.SYSTEM,
        priority: int = 1000,
        required: bool = True,
    ) -> None:
        self.name = name
        self.content = content
        self.kind = kind
        self.priority = priority
        self.required = required

    async def collect(self, request: ContextBuildRequest) -> list[ContextItem]:
        del request
        return [
            ContextItem(
                source=self.name,
                kind=self.kind,
                content=self.content,
                priority=self.priority,
                required=self.required,
            )
        ]


class RecentConversationSource:
    name = "recent_conversation"
    required = False

    def __init__(
        self,
        store: ConversationStore,
        *,
        message_limit: int = 20,
    ) -> None:
        if not 1 <= message_limit <= 1000:
            raise ValueError("message_limit must be between 1 and 1000")
        self.store = store
        self.message_limit = message_limit

    async def collect(self, request: ContextBuildRequest) -> list[ContextItem]:
        if request.tenant_id is None or request.user_id is None or request.conversation_id is None:
            return []
        messages = await self.store.list_messages(
            tenant_id=request.tenant_id,
            conversation_id=request.conversation_id,
            limit=1000,
        )
        scoped = [message for message in messages if message.user_id == request.user_id][
            -self.message_limit :
        ]
        return [
            ContextItem(
                source=self.name,
                kind=ContextKind.RECENT_CONVERSATION,
                content=f"{message.role.value}: {message.text}",
                priority=700,
                metadata={"message_id": message.id},
            )
            for message in scoped
        ]


class SessionSummarySource:
    name = "session_summary"
    required = False

    def __init__(self, memory: MemoryManager) -> None:
        self.memory = memory

    async def collect(self, request: ContextBuildRequest) -> list[ContextItem]:
        if request.tenant_id is None or request.user_id is None or request.conversation_id is None:
            return []
        summary = await self.memory.get_session_summary(
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            conversation_id=request.conversation_id,
        )
        if summary is None:
            return []
        return [
            ContextItem(
                source=self.name,
                kind=ContextKind.SESSION_SUMMARY,
                content=summary.render(),
                priority=800,
                metadata={"summary_id": summary.id},
            )
        ]


class LongTermMemorySource:
    name = "long_term_memory"
    required = False

    def __init__(
        self,
        memory: LongTermMemoryManager,
        *,
        namespace_resolver: Callable[[ContextBuildRequest], frozenset[str]] | None = None,
        top_k: int = 8,
    ) -> None:
        if not 1 <= top_k <= 50:
            raise ValueError("top_k must be between 1 and 50")
        self.memory = memory
        self.namespace_resolver = namespace_resolver or self._user_namespace
        self.top_k = top_k

    async def collect(self, request: ContextBuildRequest) -> list[ContextItem]:
        if request.tenant_id is None or request.user_id is None:
            return []
        results = await self.memory.recall(
            MemoryOperationContext(
                tenant_id=request.tenant_id,
                user_id=request.user_id,
                agent_id=request.agent_id,
                session_id=request.session_id,
                conversation_id=request.conversation_id,
                trace_id=request.trace_id,
                capabilities=request.capabilities,
            ),
            text=request.query,
            namespaces=self.namespace_resolver(request),
            top_k=self.top_k,
        )
        return [
            ContextItem(
                source=self.name,
                kind=ContextKind.MEMORY,
                content=result.item.content,
                priority=600,
                relevance=result.score,
                metadata={
                    "memory_id": result.item.id,
                    "namespace": result.item.namespace,
                    "memory_type": result.item.type.value,
                },
            )
            for result in results
        ]

    @staticmethod
    def _user_namespace(request: ContextBuildRequest) -> frozenset[str]:
        return frozenset({f"user:{request.user_id}"}) if request.user_id else frozenset()

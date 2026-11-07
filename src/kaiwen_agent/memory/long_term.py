from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from kaiwen_agent.memory.admission import MemoryAdmissionEngine
from kaiwen_agent.memory.long_term_store import LongTermMemoryStore
from kaiwen_agent.memory.models import (
    MemoryAdmissionDecision,
    MemoryAdmissionResult,
    MemoryCandidate,
    MemoryItem,
    MemoryOperationContext,
    MemorySearchQuery,
    MemorySearchResult,
    MemoryStatus,
    MemoryType,
)
from kaiwen_agent.policy import PolicyContext, PolicyDecisionType, PolicyEngine
from kaiwen_agent.types import utc_now


class EmbeddingProvider(Protocol):
    async def embed(self, text: str) -> list[float]: ...


class MemoryAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool
    review_required: bool = False
    reason: str


class MemoryAuthorizer(Protocol):
    async def authorize(
        self,
        context: MemoryOperationContext,
        *,
        action: str,
        namespace: str,
    ) -> MemoryAuthorization: ...


class PolicyMemoryAuthorizer:
    """Routes every long-term-memory operation through the central policy engine."""

    def __init__(self, policy_engine: PolicyEngine) -> None:
        self.policy_engine = policy_engine

    async def authorize(
        self,
        context: MemoryOperationContext,
        *,
        action: str,
        namespace: str,
    ) -> MemoryAuthorization:
        decision = await self.policy_engine.evaluate(
            PolicyContext(
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                agent_id=context.agent_id,
                session_id=context.session_id,
                conversation_id=context.conversation_id,
                run_id=context.run_id,
                trace_id=context.trace_id,
                environment=context.environment,
                action=action,
                resource=namespace,
                capabilities=context.capabilities,
            )
        )
        return MemoryAuthorization(
            allowed=decision.decision == PolicyDecisionType.ALLOW,
            review_required=decision.decision
            in {
                PolicyDecisionType.MODIFY,
                PolicyDecisionType.REVIEW,
                PolicyDecisionType.REQUIRE_APPROVAL,
            },
            reason=decision.reason,
        )


class MemoryWriteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    admission: MemoryAdmissionResult
    item: MemoryItem | None = None
    duplicate: bool = False


class LongTermMemoryManager:
    def __init__(
        self,
        *,
        store: LongTermMemoryStore,
        authorizer: MemoryAuthorizer,
        admission: MemoryAdmissionEngine | None = None,
        embeddings: EmbeddingProvider | None = None,
    ) -> None:
        self.store = store
        self.authorizer = authorizer
        self.admission = admission or MemoryAdmissionEngine()
        self.embeddings = embeddings

    async def remember(
        self, context: MemoryOperationContext, candidate: MemoryCandidate
    ) -> MemoryWriteResult:
        self._validate_namespace(context, candidate.namespace)
        admission = self.admission.evaluate(candidate)
        if admission.decision == MemoryAdmissionDecision.DISCARD:
            return MemoryWriteResult(admission=admission)

        authorization = await self.authorizer.authorize(
            context, action="memory.write", namespace=candidate.namespace
        )
        if not authorization.allowed and not authorization.review_required:
            denied = admission.model_copy(
                update={
                    "decision": MemoryAdmissionDecision.DISCARD,
                    "reasons": [*admission.reasons, f"policy_denied:{authorization.reason}"],
                }
            )
            return MemoryWriteResult(admission=denied)

        existing = await self.store.find_active_by_key(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            namespace=candidate.namespace,
            subject=candidate.subject,
            key=candidate.key,
        )
        if existing is not None and existing.content == candidate.content:
            return MemoryWriteResult(admission=admission, item=existing, duplicate=True)

        needs_review = (
            admission.decision == MemoryAdmissionDecision.REVIEW or authorization.review_required
        )
        embedding = None
        if not needs_review and self.embeddings is not None:
            embedding = await self.embeddings.embed(candidate.content)
        item = self._to_item(
            context,
            candidate,
            status=(MemoryStatus.PENDING_REVIEW if needs_review else MemoryStatus.ACTIVE),
            supersedes_id=existing.id if existing is not None else None,
            embedding=embedding,
        )
        stored = await self.store.save_memory(item)
        final_admission = admission.model_copy(
            update={
                "decision": (
                    MemoryAdmissionDecision.REVIEW
                    if needs_review
                    else MemoryAdmissionDecision.AUTO_SAVE
                )
            }
        )
        return MemoryWriteResult(admission=final_admission, item=stored)

    async def recall(
        self,
        context: MemoryOperationContext,
        *,
        text: str,
        namespaces: frozenset[str],
        top_k: int = 8,
    ) -> list[MemorySearchResult]:
        if not namespaces:
            raise ValueError("At least one memory namespace is required")
        for namespace in namespaces:
            self._validate_namespace(context, namespace)
            authorization = await self.authorizer.authorize(
                context, action="memory.read", namespace=namespace
            )
            if not authorization.allowed:
                raise PermissionError(f"Memory read denied: {authorization.reason}")
        probe = MemoryCandidate(
            namespace=next(iter(namespaces)),
            type=MemoryType.SEMANTIC,
            subject="query",
            key="query",
            content=text,
        )
        if self.admission.secret_detector.contains_secret(probe):
            raise ValueError("Secret-bearing queries cannot be sent to memory retrieval")
        embedding = await self.embeddings.embed(text) if self.embeddings is not None else None
        return await self.store.search_memories(
            MemorySearchQuery(
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                namespaces=namespaces,
                text=text,
                top_k=top_k,
            ),
            embedding=embedding,
        )

    async def list_memories(
        self,
        context: MemoryOperationContext,
        *,
        namespace: str,
        status: MemoryStatus | None = None,
        limit: int = 100,
    ) -> list[MemoryItem]:
        self._validate_namespace(context, namespace)
        authorization = await self.authorizer.authorize(
            context, action="memory.admin", namespace=namespace
        )
        if not authorization.allowed:
            raise PermissionError(f"Memory administration denied: {authorization.reason}")
        return await self.store.list_memories(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            status=status,
            namespace=namespace,
            limit=limit,
        )

    async def approve_review(
        self, context: MemoryOperationContext, *, memory_id: str
    ) -> MemoryItem:
        item = await self._owned(context, memory_id)
        if item.status != MemoryStatus.PENDING_REVIEW:
            raise ValueError("Only pending review memories can be approved")
        authorization = await self.authorizer.authorize(
            context, action="memory.review", namespace=item.namespace
        )
        if not authorization.allowed:
            raise PermissionError(f"Memory review denied: {authorization.reason}")
        embedding = (
            await self.embeddings.embed(item.content) if self.embeddings is not None else None
        )
        return await self.store.update_memory(
            item.model_copy(
                update={
                    "status": MemoryStatus.ACTIVE,
                    "embedding": embedding,
                    "updated_at": utc_now(),
                }
            )
        )

    async def edit(
        self, context: MemoryOperationContext, *, memory_id: str, content: str
    ) -> MemoryItem:
        item = await self._owned(context, memory_id)
        authorization = await self.authorizer.authorize(
            context, action="memory.edit", namespace=item.namespace
        )
        if not authorization.allowed:
            raise PermissionError(f"Memory edit denied: {authorization.reason}")
        candidate = MemoryCandidate(
            namespace=item.namespace,
            type=item.type,
            subject=item.subject,
            key=item.key,
            content=content,
            structured_data=item.structured_data,
            confidence=item.confidence,
            importance=item.importance,
            sensitivity=item.sensitivity,
            source_type=item.source_type,
        )
        if self.admission.secret_detector.contains_secret(candidate):
            raise ValueError("Secrets cannot be stored in memory")
        embedding = None
        if item.status == MemoryStatus.ACTIVE and self.embeddings is not None:
            embedding = await self.embeddings.embed(content)
        return await self.store.update_memory(
            item.model_copy(
                update={"content": content, "embedding": embedding, "updated_at": utc_now()}
            )
        )

    async def forget(self, context: MemoryOperationContext, *, memory_id: str) -> bool:
        item = await self._owned(context, memory_id)
        authorization = await self.authorizer.authorize(
            context, action="memory.forget", namespace=item.namespace
        )
        if not authorization.allowed:
            raise PermissionError(f"Memory forget denied: {authorization.reason}")
        return await self.store.forget_memory(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            memory_id=memory_id,
        )

    async def _owned(self, context: MemoryOperationContext, memory_id: str) -> MemoryItem:
        item = await self.store.get_memory(
            tenant_id=context.tenant_id, user_id=context.user_id, memory_id=memory_id
        )
        if item is None:
            raise KeyError("Memory not found in trusted owner scope")
        self._validate_namespace(context, item.namespace)
        return item

    @staticmethod
    def _validate_namespace(context: MemoryOperationContext, namespace: str) -> None:
        prefix, _, identifier = namespace.partition(":")
        allowed = False
        if prefix == "user":
            allowed = identifier == context.user_id
        elif prefix == "tenant":
            allowed = identifier == context.tenant_id and "memory.tenant" in context.capabilities
        elif prefix == "project":
            allowed = (
                "memory.project" in context.capabilities
                or f"memory.project:{identifier}" in context.capabilities
            )
        elif prefix == "system":
            allowed = "memory.system" in context.capabilities
        if not allowed:
            raise PermissionError("Memory namespace is outside the trusted identity scope")

    @staticmethod
    def _to_item(
        context: MemoryOperationContext,
        candidate: MemoryCandidate,
        *,
        status: MemoryStatus,
        supersedes_id: str | None,
        embedding: list[float] | None,
    ) -> MemoryItem:
        return MemoryItem(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            namespace=candidate.namespace,
            type=candidate.type,
            subject=candidate.subject,
            key=candidate.key,
            content=candidate.content,
            structured_data=candidate.structured_data,
            confidence=candidate.confidence,
            importance=candidate.importance,
            sensitivity=candidate.sensitivity,
            status=status,
            supersedes_id=supersedes_id,
            source_type=candidate.source_type,
            source_conversation_id=candidate.source_conversation_id,
            source_message_id=candidate.source_message_id,
            source_run_id=candidate.source_run_id,
            source_job_id=candidate.source_job_id,
            expires_at=candidate.expires_at,
            embedding=embedding,
        )

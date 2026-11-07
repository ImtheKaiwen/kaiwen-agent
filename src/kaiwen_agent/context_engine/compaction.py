from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kaiwen_agent.context_engine.token_counter import EstimatedTokenCounter, TokenCounter
from kaiwen_agent.memory import MemoryManager, SecretDetector, SessionSummary
from kaiwen_agent.realtime.conversation import ConversationMessage, ConversationStore


class CompactionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_context_tokens: int = Field(ge=2)
    soft_compaction_ratio: float = Field(default=0.8, gt=0, lt=1)
    recent_messages: int = Field(default=12, ge=1, le=100)
    message_limit: int = Field(default=1000, ge=2, le=1000)

    @property
    def threshold_tokens(self) -> int:
        return max(1, int(self.max_context_tokens * self.soft_compaction_ratio))


class CompactionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = ""
    goal: str | None = None
    decisions: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    active_projects: tuple[str, ...] = ()
    completed: tuple[str, ...] = ()
    next_steps: tuple[str, ...] = ()
    known_constraints: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    source_message_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_content(self) -> CompactionDraft:
        if not any(
            (
                self.summary.strip(),
                self.goal and self.goal.strip(),
                self.decisions,
                self.open_questions,
                self.active_projects,
                self.completed,
                self.next_steps,
                self.known_constraints,
                self.artifact_refs,
            )
        ):
            raise ValueError("Compaction draft must contain summary content")
        return self


class CompactionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    messages: tuple[ConversationMessage, ...] = Field(min_length=1)
    previous_summary: SessionSummary | None = None


class SummaryGenerator(Protocol):
    async def generate(self, request: CompactionRequest) -> CompactionDraft: ...


class CompactionStatus(StrEnum):
    SKIPPED = "skipped"
    COMPACTED = "compacted"
    REJECTED = "rejected"


class CompactionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: CompactionStatus
    observed_tokens: int = Field(ge=0)
    summary: SessionSummary | None = None
    reason: str | None = None


class ContextCompactionManager:
    """Creates a lossy conversation summary while preserving canonical stores."""

    def __init__(
        self,
        *,
        conversations: ConversationStore,
        memory: MemoryManager,
        generator: SummaryGenerator,
        policy: CompactionPolicy,
        token_counter: TokenCounter | None = None,
        secret_detector: SecretDetector | None = None,
    ) -> None:
        self.conversations = conversations
        self.memory = memory
        self.generator = generator
        self.policy = policy
        self.token_counter = token_counter or EstimatedTokenCounter()
        self.secret_detector = secret_detector or SecretDetector()

    async def compact_if_needed(
        self,
        *,
        tenant_id: str | None,
        user_id: str | None,
        conversation_id: str | None,
        current_input: str,
    ) -> CompactionResult:
        if tenant_id is None or user_id is None or conversation_id is None:
            return CompactionResult(
                status=CompactionStatus.SKIPPED,
                observed_tokens=self.token_counter.count(current_input),
                reason="owner_scope_unavailable",
            )
        messages = await self.conversations.list_messages(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            limit=self.policy.message_limit,
        )
        scoped = [message for message in messages if message.user_id == user_id]
        previous = await self.memory.get_session_summary(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        pending = scoped
        if previous is not None and previous.source_message_id is not None:
            cursor = next(
                (
                    index
                    for index, message in enumerate(scoped)
                    if message.id == previous.source_message_id
                ),
                None,
            )
            if cursor is None:
                return CompactionResult(
                    status=CompactionStatus.REJECTED,
                    observed_tokens=self._observed_tokens(scoped, previous, current_input),
                    reason="source_cursor_missing",
                )
            pending = scoped[cursor + 1 :]
        observed = self._observed_tokens(pending, previous, current_input)
        if observed < self.policy.threshold_tokens:
            return CompactionResult(
                status=CompactionStatus.SKIPPED,
                observed_tokens=observed,
                reason="below_threshold",
            )
        candidates = pending[: -self.policy.recent_messages]
        if not candidates:
            return CompactionResult(
                status=CompactionStatus.SKIPPED,
                observed_tokens=observed,
                reason="no_old_messages",
            )
        try:
            draft = await self.generator.generate(
                CompactionRequest(messages=tuple(candidates), previous_summary=previous)
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return CompactionResult(
                status=CompactionStatus.REJECTED,
                observed_tokens=observed,
                reason="summary_generator_failed",
            )
        rejection = self._validate(draft, candidates, previous)
        if rejection is not None:
            return CompactionResult(
                status=CompactionStatus.REJECTED,
                observed_tokens=observed,
                reason=rejection,
            )
        try:
            summary = await self.memory.save_session_summary(
                SessionSummary(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    summary=draft.summary,
                    goal=draft.goal,
                    decisions=list(draft.decisions),
                    open_questions=list(draft.open_questions),
                    active_projects=list(draft.active_projects),
                    completed=list(draft.completed),
                    next_steps=list(draft.next_steps),
                    known_constraints=list(draft.known_constraints),
                    artifact_refs=list(draft.artifact_refs),
                    source_message_ids=[
                        *(previous.source_message_ids if previous else []),
                        *draft.source_message_ids,
                    ],
                    source_message_id=candidates[-1].id,
                )
            )
        except ValueError:
            return CompactionResult(
                status=CompactionStatus.REJECTED,
                observed_tokens=observed,
                reason="summary_token_limit_exceeded",
            )
        return CompactionResult(
            status=CompactionStatus.COMPACTED,
            observed_tokens=observed,
            summary=summary,
        )

    def _observed_tokens(
        self,
        messages: list[ConversationMessage],
        previous: SessionSummary | None,
        current_input: str,
    ) -> int:
        text = "\n".join(f"{message.role.value}: {message.text}" for message in messages)
        if previous is not None:
            text = f"{previous.render()}\n{text}"
        return self.token_counter.count(f"{text}\nuser: {current_input}")

    def _validate(
        self,
        draft: CompactionDraft,
        candidates: list[ConversationMessage],
        previous: SessionSummary | None,
    ) -> str | None:
        expected_ids = tuple(message.id for message in candidates)
        if draft.source_message_ids != expected_ids:
            return "source_message_ids_mismatch"
        allowed_artifacts = {
            item
            for message in candidates
            for item in message.metadata.get("artifact_refs", [])
            if isinstance(item, str)
        }
        if previous is not None:
            allowed_artifacts.update(previous.artifact_refs)
            if previous.goal and draft.goal != previous.goal:
                return "previous_goal_not_preserved"
            if not set(previous.decisions) <= set(draft.decisions):
                return "previous_decisions_not_preserved"
            if not set(previous.known_constraints) <= set(draft.known_constraints):
                return "previous_constraints_not_preserved"
            if not set(previous.artifact_refs) <= set(draft.artifact_refs):
                return "previous_artifact_references_not_preserved"
        if not set(draft.artifact_refs) <= allowed_artifacts:
            return "unknown_artifact_reference"
        rendered = "\n".join(
            [
                draft.summary,
                draft.goal or "",
                *draft.decisions,
                *draft.open_questions,
                *draft.active_projects,
                *draft.completed,
                *draft.next_steps,
                *draft.known_constraints,
            ]
        )
        if self.secret_detector.contains_secret_text(rendered):
            return "secret_detected"
        return None

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import BaseModel

from kaiwen_agent import AgentContext, tool
from kaiwen_agent.artifacts import ArtifactContext, ArtifactManager
from kaiwen_agent.context_engine import (
    CallableTokenCounter,
    CompactionDraft,
    CompactionPolicy,
    CompactionRequest,
    CompactionStatus,
    ContextBudget,
    ContextBudgetAllocator,
    ContextBuilder,
    ContextCompactionManager,
    ContextItem,
    ContextKind,
    ContextWindow,
    StaticContextSource,
)
from kaiwen_agent.memory import (
    InMemorySessionMemoryStore,
    SessionMemoryManager,
    SessionSummary,
)
from kaiwen_agent.realtime import (
    ConversationMessage,
    ConversationModality,
    ConversationRole,
    InMemoryConversationStore,
)
from kaiwen_agent.runtime import AgentRuntime, ContextWindowExceededError, RuntimeIdentity
from kaiwen_agent.tools import ArtifactToolResultProcessor, ToolOutputOffloadError
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.types import AgentInput, ModelResponse, ToolCall, ToolResult


class CapturingProvider:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def respond(
        self,
        agent_input: AgentInput,
        *,
        context: AgentContext,
        tools: Sequence[ToolDefinition],
        tool_results: Sequence[ToolResult],
        provider_state: Any | None,
    ) -> ModelResponse:
        self.calls.append(
            {
                "input": agent_input,
                "context": context,
                "tools": tuple(tools),
                "tool_results": tuple(tool_results),
                "provider_state": provider_state,
            }
        )
        return self.responses.pop(0)


class LargeOutputInput(BaseModel):
    size: int


@tool(
    name="output.large",
    description="Return a large output",
    input_model=LargeOutputInput,
    permissions={"output.read"},
)
async def large_output(context: AgentContext, arguments: LargeOutputInput) -> dict[str, str]:
    del context
    return {"content": "x" * arguments.size}


class FakeSummaryGenerator:
    def __init__(self, *, invalid_sources: bool = False, secret: bool = False) -> None:
        self.invalid_sources = invalid_sources
        self.secret = secret
        self.calls: list[CompactionRequest] = []

    async def generate(self, request: CompactionRequest) -> CompactionDraft:
        self.calls.append(request)
        previous = request.previous_summary
        source_ids = tuple(message.id for message in request.messages)
        if self.invalid_sources:
            source_ids = ("unknown",)
        return CompactionDraft(
            summary=("api_key=super-secret-value" if self.secret else "Older discussion"),
            goal=previous.goal if previous else "Keep the goal",
            decisions=tuple(previous.decisions) if previous else ("Keep policy server-side",),
            known_constraints=(
                tuple(previous.known_constraints) if previous else ("Do not lose constraints",)
            ),
            source_message_ids=source_ids,
        )


def runtime_identity(*, owned: bool = True) -> RuntimeIdentity:
    return RuntimeIdentity(
        session_id="session_1",
        tenant_id="tenant_1" if owned else None,
        user_id="user_1" if owned else None,
        conversation_id="conversation_1" if owned else None,
        permissions=frozenset({"output.read"}),
    )


def test_provider_aware_counter_controls_context_allocation() -> None:
    counter = CallableTokenCounter(lambda text: len(text.split()))
    allocator = ContextBudgetAllocator(counter)
    built = allocator.allocate(
        [
            ContextItem(
                source="test",
                kind=ContextKind.CURRENT_INPUT,
                content="one two three",
                required=True,
            )
        ],
        ContextBudget(max_total_tokens=2, current_input=2),
    )
    assert built.estimated_tokens == 2
    assert built.items[0].content == "one two…"
    assert counter.count("one two three") == 3
    assert counter.truncate("one two three", 2) == "one two…"


def test_runtime_builds_bounded_context_without_changing_legacy_provider_contract() -> None:
    async def scenario() -> None:
        provider = CapturingProvider([ModelResponse(text="ok")])
        builder = ContextBuilder(
            [StaticContextSource(name="canonical", content="Pinned instruction")]
        )
        runtime = AgentRuntime(
            name="assistant",
            model=provider,
            context_builder=builder,
            context_budget=ContextBudget(max_total_tokens=100),
            context_window=ContextWindow(
                max_context_tokens=200,
                reserved_output_tokens=20,
                reserved_request_tokens=20,
            ),
        )
        result = await runtime.chat("Current question", identity=runtime_identity())
        assert result.text == "ok"
        sent = provider.calls[0]["input"]
        assert "[system:canonical]\nPinned instruction" in sent.text
        assert "[current_input:current_input]\nCurrent question" in sent.text
        assert sent.metadata["original_input"] == "Current question"
        context = provider.calls[0]["context"]
        assert context.metadata["built_context"]["truncated"] is False

    asyncio.run(scenario())


def test_context_window_counts_tool_schema_and_fails_closed() -> None:
    async def scenario() -> None:
        provider = CapturingProvider([])
        runtime = AgentRuntime(
            name="assistant",
            model=provider,
            tools=[large_output],
            permissions={"output.read"},
            context_builder=ContextBuilder(),
            context_window=ContextWindow(max_context_tokens=10, reserved_output_tokens=1),
        )
        with pytest.raises(ContextWindowExceededError):
            await runtime.action(
                "run",
                identity=runtime_identity(),
                tool_names=("output.large",),
            )
        assert provider.calls == []

    asyncio.run(scenario())


def test_compaction_preserves_recent_raw_history_and_validated_summary() -> None:
    async def scenario() -> None:
        conversations = InMemoryConversationStore()
        messages = [
            ConversationMessage(
                id=f"message_{index}",
                tenant_id="tenant_1",
                user_id="user_1",
                conversation_id="conversation_1",
                role=ConversationRole.USER if index % 2 == 0 else ConversationRole.ASSISTANT,
                modality=ConversationModality.TEXT,
                text=f"long message {index} " + ("x" * 30),
            )
            for index in range(4)
        ]
        for message in messages:
            await conversations.append_message(message)
        memory = SessionMemoryManager(InMemorySessionMemoryStore())
        generator = FakeSummaryGenerator()
        manager = ContextCompactionManager(
            conversations=conversations,
            memory=memory,
            generator=generator,
            policy=CompactionPolicy(
                max_context_tokens=10,
                soft_compaction_ratio=0.5,
                recent_messages=1,
            ),
        )
        result = await manager.compact_if_needed(
            tenant_id="tenant_1",
            user_id="user_1",
            conversation_id="conversation_1",
            current_input="next",
        )
        assert result.status == CompactionStatus.COMPACTED
        assert result.summary is not None
        assert result.summary.source_message_ids == [item.id for item in messages[:-1]]
        assert result.summary.known_constraints == ["Do not lose constraints"]
        raw = await conversations.list_messages(
            tenant_id="tenant_1", conversation_id="conversation_1", limit=100
        )
        assert [item.id for item in raw] == [item.id for item in messages]
        assert generator.calls[0].messages[-1].id == messages[-2].id

        additions = [
            ConversationMessage(
                id=f"later_{index}",
                tenant_id="tenant_1",
                user_id="user_1",
                conversation_id="conversation_1",
                role=ConversationRole.USER,
                modality=ConversationModality.TEXT,
                text="later " + ("y" * 30),
            )
            for index in range(2)
        ]
        for message in additions:
            await conversations.append_message(message)
        repeated = await manager.compact_if_needed(
            tenant_id="tenant_1",
            user_id="user_1",
            conversation_id="conversation_1",
            current_input="next again",
        )
        assert repeated.status == CompactionStatus.COMPACTED
        assert [item.id for item in generator.calls[1].messages] == [
            messages[-1].id,
            additions[0].id,
        ]
        assert repeated.summary is not None
        assert repeated.summary.source_message_ids == [
            *(item.id for item in messages[:-1]),
            messages[-1].id,
            additions[0].id,
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("generator", "reason"),
    [
        (FakeSummaryGenerator(invalid_sources=True), "source_message_ids_mismatch"),
        (FakeSummaryGenerator(secret=True), "secret_detected"),
    ],
)
def test_compaction_rejects_unverifiable_or_secret_summary(
    generator: FakeSummaryGenerator, reason: str
) -> None:
    async def scenario() -> None:
        conversations = InMemoryConversationStore()
        for _index in range(3):
            await conversations.append_message(
                ConversationMessage(
                    tenant_id="tenant_1",
                    user_id="user_1",
                    conversation_id="conversation_1",
                    role=ConversationRole.USER,
                    modality=ConversationModality.TEXT,
                    text="x" * 30,
                )
            )
        memory = SessionMemoryManager(InMemorySessionMemoryStore())
        manager = ContextCompactionManager(
            conversations=conversations,
            memory=memory,
            generator=generator,
            policy=CompactionPolicy(
                max_context_tokens=4,
                soft_compaction_ratio=0.5,
                recent_messages=1,
            ),
        )
        result = await manager.compact_if_needed(
            tenant_id="tenant_1",
            user_id="user_1",
            conversation_id="conversation_1",
            current_input="next",
        )
        assert result.status == CompactionStatus.REJECTED
        assert result.reason == reason
        assert (
            await memory.get_session_summary(
                tenant_id="tenant_1",
                user_id="user_1",
                conversation_id="conversation_1",
            )
            is None
        )

    asyncio.run(scenario())


def test_compaction_cannot_drop_previous_goal_decisions_or_constraints() -> None:
    class LossyGenerator:
        async def generate(self, request: CompactionRequest) -> CompactionDraft:
            return CompactionDraft(
                summary="lossy",
                goal="different goal",
                source_message_ids=tuple(item.id for item in request.messages),
            )

    async def scenario() -> None:
        conversations = InMemoryConversationStore()
        for _index in range(3):
            await conversations.append_message(
                ConversationMessage(
                    tenant_id="tenant_1",
                    user_id="user_1",
                    conversation_id="conversation_1",
                    role=ConversationRole.USER,
                    modality=ConversationModality.TEXT,
                    text="x" * 30,
                )
            )
        memory = SessionMemoryManager(InMemorySessionMemoryStore())
        await memory.save_session_summary(
            SessionSummary(
                tenant_id="tenant_1",
                user_id="user_1",
                conversation_id="conversation_1",
                goal="canonical goal",
                decisions=["decision"],
                known_constraints=["constraint"],
            )
        )
        manager = ContextCompactionManager(
            conversations=conversations,
            memory=memory,
            generator=LossyGenerator(),
            policy=CompactionPolicy(
                max_context_tokens=4,
                soft_compaction_ratio=0.5,
                recent_messages=1,
            ),
        )
        result = await manager.compact_if_needed(
            tenant_id="tenant_1",
            user_id="user_1",
            conversation_id="conversation_1",
            current_input="next",
        )
        assert result.status == CompactionStatus.REJECTED
        assert result.reason == "previous_goal_not_preserved"

    asyncio.run(scenario())


def test_large_tool_output_is_offloaded_and_provider_receives_reference() -> None:
    async def scenario() -> None:
        artifacts = ArtifactManager()
        provider = CapturingProvider(
            [
                ModelResponse(
                    tool_calls=[ToolCall(id="call_1", name="output.large", arguments={"size": 100})]
                ),
                ModelResponse(text="done"),
            ]
        )
        runtime = AgentRuntime(
            name="assistant",
            model=provider,
            tools=[large_output],
            permissions={"output.read"},
            tool_result_processor=ArtifactToolResultProcessor(
                artifacts, threshold_bytes=20, preview_characters=8
            ),
        )
        result = await runtime.action(
            "run",
            identity=runtime_identity(),
            tool_names=("output.large",),
        )
        offloaded = result.tool_results[0].output
        assert offloaded["offloaded"] is True
        assert offloaded["preview"] == '{"conten'
        assert provider.calls[1]["tool_results"][0].output == offloaded
        stored = await artifacts.read(
            ArtifactContext(
                tenant_id="tenant_1",
                user_id="user_1",
                session_id="session_1",
                trace_id=provider.calls[0]["context"].trace_id,
                run_id=result.run_id,
            ),
            offloaded["artifact_id"],
        )
        assert stored.metadata.checksum_sha256 == offloaded["checksum_sha256"]

    asyncio.run(scenario())


def test_large_tool_output_without_owner_scope_fails_closed() -> None:
    async def scenario() -> None:
        provider = CapturingProvider(
            [
                ModelResponse(
                    tool_calls=[ToolCall(id="call_1", name="output.large", arguments={"size": 100})]
                )
            ]
        )
        runtime = AgentRuntime(
            name="assistant",
            model=provider,
            tools=[large_output],
            permissions={"output.read"},
            tool_result_processor=ArtifactToolResultProcessor(
                ArtifactManager(), threshold_bytes=20
            ),
        )
        with pytest.raises(ToolOutputOffloadError, match="requires tenant"):
            await runtime.action(
                "run",
                identity=runtime_identity(owned=False),
                tool_names=("output.large",),
            )

    asyncio.run(scenario())

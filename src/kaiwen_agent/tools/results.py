from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Protocol

from kaiwen_agent.artifacts import (
    ArtifactContext,
    ArtifactCreate,
    ArtifactManager,
    ArtifactType,
)
from kaiwen_agent.context import AgentContext
from kaiwen_agent.events import EventType
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.tools.execution import ToolExecutor
from kaiwen_agent.types import ToolCall, ToolResult

ToolEventCallback = Callable[[EventType, dict[str, object]], Awaitable[None]]


async def _no_events(event_type: EventType, payload: dict[str, object]) -> None:
    del event_type, payload


class ToolResultProcessor(Protocol):
    async def process(
        self,
        result: ToolResult,
        *,
        context: AgentContext,
        run_id: str,
    ) -> ToolResult: ...


class PassthroughToolResultProcessor:
    async def process(
        self,
        result: ToolResult,
        *,
        context: AgentContext,
        run_id: str,
    ) -> ToolResult:
        del context, run_id
        return result


class ToolOutputOffloadError(RuntimeError):
    pass


class ArtifactToolResultProcessor:
    """Moves large model-facing tool output into an owner-scoped artifact."""

    def __init__(
        self,
        artifacts: ArtifactManager,
        *,
        threshold_bytes: int = 32_768,
        preview_characters: int = 1_000,
    ) -> None:
        if threshold_bytes < 1:
            raise ValueError("threshold_bytes must be positive")
        if preview_characters < 0:
            raise ValueError("preview_characters cannot be negative")
        self.artifacts = artifacts
        self.threshold_bytes = threshold_bytes
        self.preview_characters = preview_characters

    async def process(
        self,
        result: ToolResult,
        *,
        context: AgentContext,
        run_id: str,
    ) -> ToolResult:
        serialized = json.dumps(
            result.output,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        content = serialized.encode("utf-8")
        if len(content) <= self.threshold_bytes:
            return result
        if context.tenant_id is None or context.user_id is None or context.trace_id is None:
            raise ToolOutputOffloadError(
                "Large tool output requires tenant, user, and trace ownership"
            )
        metadata = await self.artifacts.create(
            ArtifactContext(
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                session_id=context.session_id,
                trace_id=context.trace_id,
                run_id=run_id,
            ),
            ArtifactCreate(
                type=ArtifactType.JOB_RESULT,
                mime_type="application/json",
                content=content,
                labels={"tool": result.name, "call_id": result.call_id},
            ),
        )
        return ToolResult(
            call_id=result.call_id,
            name=result.name,
            output={
                "offloaded": True,
                "artifact_id": metadata.id,
                "storage_ref": metadata.storage_ref,
                "checksum_sha256": metadata.checksum_sha256,
                "size_bytes": metadata.size_bytes,
                "mime_type": metadata.mime_type,
                "preview": serialized[: self.preview_characters],
            },
        )


class ToolResultProcessingExecutor(ToolExecutor):
    """Decorates the existing executor without changing Agent's public API."""

    def __init__(self, executor: ToolExecutor, processor: ToolResultProcessor) -> None:
        self.executor = executor
        self.processor = processor

    async def execute(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: AgentContext,
        *,
        execution_scope: str,
        emit: ToolEventCallback = _no_events,
    ) -> ToolResult:
        result = await self.executor.execute(
            definition,
            call,
            context,
            execution_scope=execution_scope,
            emit=emit,
        )
        return await self.processor.process(
            result,
            context=context,
            run_id=execution_scope,
        )

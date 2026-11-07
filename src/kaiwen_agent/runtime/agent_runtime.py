from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from kaiwen_agent.agent import Agent
from kaiwen_agent.context_engine import (
    ContextBudget,
    ContextBuilder,
    ContextBuildRequest,
    ContextCompactionManager,
    ContextRenderer,
    ContextWindow,
)
from kaiwen_agent.events import EventSink, InMemoryEventSink
from kaiwen_agent.models.base import ModelProvider
from kaiwen_agent.runtime.models import AutonomyLevel, RunMode, RuntimeIdentity
from kaiwen_agent.sessions import InMemoryRunStore, RunStore
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.tools.execution import ToolExecutor
from kaiwen_agent.tools.registry import ToolRegistry
from kaiwen_agent.tools.results import ToolResultProcessingExecutor, ToolResultProcessor
from kaiwen_agent.types import AgentInput, AgentResult


class RuntimeModeUnavailableError(RuntimeError):
    pass


class ContextWindowExceededError(RuntimeError):
    pass


class AgentRuntime:
    """Single-agent-first composition facade over the existing Agent loop."""

    def __init__(
        self,
        *,
        name: str,
        model: ModelProvider,
        tools: Sequence[ToolDefinition] = (),
        permissions: set[str] | frozenset[str] | None = None,
        event_sink: EventSink | None = None,
        run_store: RunStore | None = None,
        tool_executor: ToolExecutor | None = None,
        tool_result_processor: ToolResultProcessor | None = None,
        services: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        default_autonomy: AutonomyLevel = AutonomyLevel.SUPERVISED,
        context_builder: ContextBuilder | None = None,
        context_budget: ContextBudget | None = None,
        context_window: ContextWindow | None = None,
        context_renderer: ContextRenderer | None = None,
        compaction_manager: ContextCompactionManager | None = None,
        max_steps: int = 8,
    ) -> None:
        if not name:
            raise ValueError("name must not be empty")
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self.name = name
        self.model = model
        self.registry = ToolRegistry(tools)
        self.permissions = frozenset(permissions or ())
        self.event_sink = event_sink or InMemoryEventSink()
        self.run_store = run_store or InMemoryRunStore()
        self.tool_executor = tool_executor or ToolExecutor()
        self.tool_result_processor = tool_result_processor
        self.services = dict(services or {})
        self.metadata = dict(metadata or {})
        self.default_autonomy = default_autonomy
        self.context_builder = context_builder
        self.context_budget = context_budget or ContextBudget()
        self.context_window = context_window
        self.context_renderer = context_renderer or ContextRenderer()
        self.compaction_manager = compaction_manager
        self.max_steps = max_steps

    async def run(
        self,
        agent_input: AgentInput | str,
        *,
        identity: RuntimeIdentity,
        mode: RunMode = RunMode.CHAT,
        autonomy: AutonomyLevel | None = None,
        tool_names: Sequence[str] = (),
        services: Mapping[str, Any] | None = None,
    ) -> AgentResult:
        if mode == RunMode.WORKFLOW:
            raise RuntimeModeUnavailableError(
                "Workflow mode requires the durable workflow controller"
            )
        selected_tools = self._select_tools(tool_names) if mode == RunMode.ACTION else ()
        if mode == RunMode.CHAT and tool_names:
            raise ValueError("Chat mode does not expose tools; use action mode explicitly")
        normalized_input = (
            agent_input if isinstance(agent_input, AgentInput) else AgentInput(text=agent_input)
        )
        effective_autonomy = autonomy or self.default_autonomy
        effective_services = {**self.services, **dict(services or {})}
        effective_identity = identity.model_copy(
            update={
                "trace_id": identity.trace_id or f"trace_{uuid4().hex}",
                "metadata": {**identity.metadata, **self.metadata},
            }
        )
        normalized_input, context_metadata = await self._prepare_context(
            normalized_input,
            identity=effective_identity,
            mode=mode,
            selected_tools=selected_tools,
        )
        effective_identity = effective_identity.model_copy(
            update={"metadata": {**effective_identity.metadata, **context_metadata}}
        )
        context = effective_identity.to_agent_context(
            agent_id=self.name,
            services=effective_services,
            mode=mode,
            autonomy=effective_autonomy,
        )
        agent = Agent(
            name=self.name,
            model=self.model,
            tools=list(selected_tools),
            permissions=self.permissions,
            event_sink=self.event_sink,
            run_store=self.run_store,
            tool_executor=(
                ToolResultProcessingExecutor(self.tool_executor, self.tool_result_processor)
                if self.tool_result_processor is not None
                else self.tool_executor
            ),
            max_steps=self.max_steps,
        )
        return await agent.run(normalized_input, context=context)

    async def chat(
        self,
        text: str,
        *,
        identity: RuntimeIdentity,
        autonomy: AutonomyLevel | None = None,
        services: Mapping[str, Any] | None = None,
    ) -> AgentResult:
        return await self.run(
            text,
            identity=identity,
            mode=RunMode.CHAT,
            autonomy=autonomy,
            services=services,
        )

    async def action(
        self,
        text: str,
        *,
        identity: RuntimeIdentity,
        tool_names: Sequence[str],
        autonomy: AutonomyLevel | None = None,
        services: Mapping[str, Any] | None = None,
    ) -> AgentResult:
        return await self.run(
            text,
            identity=identity,
            mode=RunMode.ACTION,
            autonomy=autonomy,
            tool_names=tool_names,
            services=services,
        )

    def _select_tools(self, tool_names: Sequence[str]) -> tuple[ToolDefinition, ...]:
        if len(tool_names) != len(set(tool_names)):
            raise ValueError("tool_names must not contain duplicates")
        return tuple(self.registry.get(name) for name in tool_names)

    async def _prepare_context(
        self,
        agent_input: AgentInput,
        *,
        identity: RuntimeIdentity,
        mode: RunMode,
        selected_tools: tuple[ToolDefinition, ...],
    ) -> tuple[AgentInput, dict[str, Any]]:
        metadata: dict[str, Any] = {}
        if self.compaction_manager is not None:
            compacted = await self.compaction_manager.compact_if_needed(
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                conversation_id=identity.conversation_id,
                current_input=agent_input.text,
            )
            metadata["context_compaction"] = compacted.model_dump(mode="json", exclude={"summary"})
        if self.context_builder is None:
            return agent_input, metadata

        budget = self._effective_context_budget(selected_tools)
        built = await self.context_builder.build(
            ContextBuildRequest(
                agent_id=self.name,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                session_id=identity.session_id,
                conversation_id=identity.conversation_id,
                query=agent_input.text,
                capability=mode.value,
                trace_id=identity.trace_id or f"trace_{uuid4().hex}",
                capabilities=identity.permissions,
                metadata=agent_input.metadata,
            ),
            budget=budget,
        )
        rendered = self.context_renderer.render(built)
        if self.context_window is not None:
            counter = self.context_builder.allocator.token_counter
            tool_tokens = self._tool_schema_tokens(selected_tools)
            if counter.count(rendered) + tool_tokens > self.context_window.available_input_tokens:
                raise ContextWindowExceededError(
                    "Rendered context and tool schemas exceed the configured input window"
                )
        metadata["built_context"] = {
            "estimated_tokens": built.estimated_tokens,
            "truncated": built.truncated,
            "omitted_sources": built.omitted_sources,
        }
        return (
            AgentInput(
                text=rendered,
                metadata={**agent_input.metadata, "original_input": agent_input.text},
            ),
            metadata,
        )

    def _effective_context_budget(
        self, selected_tools: tuple[ToolDefinition, ...]
    ) -> ContextBudget:
        if self.context_window is None:
            return self.context_budget
        available = self.context_window.available_input_tokens - self._tool_schema_tokens(
            selected_tools
        )
        if available < 1:
            raise ContextWindowExceededError(
                "Tool schemas and output reservation leave no model input capacity"
            )
        return self.context_budget.model_copy(
            update={"max_total_tokens": min(self.context_budget.max_total_tokens, available)}
        )

    def _tool_schema_tokens(self, tools: tuple[ToolDefinition, ...]) -> int:
        if not tools or self.context_builder is None:
            return 0
        serialized = json.dumps(
            [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in tools
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        return self.context_builder.allocator.token_counter.count(serialized)

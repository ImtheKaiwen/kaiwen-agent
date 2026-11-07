from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from kaiwen_agent.context import AgentContext
from kaiwen_agent.events import AgentEvent, EventSink, EventType, InMemoryEventSink
from kaiwen_agent.models.base import ModelProvider
from kaiwen_agent.sessions import InMemoryRunStore, RunStore
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.tools.execution import ToolExecutor
from kaiwen_agent.tools.registry import ToolRegistry
from kaiwen_agent.types import AgentInput, AgentResult, AgentRun, ModelUsage, ToolResult


class Agent:
    def __init__(
        self,
        *,
        name: str,
        model: ModelProvider,
        tools: list[ToolDefinition] | None = None,
        permissions: set[str] | frozenset[str] | None = None,
        event_sink: EventSink | None = None,
        run_store: RunStore | None = None,
        tool_executor: ToolExecutor | None = None,
        max_steps: int = 8,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self.name = name
        self.model = model
        self.registry = ToolRegistry(tools or [])
        self.permissions = frozenset(permissions or set())
        self.event_sink = event_sink or InMemoryEventSink()
        self.run_store = run_store or InMemoryRunStore()
        self.tool_executor = tool_executor or ToolExecutor()
        self.max_steps = max_steps

    async def run(self, agent_input: AgentInput, *, context: AgentContext) -> AgentResult:
        trace_id = context.trace_id or f"trace_{uuid4().hex}"
        effective_context = AgentContext(
            session_id=context.session_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            agent_id=self.name,
            conversation_id=context.conversation_id,
            client_id=context.client_id,
            trace_id=trace_id,
            environment=context.environment,
            permissions=context.permissions & self.permissions,
            services=context.services,
            metadata={**context.metadata, "configured_agent_id": self.name},
        )
        run = AgentRun(session_id=context.session_id, status="running")
        await self.run_store.save(run)
        await self._emit("run.started", run, trace_id, {"agent": self.name}, effective_context)
        tool_results: list[ToolResult] = []
        pending_tool_results: list[ToolResult] = []
        provider_state: object | None = None
        total_usage = ModelUsage()

        try:
            for step in range(self.max_steps):
                response = await self.model.respond(
                    agent_input,
                    context=effective_context,
                    tools=self.registry.definitions(),
                    tool_results=pending_tool_results,
                    provider_state=provider_state,
                )
                pending_tool_results = []
                provider_state = response.provider_state
                if response.usage is not None:
                    total_usage.input_tokens += response.usage.input_tokens
                    total_usage.output_tokens += response.usage.output_tokens
                    total_usage.total_tokens += response.usage.total_tokens
                    await self._emit(
                        "run.status",
                        run,
                        trace_id,
                        {
                            "kind": "model_usage",
                            "model": response.model or "unknown",
                            **response.usage.model_dump(),
                        },
                        effective_context,
                    )
                if not response.tool_calls:
                    result = AgentResult(
                        run_id=run.id,
                        session_id=run.session_id,
                        text=response.text or "",
                        status="completed",
                        tool_results=tool_results,
                        structured_output=response.structured_output,
                        usage=total_usage,
                    )
                    run.status = "completed"
                    run.completed_at = datetime.now(UTC)
                    await self.run_store.save(run)
                    await self._emit(
                        "run.completed",
                        run,
                        trace_id,
                        {"step": step, "text": result.text},
                        effective_context,
                    )
                    return result

                for call in response.tool_calls:
                    definition = self.registry.get(call.name)
                    tool_result = await self.tool_executor.execute(
                        definition,
                        call,
                        effective_context,
                        execution_scope=run.id,
                        emit=lambda event_type, payload: self._emit(
                            event_type, run, trace_id, payload, effective_context
                        ),
                    )
                    tool_results.append(tool_result)
                    pending_tool_results.append(tool_result)

            raise RuntimeError(f"Agent exceeded max_steps={self.max_steps}")
        except Exception as error:
            run.status = "failed"
            run.completed_at = datetime.now(UTC)
            await self.run_store.save(run)
            await self._emit(
                "run.failed",
                run,
                trace_id,
                {"error_type": type(error).__name__},
                effective_context,
            )
            raise

    async def _emit(
        self,
        event_type: EventType,
        run: AgentRun,
        trace_id: str,
        payload: dict[str, object],
        context: AgentContext,
    ) -> None:
        await self.event_sink.append(
            AgentEvent(
                type=event_type,
                trace_id=trace_id,
                session_id=run.session_id,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                payload=payload,
            )
        )

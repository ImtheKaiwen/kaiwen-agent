from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import ValidationError

from kaiwen_agent.audit import SecretRedactor
from kaiwen_agent.context import AgentContext
from kaiwen_agent.events import AgentEvent, EventSink, EventType, InMemoryEventSink
from kaiwen_agent.exceptions import (
    ActionOutcomeUncertainError,
    ToolArgumentsError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolTimeoutError,
)
from kaiwen_agent.models.base import ModelProvider
from kaiwen_agent.sessions import InMemoryRunStore, RunStore
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.tools.durability import DurableActionScope
from kaiwen_agent.tools.execution import ToolExecutor, require_action_scope_support
from kaiwen_agent.tools.registry import ToolRegistry
from kaiwen_agent.types import (
    AgentInput,
    AgentResult,
    AgentRun,
    ModelUsage,
    ToolCall,
    ToolResult,
)


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
        tool_errors: Literal["fail", "return"] = "fail",
        max_tool_errors: int = 3,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if tool_errors not in {"fail", "return"}:
            raise ValueError("tool_errors must be 'fail' or 'return'")
        if max_tool_errors < 1:
            raise ValueError("max_tool_errors must be at least 1")
        self.name = name
        self.model = model
        self.registry = ToolRegistry(tools or [])
        self.permissions = frozenset(permissions or set())
        self.event_sink = event_sink or InMemoryEventSink()
        self.run_store = run_store or InMemoryRunStore()
        self.tool_executor = tool_executor or ToolExecutor()
        self.max_steps = max_steps
        self.tool_errors = tool_errors
        self.max_tool_errors = max_tool_errors

    async def run(
        self,
        agent_input: AgentInput,
        *,
        context: AgentContext,
        action_scope: DurableActionScope | None = None,
    ) -> AgentResult:
        if action_scope is not None:
            # Before any model call or tool: an executor that cannot enforce the scope would
            # attribute side effects to the wrong run.
            require_action_scope_support(self.tool_executor)
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
        returned_errors = 0

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
                    definition = None
                    try:
                        definition = self.registry.get(call.name)
                        # Only a host-verified durable scope is forwarded, so executors that
                        # predate it keep working for standalone runs.
                        scope_args: dict[str, Any] = (
                            {} if action_scope is None else {"action_scope": action_scope}
                        )
                        tool_result = await self.tool_executor.execute(
                            definition,
                            call,
                            effective_context,
                            execution_scope=run.id,
                            emit=lambda event_type, payload: self._emit(
                                event_type, run, trace_id, payload, effective_context
                            ),
                            **scope_args,
                        )
                    except Exception as error:
                        sanitized = _sanitized_tool_error(error, definition, call)
                        if (
                            sanitized is None
                            or self.tool_errors != "return"
                            or returned_errors >= self.max_tool_errors
                        ):
                            raise
                        returned_errors += 1
                        tool_result = sanitized
                        await self._emit(
                            "run.status",
                            run,
                            trace_id,
                            {
                                "kind": "tool_error_returned",
                                "tool": call.name,
                                "call_id": call.id,
                                "error_type": sanitized.output["error"]["type"],
                            },
                            effective_context,
                        )
                    tool_results.append(tool_result)
                    pending_tool_results.append(tool_result)

            raise RuntimeError(f"Agent exceeded max_steps={self.max_steps}")
        except asyncio.CancelledError:
            # Cancellation is a distinct terminal state, recorded even if cancelled again.
            await asyncio.shield(self._record_cancellation(run, trace_id, effective_context))
            raise
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

    async def _record_cancellation(
        self, run: AgentRun, trace_id: str, context: AgentContext
    ) -> None:
        run.status = "cancelled"
        run.completed_at = datetime.now(UTC)
        await self.run_store.save(run)
        await self._emit("run.cancelled", run, trace_id, {"agent": self.name}, context)

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


_REDACTOR = SecretRedactor()


def _sanitized_tool_error(
    error: Exception, definition: ToolDefinition | None, call: ToolCall
) -> ToolResult | None:
    """Return a correctable, secret-free result for errors the model may fix itself.

    Permission, policy, approval, security, idempotency-conflict, and uncertain
    side-effect errors return None and therefore always stay fatal. Failures after a
    side-effecting handler started are never returned because their outcome is unknown.
    """

    details: list[dict[str, Any]] | None = None
    if isinstance(error, ToolNotFoundError):
        kind, message = "unknown_tool", f"Tool is not available: {call.name[:100]}"
    elif isinstance(error, ToolArgumentsError):
        kind, message = "validation_error", f"Invalid arguments for {call.name[:100]}"
        cause = error.__cause__
        if isinstance(cause, ValidationError):
            details = [
                {
                    "loc": list(item["loc"]),
                    "type": item["type"],
                    "message": _REDACTOR.redact(str(item["msg"]))[:200],
                }
                for item in cause.errors(
                    include_url=False, include_context=False, include_input=False
                )[:10]
            ]
    elif (
        isinstance(error, ToolExecutionError | ToolTimeoutError)
        and not isinstance(error, ActionOutcomeUncertainError)
        and not isinstance(error.__cause__, PermissionError)
        and definition is not None
        and definition.side_effect == "none"
    ):
        timed_out = isinstance(error, ToolTimeoutError)
        kind = "tool_timeout" if timed_out else "tool_error"
        message = (
            "The tool timed out. Try a smaller request."
            if timed_out
            else "The tool failed. Try different arguments or another approach."
        )
    else:
        return None
    payload: dict[str, Any] = {"type": kind, "message": message}
    if details is not None:
        payload["details"] = details
    return ToolResult(call_id=call.id, name=call.name, output={"error": payload}, is_error=True)

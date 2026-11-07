from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from hashlib import sha256
from typing import Any, Literal

from pydantic import ValidationError

from kaiwen_agent.audit import AuditRecord, AuditStore, RedactingAuditStore, SecretRedactor
from kaiwen_agent.context import AgentContext
from kaiwen_agent.events import EventType
from kaiwen_agent.exceptions import (
    ActionOutcomeUncertainError,
    ApprovalDeniedError,
    ApprovalRequiredError,
    PermissionDeniedError,
    ToolExecutionError,
    ToolTimeoutError,
    ToolValidationError,
)
from kaiwen_agent.policy import (
    PolicyContext,
    PolicyDecisionType,
    PolicyEngine,
    ToolApprovalPolicy,
    ToolPermissionPolicy,
)
from kaiwen_agent.tools.approval import ApprovalGate, ApprovalRequest
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.tools.durability import (
    ActionIntentRecord,
    ActionIntentStatus,
    ActionIntentStore,
)
from kaiwen_agent.tools.idempotency import IdempotencyStore, InMemoryIdempotencyStore
from kaiwen_agent.types import ToolCall, ToolResult

ToolEventCallback = Callable[[EventType, dict[str, object]], Awaitable[None]]


async def _no_events(event_type: EventType, payload: dict[str, object]) -> None:
    del event_type, payload


class ToolExecutor:
    def __init__(
        self,
        *,
        approval_gate: ApprovalGate | None = None,
        idempotency_store: IdempotencyStore | None = None,
        audit_store: AuditStore | None = None,
        redactor: SecretRedactor | None = None,
        policy_engine: PolicyEngine | None = None,
        action_intent_store: ActionIntentStore | None = None,
    ) -> None:
        self.approval_gate = approval_gate
        self.idempotency_store = idempotency_store or InMemoryIdempotencyStore()
        self.audit_store = (
            RedactingAuditStore(audit_store, redactor) if audit_store is not None else None
        )
        self.policy_engine = policy_engine or PolicyEngine(audit_store=audit_store)
        self.action_intent_store = action_intent_store

    async def execute(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: AgentContext,
        *,
        execution_scope: str,
        emit: ToolEventCallback = _no_events,
    ) -> ToolResult:
        try:
            arguments = definition.input_model.model_validate(call.arguments)
        except ValidationError as error:
            await self._audit(
                context,
                definition,
                call,
                "failed",
                {"error_type": type(error).__name__, "arguments": call.arguments},
            )
            raise ToolValidationError(f"Invalid arguments for {definition.name}") from error

        policy_context = PolicyContext(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            agent_id=context.agent_id or "unknown-agent",
            session_id=context.session_id,
            conversation_id=context.conversation_id,
            run_id=execution_scope,
            trace_id=context.trace_id or "trace_unavailable",
            environment=context.environment,
            action=f"tool.execute:{definition.name}",
            resource=definition.name,
            capabilities=context.permissions,
            metadata={
                "tool_name": definition.name,
                "side_effect": definition.side_effect,
                "requires_approval": definition.requires_approval,
            },
        )
        decision = await self.policy_engine.evaluate(
            policy_context,
            extra_policies=(
                ToolPermissionPolicy(definition.permissions),
                ToolApprovalPolicy(
                    required=definition.requires_approval,
                    approval_scope=f"tool:{definition.name}",
                ),
            ),
            emit=emit,
        )
        if decision.decision == PolicyDecisionType.DENY:
            await self._audit(
                context,
                definition,
                call,
                "denied",
                {
                    "reason": decision.reason,
                    "policy_id": decision.policy_id,
                    "rule_id": decision.rule_id,
                },
            )
            raise PermissionDeniedError(f"Policy denied tool {definition.name}: {decision.reason}")

        if decision.modifications is not None:
            unknown = set(decision.modifications) - {"arguments"}
            modified_arguments = decision.modifications.get("arguments")
            if unknown or not isinstance(modified_arguments, dict):
                raise ToolValidationError(f"Unsupported policy modifications for {definition.name}")
            call = call.model_copy(update={"arguments": {**call.arguments, **modified_arguments}})
            try:
                arguments = definition.input_model.model_validate(call.arguments)
            except ValidationError as error:
                raise ToolValidationError(
                    f"Policy produced invalid arguments for {definition.name}"
                ) from error

        arguments_checksum = _arguments_checksum(call.arguments)
        idempotency_identity = call.idempotency_key or f"{execution_scope}:{call.id}"
        action_intent = self._action_intent(
            definition,
            call,
            context,
            execution_scope=execution_scope,
            arguments_checksum=arguments_checksum,
            idempotency_identity=idempotency_identity,
        )
        action_store = self.action_intent_store
        if action_intent is not None:
            assert action_store is not None
            existing_intent = await action_store.get_action_intent(
                intent_id=action_intent.id,
                tenant_id=action_intent.tenant_id,
                user_id=action_intent.user_id,
            )
            if (
                existing_intent is not None
                and existing_intent.status == ActionIntentStatus.SUCCEEDED
            ):
                if existing_intent.result is None:
                    raise ActionOutcomeUncertainError(
                        "Completed action is missing its durable receipt",
                        action_intent_id=existing_intent.id,
                    )
                await emit(
                    "tool.completed",
                    {"tool": definition.name, "call_id": call.id, "cached": True},
                )
                return existing_intent.result.model_copy(update={"call_id": call.id})
            if existing_intent is not None and existing_intent.status in {
                ActionIntentStatus.EXECUTING,
                ActionIntentStatus.UNCERTAIN,
            }:
                raise ActionOutcomeUncertainError(
                    f"Action outcome is uncertain for tool {definition.name}",
                    action_intent_id=existing_intent.id,
                )
            if existing_intent is None:
                await action_store.prepare_action_intent(action_intent)

        if decision.decision in {
            PolicyDecisionType.REQUIRE_APPROVAL,
            PolicyDecisionType.REVIEW,
        }:
            request = ApprovalRequest(
                tool_name=definition.name,
                call_id=call.id,
                arguments=call.arguments,
                side_effect=definition.side_effect,
                execution_scope=execution_scope,
                action_intent_id=action_intent.id if action_intent is not None else None,
                arguments_checksum=arguments_checksum,
            )
            await emit("approval.requested", {"tool": definition.name, "call_id": call.id})
            if self.approval_gate is None:
                await self._audit(
                    context, definition, call, "denied", {"reason": "approval_unavailable"}
                )
                raise ApprovalRequiredError(f"Tool {definition.name} requires approval")
            approved = await self.approval_gate.approve(request, context)
            await emit(
                "approval.resolved",
                {"tool": definition.name, "call_id": call.id, "approved": approved},
            )
            if not approved:
                if action_intent is not None:
                    assert action_store is not None
                    await action_store.transition_action_intent(
                        intent_id=action_intent.id,
                        tenant_id=action_intent.tenant_id,
                        user_id=action_intent.user_id,
                        expected=frozenset({ActionIntentStatus.PREPARED}),
                        status=ActionIntentStatus.CANCELLED,
                        error_code="approval_denied",
                    )
                await self._audit(
                    context, definition, call, "denied", {"reason": "approval_denied"}
                )
                raise ApprovalDeniedError(f"Approval denied for tool {definition.name}")

        idempotency_key = call.idempotency_key
        if definition.side_effect != "none" and idempotency_key is None:
            idempotency_key = f"{execution_scope}:{call.id}"
        if idempotency_key is not None:
            idempotency_key = _scoped_idempotency_key(context, definition.name, idempotency_key)
        if idempotency_key is not None and action_intent is None:
            cached = await self.idempotency_store.get(idempotency_key)
            if cached is not None:
                await self._audit(context, definition, call, "succeeded", {"cached": True})
                await emit(
                    "tool.completed",
                    {"tool": definition.name, "call_id": call.id, "cached": True},
                )
                return cached.model_copy(update={"call_id": call.id})

        if action_intent is not None:
            assert action_store is not None
            prepared = await action_store.prepare_action_intent(action_intent)
            if prepared.status == ActionIntentStatus.SUCCEEDED and prepared.result is not None:
                return prepared.result.model_copy(update={"call_id": call.id})
            claimed = await action_store.transition_action_intent(
                intent_id=prepared.id,
                tenant_id=prepared.tenant_id,
                user_id=prepared.user_id,
                expected=frozenset({ActionIntentStatus.PREPARED}),
                status=ActionIntentStatus.EXECUTING,
            )
            if claimed is None:
                raise ActionOutcomeUncertainError(
                    f"Action cannot be claimed safely for tool {definition.name}",
                    action_intent_id=prepared.id,
                )
            context = replace(
                context,
                metadata={
                    **context.metadata,
                    "action_intent_id": claimed.id,
                    "idempotency_key": claimed.idempotency_key,
                },
            )

        await emit("tool.started", {"tool": definition.name, "call_id": call.id})
        last_error: BaseException | None = None
        for attempt in range(1, definition.retry_policy.max_attempts + 1):
            try:
                output: Any = await asyncio.wait_for(
                    definition.handler(context, arguments),
                    timeout=definition.timeout_seconds,
                )
                result = ToolResult(call_id=call.id, name=definition.name, output=output)
                if action_intent is not None:
                    assert action_store is not None
                    completed = await action_store.transition_action_intent(
                        intent_id=action_intent.id,
                        tenant_id=action_intent.tenant_id,
                        user_id=action_intent.user_id,
                        expected=frozenset({ActionIntentStatus.EXECUTING}),
                        status=ActionIntentStatus.SUCCEEDED,
                        result=result,
                    )
                    if completed is None:
                        raise ActionOutcomeUncertainError(
                            "Tool succeeded but its durable receipt could not be committed",
                            action_intent_id=action_intent.id,
                        )
                if idempotency_key is not None:
                    await self.idempotency_store.put(idempotency_key, result)
                await self._audit(
                    context,
                    definition,
                    call,
                    "succeeded",
                    {"attempt": attempt, "cached": False},
                )
                await emit(
                    "tool.completed",
                    {
                        "tool": definition.name,
                        "call_id": call.id,
                        "attempt": attempt,
                        "cached": False,
                    },
                )
                return result
            except ActionOutcomeUncertainError as error:
                last_error = error
                retryable = False
            except TimeoutError:
                last_error = ToolTimeoutError(
                    f"Tool {definition.name} timed out after {definition.timeout_seconds}s"
                )
                retryable = definition.idempotent and (
                    TimeoutError in definition.retry_policy.retry_on
                )
            except Exception as error:
                last_error = error
                retryable = definition.idempotent and isinstance(
                    error, definition.retry_policy.retry_on
                )

            if attempt < definition.retry_policy.max_attempts and retryable:
                if definition.retry_policy.backoff_seconds:
                    await asyncio.sleep(definition.retry_policy.backoff_seconds)
                continue
            break

        await emit(
            "tool.failed",
            {
                "tool": definition.name,
                "call_id": call.id,
                "error_type": type(last_error).__name__,
            },
        )
        await self._audit(
            context,
            definition,
            call,
            "failed",
            {"error_type": type(last_error).__name__},
        )
        if action_intent is not None:
            assert action_store is not None
            await action_store.transition_action_intent(
                intent_id=action_intent.id,
                tenant_id=action_intent.tenant_id,
                user_id=action_intent.user_id,
                expected=frozenset({ActionIntentStatus.EXECUTING}),
                status=ActionIntentStatus.UNCERTAIN,
                error_code=type(last_error).__name__,
            )
            raise ActionOutcomeUncertainError(
                f"Tool {definition.name} failed after side-effect execution began",
                action_intent_id=action_intent.id,
            ) from last_error
        if isinstance(last_error, ToolTimeoutError):
            raise last_error
        raise ToolExecutionError(f"Tool {definition.name} failed") from last_error

    def _action_intent(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: AgentContext,
        *,
        execution_scope: str,
        arguments_checksum: str,
        idempotency_identity: str,
    ) -> ActionIntentRecord | None:
        if definition.side_effect == "none" or self.action_intent_store is None:
            return None
        if not getattr(self.action_intent_store, "is_durable", False):
            raise ValueError("Action intent store must be durable for side-effecting execution")
        if context.tenant_id is None or context.user_id is None:
            raise ValueError("Durable actions require trusted tenant_id and user_id")
        stable_material = "\x1f".join(
            (
                context.tenant_id,
                context.user_id,
                execution_scope,
                context.conversation_id or context.session_id,
                definition.name,
                idempotency_identity,
                arguments_checksum,
            )
        )
        intent_id = f"intent_{sha256(stable_material.encode()).hexdigest()}"
        return ActionIntentRecord(
            id=intent_id,
            runtime_run_id=execution_scope,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            conversation_id=context.conversation_id,
            tool_name=definition.name,
            call_id=call.id,
            arguments_checksum=arguments_checksum,
            idempotency_key=idempotency_identity,
            side_effect=definition.side_effect,
        )

    async def _audit(
        self,
        context: AgentContext,
        definition: ToolDefinition,
        call: ToolCall,
        outcome: Literal["succeeded", "failed", "denied"],
        details: dict[str, object],
    ) -> None:
        if self.audit_store is None:
            return
        await self.audit_store.append_audit(
            AuditRecord(
                trace_id=context.trace_id or "trace_unavailable",
                session_id=context.session_id,
                action=f"tool:{definition.name}",
                outcome=outcome,
                details={
                    "call_id": call.id,
                    "side_effect": definition.side_effect,
                    "arguments": call.arguments,
                    **details,
                },
            )
        )


async def execute_tool(
    definition: ToolDefinition,
    call: ToolCall,
    context: AgentContext,
) -> ToolResult:
    """Compatibility helper for the default, non-approval execution path."""

    return await ToolExecutor().execute(
        definition,
        call,
        context,
        execution_scope="standalone",
    )


def _arguments_checksum(arguments: dict[str, Any]) -> str:
    encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode()).hexdigest()


def _scoped_idempotency_key(context: AgentContext, tool_name: str, key: str) -> str:
    if context.tenant_id is None and context.user_id is None:
        return f"{tool_name}:{key}"
    return f"{context.tenant_id or '-'}:{context.user_id or '-'}:{tool_name}:{key}"

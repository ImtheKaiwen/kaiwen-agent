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
    IdempotencyConflictError,
    PermissionDeniedError,
    ToolArgumentsError,
    ToolExecutionError,
    ToolTimeoutError,
    ToolValidationError,
)
from kaiwen_agent.policy import (
    PolicyContext,
    PolicyDecision,
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
    DurableActionScope,
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
        # Process-local single-flight for identical scoped operations. Cross-process
        # exclusion is the durable action-intent store's atomic claim.
        self._inflight: dict[str, tuple[str, asyncio.Future[ToolResult]]] = {}

    async def execute(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: AgentContext,
        *,
        execution_scope: str,
        emit: ToolEventCallback = _no_events,
        action_scope: DurableActionScope | None = None,
    ) -> ToolResult:
        # `execution_scope` is the per-invocation scope. When a host-verified durable
        # workflow scope is present it owns the action (cancellation and recovery look
        # actions up by durable run) and names the idempotency namespace: one task of one
        # run, so tasks that reuse a provider-generated call id never share an intent.
        owner_run_id = execution_scope
        if action_scope is not None:
            _check_action_scope(action_scope, context)
            owner_run_id = action_scope.runtime_run_id
            execution_scope = action_scope.execution_identity
            if call.idempotency_key is not None:
                call = call.model_copy(
                    update={"idempotency_key": f"{execution_scope}:{call.idempotency_key}"}
                )
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
            raise ToolArgumentsError(f"Invalid arguments for {definition.name}") from error

        policy_context = PolicyContext(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            agent_id=context.agent_id or "unknown-agent",
            session_id=context.session_id,
            conversation_id=context.conversation_id,
            run_id=owner_run_id,
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
            owner_run_id=owner_run_id,
            arguments_checksum=arguments_checksum,
            idempotency_identity=idempotency_identity,
        )
        flight_key = self._flight_key(
            context,
            definition,
            call,
            execution_scope=execution_scope,
            action_intent=action_intent,
        )

        def run() -> Awaitable[ToolResult]:
            return self._execute_guarded(
                definition,
                call,
                context,
                arguments,
                decision,
                execution_scope=execution_scope,
                owner_run_id=owner_run_id,
                emit=emit,
                arguments_checksum=arguments_checksum,
                idempotency_identity=idempotency_identity,
                action_intent=action_intent,
            )

        if flight_key is None:
            return await run()
        return await self._single_flight(
            flight_key,
            arguments_checksum,
            call,
            definition,
            emit,
            action_intent_id=action_intent.id if action_intent is not None else None,
            run=run,
        )

    def _flight_key(
        self,
        context: AgentContext,
        definition: ToolDefinition,
        call: ToolCall,
        *,
        execution_scope: str,
        action_intent: ActionIntentRecord | None,
    ) -> str | None:
        if action_intent is not None:
            return action_intent.id
        key = call.idempotency_key
        if definition.side_effect != "none" and key is None:
            key = f"{execution_scope}:{call.id}"
        return _scoped_idempotency_key(context, definition.name, key) if key is not None else None

    async def _single_flight(
        self,
        flight_key: str,
        arguments_checksum: str,
        call: ToolCall,
        definition: ToolDefinition,
        emit: ToolEventCallback,
        *,
        action_intent_id: str | None,
        run: Callable[[], Awaitable[ToolResult]],
    ) -> ToolResult:
        current = self._inflight.get(flight_key)
        if current is not None:
            in_flight_checksum, in_flight = current
            if in_flight_checksum != arguments_checksum:
                raise IdempotencyConflictError(
                    f"Idempotency key for {definition.name} is in use with different arguments"
                )
            result = await asyncio.shield(in_flight)
            await emit(
                "tool.completed",
                {"tool": definition.name, "call_id": call.id, "cached": True},
            )
            return result.model_copy(update={"call_id": call.id})
        future: asyncio.Future[ToolResult] = asyncio.get_running_loop().create_future()
        self._inflight[flight_key] = (arguments_checksum, future)
        try:
            result = await run()
        except BaseException as error:
            waiter_error: BaseException = error
            if isinstance(error, asyncio.CancelledError):
                waiter_error = (
                    ActionOutcomeUncertainError(
                        "The in-flight duplicate action was interrupted",
                        action_intent_id=action_intent_id,
                    )
                    if action_intent_id is not None
                    else ToolExecutionError("The in-flight duplicate operation was cancelled")
                )
            future.set_exception(waiter_error)
            future.exception()  # mark retrieved when no duplicate is waiting
            raise
        else:
            future.set_result(result)
            return result
        finally:
            self._inflight.pop(flight_key, None)

    async def _find_intent_with_other_id(
        self,
        store: ActionIntentStore,
        intent: ActionIntentRecord,
        definition: ToolDefinition,
    ) -> ActionIntentRecord | None:
        lookup = getattr(store, "get_action_intent_by_key", None)
        if lookup is None:
            return None  # the store's own key uniqueness still guards re-execution
        found: ActionIntentRecord | None = await lookup(
            tenant_id=intent.tenant_id,
            user_id=intent.user_id,
            runtime_run_id=intent.runtime_run_id,
            idempotency_key=intent.idempotency_key,
        )
        if found is None or found.id == intent.id:
            return None
        if (found.tool_name, found.conversation_id) != (intent.tool_name, intent.conversation_id):
            raise IdempotencyConflictError(
                f"Idempotency key for {definition.name} was already used with a different binding"
            )
        return found

    @staticmethod
    async def _prepare_intent(
        store: ActionIntentStore, intent: ActionIntentRecord, definition: ToolDefinition
    ) -> ActionIntentRecord:
        try:
            return await store.prepare_action_intent(intent)
        except ValueError as error:
            raise IdempotencyConflictError(
                f"Idempotency key for {definition.name} was already used with a different binding"
            ) from error

    async def _execute_guarded(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: AgentContext,
        arguments: Any,
        decision: PolicyDecision,
        *,
        execution_scope: str,
        owner_run_id: str,
        emit: ToolEventCallback,
        arguments_checksum: str,
        idempotency_identity: str,
        action_intent: ActionIntentRecord | None,
    ) -> ToolResult:
        action_store = self.action_intent_store
        if action_intent is not None:
            assert action_store is not None
            existing_intent = await action_store.get_action_intent(
                intent_id=action_intent.id,
                tenant_id=action_intent.tenant_id,
                user_id=action_intent.user_id,
            )
            if existing_intent is None:
                legacy = await self._find_intent_with_other_id(
                    action_store, action_intent, definition
                )
                if legacy is not None:
                    # Same operation persisted under a pre-M5 id: keep its state, receipt,
                    # and approvals instead of treating the action as new.
                    existing_intent = legacy
                    action_intent = action_intent.model_copy(
                        update={"id": legacy.id, "call_id": legacy.call_id}
                    )
            if (
                existing_intent is not None
                and existing_intent.arguments_checksum != arguments_checksum
            ):
                raise IdempotencyConflictError(
                    f"Idempotency key for {definition.name} was used with different arguments"
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
                await self._prepare_intent(action_store, action_intent, definition)

        if decision.decision in {
            PolicyDecisionType.REQUIRE_APPROVAL,
            PolicyDecisionType.REVIEW,
        }:
            request = ApprovalRequest(
                tool_name=definition.name,
                call_id=call.id,
                arguments=call.arguments,
                side_effect=definition.side_effect,
                execution_scope=owner_run_id,
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
                recorded = await self.idempotency_store.get(_checksum_key(idempotency_key))
                if recorded is not None and recorded.output != arguments_checksum:
                    raise IdempotencyConflictError(
                        f"Idempotency key for {definition.name} was used with different arguments"
                    )
                await self._audit(context, definition, call, "succeeded", {"cached": True})
                await emit(
                    "tool.completed",
                    {"tool": definition.name, "call_id": call.id, "cached": True},
                )
                return cached.model_copy(update={"call_id": call.id})

        if action_intent is not None:
            assert action_store is not None
            prepared = await self._prepare_intent(action_store, action_intent, definition)
            if prepared.arguments_checksum != arguments_checksum:
                raise IdempotencyConflictError(
                    f"Idempotency key for {definition.name} was used with different arguments"
                )
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
                    await self.idempotency_store.put(
                        _checksum_key(idempotency_key),
                        ToolResult(
                            call_id=call.id, name=definition.name, output=arguments_checksum
                        ),
                    )
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
            except asyncio.CancelledError:
                if action_intent is not None:
                    assert action_store is not None
                    # The handler may have produced an external effect before it was
                    # interrupted, so the outcome is unknown and must not be replayed.
                    await asyncio.shield(
                        action_store.transition_action_intent(
                            intent_id=action_intent.id,
                            tenant_id=action_intent.tenant_id,
                            user_id=action_intent.user_id,
                            expected=frozenset({ActionIntentStatus.EXECUTING}),
                            status=ActionIntentStatus.UNCERTAIN,
                            error_code="cancelled_in_flight",
                        )
                    )
                raise
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
        owner_run_id: str,
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
            )
        )
        intent_id = f"intent_{sha256(stable_material.encode()).hexdigest()}"
        return ActionIntentRecord(
            id=intent_id,
            runtime_run_id=owner_run_id,
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


def _check_action_scope(scope: DurableActionScope, context: AgentContext) -> None:
    """Reject a workflow scope that does not belong to the caller's trusted owner."""

    if (scope.tenant_id, scope.user_id, scope.session_id) != (
        context.tenant_id,
        context.user_id,
        context.session_id,
    ):
        raise ValueError("Durable action scope does not match the execution owner")
    claimed = context.metadata.get("runtime_run_id")
    if claimed is not None and claimed != scope.runtime_run_id:
        raise ValueError("Durable action scope does not match the context's workflow run")


def _arguments_checksum(arguments: dict[str, Any]) -> str:
    encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode()).hexdigest()


def _checksum_key(idempotency_key: str) -> str:
    return f"{idempotency_key}\x1fargs"


def _scoped_idempotency_key(context: AgentContext, tool_name: str, key: str) -> str:
    if context.tenant_id is None and context.user_id is None:
        return f"{tool_name}:{key}"
    return f"{context.tenant_id or '-'}:{context.user_id or '-'}:{tool_name}:{key}"

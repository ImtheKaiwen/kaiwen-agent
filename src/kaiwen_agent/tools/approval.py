from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal, Protocol, cast

from kaiwen_agent.context import AgentContext
from kaiwen_agent.exceptions import DurableApprovalRequiredError
from kaiwen_agent.tools.durability import (
    ActionIntentStatus,
    ActionIntentStore,
    ApprovalStatus,
    DurableApprovalRecord,
    DurableApprovalStore,
)


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    tool_name: str
    call_id: str
    arguments: dict[str, Any]
    side_effect: Literal["none", "reversible", "destructive"]
    execution_scope: str | None = None
    action_intent_id: str | None = None
    arguments_checksum: str | None = None


class ApprovalGate(Protocol):
    async def approve(self, request: ApprovalRequest, context: AgentContext) -> bool: ...


class StaticApprovalGate:
    """Explicit approval gate for tests, CLIs, and trusted application adapters."""

    def __init__(self, approved: bool) -> None:
        self.approved = approved

    async def approve(self, request: ApprovalRequest, context: AgentContext) -> bool:
        del request, context
        return self.approved


class DurableApprovalGate:
    """Restart-safe, one-time approval gate bound to a stable action intent."""

    def __init__(
        self,
        store: DurableApprovalStore,
        action_store: ActionIntentStore | None = None,
    ) -> None:
        if not getattr(store, "is_durable", False):
            raise ValueError("DurableApprovalGate requires a durable approval store")
        self.store = store
        if action_store is None and hasattr(store, "get_action_intent"):
            action_store = cast(ActionIntentStore, store)
        self.action_store = action_store

    async def approve(self, request: ApprovalRequest, context: AgentContext) -> bool:
        tenant_id, user_id = _required_owner(context)
        if request.execution_scope is None or request.action_intent_id is None:
            raise ValueError("Durable approval requires execution_scope and action_intent_id")
        checksum = request.arguments_checksum or _arguments_checksum(request.arguments)
        existing = await self.store.get_approval_for_intent(
            action_intent_id=request.action_intent_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        if existing is None:
            existing = await self.store.create_pending_approval(
                DurableApprovalRecord(
                    action_intent_id=request.action_intent_id,
                    runtime_run_id=request.execution_scope,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    conversation_id=context.conversation_id,
                    tool_name=request.tool_name,
                    call_id=request.call_id,
                    arguments_checksum=checksum,
                )
            )
        _validate_binding(existing, request, context, checksum)
        if existing.status == ApprovalStatus.PENDING:
            raise DurableApprovalRequiredError(
                f"Tool {request.tool_name} requires approval",
                approval_id=existing.id,
            )
        if existing.status == ApprovalStatus.DENIED:
            return False
        if existing.status == ApprovalStatus.CONSUMED:
            if self.action_store is not None:
                intent = await self.action_store.get_action_intent(
                    intent_id=existing.action_intent_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                )
                if intent is not None and intent.status == ActionIntentStatus.PREPARED:
                    return True
            raise DurableApprovalRequiredError(
                "Approval was already consumed",
                approval_id=existing.id,
            )
        consumed = await self.store.consume_approval(
            approval_id=existing.id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        if consumed is None:
            raise DurableApprovalRequiredError(
                "Approval is no longer available",
                approval_id=existing.id,
            )
        return True

    async def resolve(
        self,
        *,
        approval_id: str,
        tenant_id: str,
        user_id: str,
        conversation_id: str | None,
        action_intent_id: str,
        tool_name: str,
        call_id: str,
        decided_by: str,
        approved: bool,
    ) -> DurableApprovalRecord:
        resolved = await self.store.resolve_approval(
            approval_id=approval_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            action_intent_id=action_intent_id,
            tool_name=tool_name,
            call_id=call_id,
            decided_by=decided_by,
            approved=approved,
        )
        if resolved is None:
            raise KeyError(
                "Approval was not found, was already decided, or belongs to another owner"
            )
        return resolved


def _required_owner(context: AgentContext) -> tuple[str, str]:
    if context.tenant_id is None or context.user_id is None:
        raise ValueError("Durable approval requires trusted tenant_id and user_id")
    return context.tenant_id, context.user_id


def _arguments_checksum(arguments: dict[str, Any]) -> str:
    encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode()).hexdigest()


def _validate_binding(
    approval: DurableApprovalRecord,
    request: ApprovalRequest,
    context: AgentContext,
    checksum: str,
) -> None:
    expected = (
        request.execution_scope,
        context.conversation_id,
        request.tool_name,
        request.call_id,
        checksum,
    )
    actual = (
        approval.runtime_run_id,
        approval.conversation_id,
        approval.tool_name,
        approval.call_id,
        approval.arguments_checksum,
    )
    if actual != expected:
        raise ValueError("Approval binding does not match the requested action")

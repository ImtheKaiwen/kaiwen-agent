from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.coordination.fencing import OwnerEpoch
from kaiwen_agent.types import ToolResult, utc_now


class ActionIntentStatus(StrEnum):
    PREPARED = "prepared"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    CONSUMED = "consumed"


_SCOPE_FIELD_LIMIT = 200


@dataclass(frozen=True, slots=True)
class DurableActionScope:
    """Which durable workflow run and task a tool action belongs to.

    Action intents and approvals are owned by `runtime_run_id`, the durable workflow run, so
    run-scoped cancellation and recovery can find them. `task_id` separates the idempotency
    identity of tasks that share the run. Only a host that has verified an active run
    (`workflow.trusted_action_scope`) builds one; it never comes from model output, tool
    arguments, or context metadata, and the executor re-checks it against the call's owner.
    """

    runtime_run_id: str
    task_id: str
    tenant_id: str
    user_id: str
    session_id: str
    # Host-owned authority of the executor that runs this task under fenced execution (ADR 0016).
    # Set only by `workflow.trusted_action_scope` from the epoch the controller bound; never read
    # from model output, tool arguments or context metadata, and excluded from `repr`.
    owner_epoch: OwnerEpoch | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        epoch = self.owner_epoch
        if epoch is not None and (
            (epoch.run_id, epoch.tenant_id, epoch.user_id, epoch.session_id)
            != (self.runtime_run_id, self.tenant_id, self.user_id, self.session_id)
        ):
            raise ValueError("Invalid durable action scope field: owner_epoch")
        for name in ("runtime_run_id", "task_id", "tenant_id", "user_id", "session_id"):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > _SCOPE_FIELD_LIMIT
                or "\x1f" in value
            ):
                raise ValueError(f"Invalid durable action scope field: {name}")

    @property
    def execution_identity(self) -> str:
        """Stable identity of the action namespace: one task of one durable run."""

        return f"{self.runtime_run_id}:{self.task_id}"


class ActionIntentRecord(BaseModel):
    """Stable record written before a side-effecting tool handler starts."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    runtime_run_id: str = Field(min_length=1, max_length=200)
    tenant_id: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1, max_length=200)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=200)
    tool_name: str = Field(min_length=1, max_length=300)
    call_id: str = Field(min_length=1, max_length=300)
    arguments_checksum: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=500)
    side_effect: str = Field(pattern="^(reversible|destructive)$")
    status: ActionIntentStatus = ActionIntentStatus.PREPARED
    result: ToolResult | None = None
    error_code: str | None = Field(default=None, max_length=300)
    external_receipt: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class DurableApprovalRecord(BaseModel):
    """One-time decision bound to an exact owner, run, tool call, and action intent."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"approval_{uuid4().hex}")
    action_intent_id: str = Field(min_length=1, max_length=200)
    runtime_run_id: str = Field(min_length=1, max_length=200)
    tenant_id: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1, max_length=200)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=200)
    tool_name: str = Field(min_length=1, max_length=300)
    call_id: str = Field(min_length=1, max_length=300)
    arguments_checksum: str = Field(min_length=64, max_length=64)
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_by: str | None = Field(default=None, min_length=1, max_length=200)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    decided_at: datetime | None = None
    consumed_at: datetime | None = None


class ActionIntentStore(Protocol):
    is_durable: bool

    async def prepare_action_intent(self, intent: ActionIntentRecord) -> ActionIntentRecord: ...

    async def get_action_intent(
        self, *, intent_id: str, tenant_id: str, user_id: str
    ) -> ActionIntentRecord | None: ...

    async def transition_action_intent(
        self,
        *,
        intent_id: str,
        tenant_id: str,
        user_id: str,
        expected: frozenset[ActionIntentStatus],
        status: ActionIntentStatus,
        result: ToolResult | None = None,
        error_code: str | None = None,
        external_receipt: dict[str, Any] | None = None,
    ) -> ActionIntentRecord | None: ...

    async def list_action_intents(
        self, *, statuses: frozenset[ActionIntentStatus], limit: int = 100
    ) -> list[ActionIntentRecord]: ...


class FencedActionIntents:
    """An `ActionIntentStore` view whose every write carries one executor's `OwnerEpoch`.

    The platform tool executor uses it inside a fenced scope, so preparing an intent, claiming it
    (PREPARED -> EXECUTING) and recording its outcome are each checked against the authoritative
    lease in the store's own transaction. There is no unfenced fallback: a store that does not
    declare the fenced execution contract is refused at construction.
    """

    is_durable = True

    def __init__(self, store: Any, epoch: OwnerEpoch) -> None:
        if getattr(store, "fenced_execution_contract", None) != "kaiwen.fenced-execution.v1":
            raise ValueError("The action intent store cannot enforce fenced execution")
        self._store = store
        self._epoch = epoch

    async def prepare_action_intent(self, intent: ActionIntentRecord) -> ActionIntentRecord:
        stored: ActionIntentRecord = await self._store.prepare_action_intent(
            intent, owner_epoch=self._epoch
        )
        return stored

    async def get_action_intent(
        self, *, intent_id: str, tenant_id: str, user_id: str
    ) -> ActionIntentRecord | None:
        found: ActionIntentRecord | None = await self._store.get_action_intent(
            intent_id=intent_id, tenant_id=tenant_id, user_id=user_id
        )
        return found

    async def transition_action_intent(
        self,
        *,
        intent_id: str,
        tenant_id: str,
        user_id: str,
        expected: frozenset[ActionIntentStatus],
        status: ActionIntentStatus,
        result: ToolResult | None = None,
        error_code: str | None = None,
        external_receipt: dict[str, Any] | None = None,
    ) -> ActionIntentRecord | None:
        updated: ActionIntentRecord | None = await self._store.transition_action_intent(
            intent_id=intent_id,
            tenant_id=tenant_id,
            user_id=user_id,
            expected=expected,
            status=status,
            result=result,
            error_code=error_code,
            external_receipt=external_receipt,
            owner_epoch=self._epoch,
        )
        return updated

    async def list_action_intents(
        self, *, statuses: frozenset[ActionIntentStatus], limit: int = 100
    ) -> list[ActionIntentRecord]:
        listed: list[ActionIntentRecord] = await self._store.list_action_intents(
            statuses=statuses, limit=limit
        )
        return listed

    async def get_action_intent_by_key(
        self, *, tenant_id: str, user_id: str, runtime_run_id: str, idempotency_key: str
    ) -> ActionIntentRecord | None:
        found: ActionIntentRecord | None = await self._store.get_action_intent_by_key(
            tenant_id=tenant_id,
            user_id=user_id,
            runtime_run_id=runtime_run_id,
            idempotency_key=idempotency_key,
        )
        return found


class ActionIntentRunQuery(Protocol):
    """Optional capability: a complete, owner- and run-scoped listing of intents.

    Stores without it are queried through the global paged listing, which cannot prove
    that no action exists once a page is full, so callers must then fail closed.
    """

    async def list_run_action_intents(
        self,
        *,
        tenant_id: str,
        user_id: str,
        runtime_run_id: str,
        statuses: frozenset[ActionIntentStatus],
    ) -> list[ActionIntentRecord]: ...


class ActionIntentKeyLookup(Protocol):
    """Optional capability that lets the executor adopt intents with a different id.

    Intents written before the arguments checksum left the intent id are stored under a
    different id than the same operation gets now. A store that offers this lookup lets
    the executor find them by their owner, run, and idempotency key, keep their state,
    receipts, and approvals, and never execute their side effect a second time.
    """

    async def get_action_intent_by_key(
        self,
        *,
        tenant_id: str,
        user_id: str,
        runtime_run_id: str,
        idempotency_key: str,
    ) -> ActionIntentRecord | None: ...


class DurableApprovalStore(Protocol):
    is_durable: bool

    async def create_pending_approval(
        self, approval: DurableApprovalRecord
    ) -> DurableApprovalRecord: ...

    async def get_approval(
        self, *, approval_id: str, tenant_id: str, user_id: str
    ) -> DurableApprovalRecord | None: ...

    async def get_approval_for_intent(
        self, *, action_intent_id: str, tenant_id: str, user_id: str
    ) -> DurableApprovalRecord | None: ...

    async def resolve_approval(
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
    ) -> DurableApprovalRecord | None: ...

    async def consume_approval(
        self,
        *,
        approval_id: str,
        tenant_id: str,
        user_id: str,
    ) -> DurableApprovalRecord | None: ...

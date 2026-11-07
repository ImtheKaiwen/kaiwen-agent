from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.coordination import LeaseStore
from kaiwen_agent.runtime.models import AutonomyLevel, RunMode
from kaiwen_agent.runtime.profile import RuntimeVersionPins
from kaiwen_agent.tools.durability import (
    ActionIntentStatus,
    ActionIntentStore,
    DurableApprovalStore,
)
from kaiwen_agent.types import utc_now


class RuntimeRunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RuntimeRunRecord(BaseModel):
    """Owner-scoped durable workflow state, separate from the legacy AgentRun."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"runtime_{uuid4().hex}")
    tenant_id: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=200)
    mode: Literal[RunMode.WORKFLOW] = RunMode.WORKFLOW
    autonomy: AutonomyLevel
    version_pins: RuntimeVersionPins
    status: RuntimeRunStatus = RuntimeRunStatus.CREATED
    safe_boundary: str | None = Field(default=None, max_length=500)
    blocked_reason: str | None = Field(default=None, max_length=1_000)
    # Lease key held by the executing controller; recovery only judges runs that have one.
    owner_lease_key: str | None = Field(default=None, min_length=1, max_length=300)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None


class RuntimeRunStore(Protocol):
    is_durable: bool

    async def save_runtime_run(self, run: RuntimeRunRecord) -> None: ...

    async def get_runtime_run(
        self, *, run_id: str, tenant_id: str, user_id: str
    ) -> RuntimeRunRecord | None: ...

    async def list_runtime_runs(
        self, *, statuses: frozenset[RuntimeRunStatus], limit: int = 100
    ) -> list[RuntimeRunRecord]: ...


@dataclass(frozen=True, slots=True)
class DurableRuntimeStores:
    runs: RuntimeRunStore
    actions: ActionIntentStore
    approvals: DurableApprovalStore

    def validate(self) -> None:
        stores = (self.runs, self.actions, self.approvals)
        if not all(getattr(store, "is_durable", False) for store in stores):
            raise ValueError(
                "Durable workflow mode requires durable run, action, and approval stores"
            )


class RuntimeRecoveryError(RuntimeError):
    pass


class RecoveryReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    uncertain_action_ids: tuple[str, ...] = ()
    blocked_run_ids: tuple[str, ...] = ()
    owner_lost_run_ids: tuple[str, ...] = ()


class RuntimeRecoveryManager:
    """Reconciles unsafe in-flight state under an exclusive startup lease."""

    def __init__(
        self,
        *,
        stores: DurableRuntimeStores,
        leases: LeaseStore,
        lease_key: str = "runtime:recovery",
        lease_ttl_seconds: float = 60.0,
    ) -> None:
        stores.validate()
        self.stores = stores
        self.leases = leases
        self.lease_key = lease_key
        self.lease_ttl_seconds = lease_ttl_seconds

    async def reconcile(self, *, owner_id: str, limit: int = 100) -> RecoveryReport:
        lease = await self.leases.acquire(
            key=self.lease_key,
            owner_id=owner_id,
            ttl_seconds=self.lease_ttl_seconds,
        )
        if lease is None:
            raise RuntimeRecoveryError("Runtime recovery lease is already held")
        uncertain: list[str] = []
        blocked: set[str] = set()
        try:
            intents = await self.stores.actions.list_action_intents(
                statuses=frozenset({ActionIntentStatus.EXECUTING}),
                limit=limit,
            )
            for intent in intents:
                updated = await self.stores.actions.transition_action_intent(
                    intent_id=intent.id,
                    tenant_id=intent.tenant_id,
                    user_id=intent.user_id,
                    expected=frozenset({ActionIntentStatus.EXECUTING}),
                    status=ActionIntentStatus.UNCERTAIN,
                    error_code="process_interrupted",
                )
                if updated is None:
                    continue
                uncertain.append(intent.id)
                run = await self.stores.runs.get_runtime_run(
                    run_id=intent.runtime_run_id,
                    tenant_id=intent.tenant_id,
                    user_id=intent.user_id,
                )
                if run is not None and run.status in {
                    RuntimeRunStatus.CREATED,
                    RuntimeRunStatus.RUNNING,
                    RuntimeRunStatus.PAUSED,
                }:
                    await self.stores.runs.save_runtime_run(
                        run.model_copy(
                            update={
                                "status": RuntimeRunStatus.BLOCKED,
                                "blocked_reason": f"uncertain_action:{intent.id}",
                                "updated_at": utc_now(),
                            }
                        )
                    )
                    blocked.add(run.id)
            owner_lost = await self._block_unowned_runs(owner_id=owner_id, limit=limit)
            return RecoveryReport(
                uncertain_action_ids=tuple(uncertain),
                blocked_run_ids=tuple(sorted(blocked)),
                owner_lost_run_ids=tuple(owner_lost),
            )
        finally:
            await self.leases.release(lease)

    async def _block_unowned_runs(self, *, owner_id: str, limit: int) -> list[str]:
        """Block RUNNING runs whose ownership lease nobody holds; never resume them.

        Age is not proof of process death, so only runs that recorded a lease key are
        judged, and only when their lease can be acquired, which proves no live executor
        is heartbeating it. The probe lease is held while the run is blocked so an
        executor cannot start concurrently.
        """

        owner_lost: list[str] = []
        runs = await self.stores.runs.list_runtime_runs(
            statuses=frozenset({RuntimeRunStatus.RUNNING}), limit=limit
        )
        for run in runs:
            if run.owner_lease_key is None:
                continue
            probe = await self.leases.acquire(
                key=run.owner_lease_key,
                owner_id=owner_id,
                ttl_seconds=self.lease_ttl_seconds,
            )
            if probe is None:
                continue
            try:
                await self.stores.runs.save_runtime_run(
                    run.model_copy(
                        update={
                            "status": RuntimeRunStatus.BLOCKED,
                            "blocked_reason": "owner_lost",
                            "updated_at": utc_now(),
                        }
                    )
                )
                owner_lost.append(run.id)
            except ValueError:
                pass  # the run changed state concurrently; leave it to its owner
            finally:
                await self.leases.release(probe)
        return owner_lost

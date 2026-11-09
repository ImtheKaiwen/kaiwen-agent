from __future__ import annotations

import inspect

from kaiwen_agent.coordination.fencing import OwnerEpoch
from kaiwen_agent.exceptions import FencedConfigurationError, RunOwnershipError
from kaiwen_agent.runtime.durability import (
    RuntimeRunRecord,
    RuntimeRunStatus,
    RuntimeRunStore,
    WorkspaceBinding,
)
from kaiwen_agent.runtime.models import AutonomyLevel, RuntimeIdentity
from kaiwen_agent.runtime.profile import RuntimeVersionPins
from kaiwen_agent.types import utc_now


class DurableRunManager:
    """Lifecycle facade for durable workflow records; it does not execute tasks."""

    def __init__(self, store: RuntimeRunStore) -> None:
        if not getattr(store, "is_durable", False):
            raise ValueError("DurableRunManager requires a durable runtime run store")
        self.store = store
        self._conditional_writes = (
            "expected_status" in inspect.signature(store.save_runtime_run).parameters
        )
        self._fenced_writes = (
            getattr(store, "fenced_execution_contract", None) == "kaiwen.fenced-execution.v1"
            and "owner_epoch" in inspect.signature(store.save_runtime_run).parameters
        )

    @property
    def supports_fenced_execution(self) -> bool:
        """Whether the store declares and implements fenced (epoch-checked) run writes."""

        return self._fenced_writes

    async def create(
        self,
        *,
        identity: RuntimeIdentity,
        autonomy: AutonomyLevel,
        version_pins: RuntimeVersionPins,
        workspace_binding: WorkspaceBinding | None = None,
    ) -> RuntimeRunRecord:
        if identity.tenant_id is None or identity.user_id is None:
            raise ValueError("Durable workflow runs require trusted tenant_id and user_id")
        run = RuntimeRunRecord(
            workspace_binding=workspace_binding,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            session_id=identity.session_id,
            conversation_id=identity.conversation_id,
            autonomy=autonomy,
            version_pins=version_pins,
        )
        await self.store.save_runtime_run(run)
        return run

    async def bind_workspace(
        self, *, run_id: str, identity: RuntimeIdentity, binding: WorkspaceBinding
    ) -> RuntimeRunRecord:
        """Bind a not-yet-started run to its authorized workspace. Idempotent, write-once."""

        current = await self.get(run_id=run_id, identity=identity)
        if current is None:
            raise KeyError("Runtime run was not found for this owner")
        if current.workspace_binding == binding:
            return current
        if current.workspace_binding is not None:
            raise ValueError("The run is already bound to a different workspace")
        if current.status is not RuntimeRunStatus.CREATED:
            raise ValueError("A run can only be bound to a workspace before it starts")
        updated = current.model_copy(update={"workspace_binding": binding, "updated_at": utc_now()})
        await self._save(
            updated, expected_status=RuntimeRunStatus.CREATED, expected_owner_fence=None
        )
        return updated

    async def get(self, *, run_id: str, identity: RuntimeIdentity) -> RuntimeRunRecord | None:
        tenant_id, user_id = _required_owner(identity)
        return await self.store.get_runtime_run(
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )

    async def start(
        self,
        *,
        run_id: str,
        identity: RuntimeIdentity,
        owner_lease_key: str | None = None,
        owner_fence: int | None = None,
        owner_epoch: OwnerEpoch | None = None,
    ) -> RuntimeRunRecord:
        return await self._transition(
            run_id=run_id,
            identity=identity,
            expected=frozenset(
                {
                    RuntimeRunStatus.CREATED,
                    RuntimeRunStatus.PAUSED,
                    RuntimeRunStatus.BLOCKED,
                }
            ),
            status=RuntimeRunStatus.RUNNING,
            owner_lease_key=owner_lease_key,
            owner_fence=owner_fence,
            owner_epoch=owner_epoch,
            take_ownership=(
                owner_lease_key is not None or owner_fence is not None or owner_epoch is not None
            ),
        )

    async def pause(
        self,
        *,
        run_id: str,
        identity: RuntimeIdentity,
        safe_boundary: str,
        owner_fence: int | None = None,
        owner_epoch: OwnerEpoch | None = None,
    ) -> RuntimeRunRecord:
        return await self._transition(
            run_id=run_id,
            identity=identity,
            expected=frozenset({RuntimeRunStatus.RUNNING}),
            status=RuntimeRunStatus.PAUSED,
            safe_boundary=safe_boundary,
            owner_fence=owner_fence,
            owner_epoch=owner_epoch,
        )

    async def block(
        self,
        *,
        run_id: str,
        identity: RuntimeIdentity,
        reason: str,
        owner_fence: int | None = None,
        owner_epoch: OwnerEpoch | None = None,
    ) -> RuntimeRunRecord:
        return await self._transition(
            run_id=run_id,
            identity=identity,
            expected=frozenset(
                {
                    RuntimeRunStatus.CREATED,
                    RuntimeRunStatus.RUNNING,
                    RuntimeRunStatus.PAUSED,
                }
            ),
            status=RuntimeRunStatus.BLOCKED,
            blocked_reason=reason,
            owner_fence=owner_fence,
            owner_epoch=owner_epoch,
        )

    async def cancel(
        self,
        *,
        run_id: str,
        identity: RuntimeIdentity,
        owner_fence: int | None = None,
        owner_epoch: OwnerEpoch | None = None,
    ) -> RuntimeRunRecord:
        return await self._transition(
            run_id=run_id,
            identity=identity,
            expected=frozenset(
                {
                    RuntimeRunStatus.CREATED,
                    RuntimeRunStatus.RUNNING,
                    RuntimeRunStatus.PAUSED,
                    RuntimeRunStatus.BLOCKED,
                }
            ),
            status=RuntimeRunStatus.CANCELLED,
            terminal=True,
            owner_fence=owner_fence,
            owner_epoch=owner_epoch,
        )

    async def complete(
        self,
        *,
        run_id: str,
        identity: RuntimeIdentity,
        owner_fence: int | None = None,
        owner_epoch: OwnerEpoch | None = None,
    ) -> RuntimeRunRecord:
        return await self._transition(
            run_id=run_id,
            identity=identity,
            expected=frozenset({RuntimeRunStatus.RUNNING}),
            status=RuntimeRunStatus.COMPLETED,
            terminal=True,
            owner_fence=owner_fence,
            owner_epoch=owner_epoch,
        )

    async def fail(
        self,
        *,
        run_id: str,
        identity: RuntimeIdentity,
        reason: str,
        owner_fence: int | None = None,
        owner_epoch: OwnerEpoch | None = None,
    ) -> RuntimeRunRecord:
        return await self._transition(
            run_id=run_id,
            identity=identity,
            expected=frozenset(
                {
                    RuntimeRunStatus.CREATED,
                    RuntimeRunStatus.RUNNING,
                    RuntimeRunStatus.PAUSED,
                    RuntimeRunStatus.BLOCKED,
                }
            ),
            status=RuntimeRunStatus.FAILED,
            blocked_reason=reason,
            terminal=True,
            owner_fence=owner_fence,
            owner_epoch=owner_epoch,
        )

    async def _transition(
        self,
        *,
        run_id: str,
        identity: RuntimeIdentity,
        expected: frozenset[RuntimeRunStatus],
        status: RuntimeRunStatus,
        safe_boundary: str | None = None,
        blocked_reason: str | None = None,
        terminal: bool = False,
        owner_lease_key: str | None = None,
        owner_fence: int | None = None,
        owner_epoch: OwnerEpoch | None = None,
        take_ownership: bool = False,
    ) -> RuntimeRunRecord:
        if owner_epoch is not None:
            if not self._fenced_writes:
                raise FencedConfigurationError(
                    "The runtime run store cannot enforce fenced run transitions"
                )
            # The epoch is the only authority in fenced mode; the publication below is derived
            # from it, never from caller-supplied lease key or token arguments.
            owner_lease_key, owner_fence = owner_epoch.lease_key, owner_epoch.epoch
        current = await self.get(run_id=run_id, identity=identity)
        if current is None:
            raise KeyError("Runtime run was not found for this owner")
        if current.status not in expected:
            raise ValueError(f"Runtime run cannot transition from {current.status} to {status}")
        now = utc_now()
        updated = current.model_copy(
            update={
                "status": status,
                "safe_boundary": safe_boundary,
                "blocked_reason": blocked_reason,
                "updated_at": now,
                "completed_at": now if terminal else None,
                **(
                    {
                        "owner_lease_key": owner_lease_key,
                        "owner_fencing_token": owner_fence,
                        **({"fenced": True} if owner_epoch is not None else {}),
                    }
                    if take_ownership
                    else {}
                ),
            }
        )
        await self._save(
            updated,
            expected_status=current.status,
            expected_owner_fence=None if take_ownership else owner_fence,
            owner_epoch=owner_epoch,
        )
        return updated

    async def _save(
        self,
        updated: RuntimeRunRecord,
        *,
        expected_status: RuntimeRunStatus,
        expected_owner_fence: int | None,
        owner_epoch: OwnerEpoch | None = None,
    ) -> None:
        if owner_epoch is not None:
            # No fallback: a fenced transition is either epoch-checked by the store or refused.
            await self.store.save_runtime_run(  # type: ignore[call-arg]
                updated,
                expected_status=expected_status,
                expected_owner_fence=None,
                owner_epoch=owner_epoch,
            )
            return
        if self._conditional_writes:
            await self.store.save_runtime_run(
                updated,
                expected_status=expected_status,
                expected_owner_fence=expected_owner_fence,
            )
            return
        if expected_owner_fence is not None:
            raise RunOwnershipError(
                "The runtime run store cannot enforce ownership-conditional writes"
            )
        await self.store.save_runtime_run(updated)


def _required_owner(identity: RuntimeIdentity) -> tuple[str, str]:
    if identity.tenant_id is None or identity.user_id is None:
        raise ValueError("Durable workflow runs require trusted tenant_id and user_id")
    return identity.tenant_id, identity.user_id

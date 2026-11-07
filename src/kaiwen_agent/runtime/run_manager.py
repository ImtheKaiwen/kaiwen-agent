from __future__ import annotations

import inspect

from kaiwen_agent.exceptions import RunOwnershipError
from kaiwen_agent.runtime.durability import (
    RuntimeRunRecord,
    RuntimeRunStatus,
    RuntimeRunStore,
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

    async def create(
        self,
        *,
        identity: RuntimeIdentity,
        autonomy: AutonomyLevel,
        version_pins: RuntimeVersionPins,
    ) -> RuntimeRunRecord:
        if identity.tenant_id is None or identity.user_id is None:
            raise ValueError("Durable workflow runs require trusted tenant_id and user_id")
        run = RuntimeRunRecord(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            session_id=identity.session_id,
            conversation_id=identity.conversation_id,
            autonomy=autonomy,
            version_pins=version_pins,
        )
        await self.store.save_runtime_run(run)
        return run

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
            take_ownership=owner_lease_key is not None or owner_fence is not None,
        )

    async def pause(
        self,
        *,
        run_id: str,
        identity: RuntimeIdentity,
        safe_boundary: str,
        owner_fence: int | None = None,
    ) -> RuntimeRunRecord:
        return await self._transition(
            run_id=run_id,
            identity=identity,
            expected=frozenset({RuntimeRunStatus.RUNNING}),
            status=RuntimeRunStatus.PAUSED,
            safe_boundary=safe_boundary,
            owner_fence=owner_fence,
        )

    async def block(
        self,
        *,
        run_id: str,
        identity: RuntimeIdentity,
        reason: str,
        owner_fence: int | None = None,
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
        )

    async def cancel(
        self, *, run_id: str, identity: RuntimeIdentity, owner_fence: int | None = None
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
        )

    async def complete(
        self, *, run_id: str, identity: RuntimeIdentity, owner_fence: int | None = None
    ) -> RuntimeRunRecord:
        return await self._transition(
            run_id=run_id,
            identity=identity,
            expected=frozenset({RuntimeRunStatus.RUNNING}),
            status=RuntimeRunStatus.COMPLETED,
            terminal=True,
            owner_fence=owner_fence,
        )

    async def fail(
        self,
        *,
        run_id: str,
        identity: RuntimeIdentity,
        reason: str,
        owner_fence: int | None = None,
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
        take_ownership: bool = False,
    ) -> RuntimeRunRecord:
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
                    {"owner_lease_key": owner_lease_key, "owner_fencing_token": owner_fence}
                    if take_ownership
                    else {}
                ),
            }
        )
        await self._save(
            updated,
            expected_status=current.status,
            expected_owner_fence=None if take_ownership else owner_fence,
        )
        return updated

    async def _save(
        self,
        updated: RuntimeRunRecord,
        *,
        expected_status: RuntimeRunStatus,
        expected_owner_fence: int | None,
    ) -> None:
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

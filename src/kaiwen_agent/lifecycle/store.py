from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Protocol

from kaiwen_agent.lifecycle.models import (
    LifecycleCycle,
    LifecycleRuntimeRecord,
    LifecycleWakeup,
    WakeupStatus,
)
from kaiwen_agent.types import utc_now


class LifecycleStore(Protocol):
    async def save_runtime(self, runtime: LifecycleRuntimeRecord) -> None: ...

    async def get_runtime(self, agent_id: str) -> LifecycleRuntimeRecord | None: ...

    async def save_cycle(self, cycle: LifecycleCycle) -> None: ...

    async def get_cycle(self, cycle_id: str) -> LifecycleCycle | None: ...

    async def list_cycles(self, *, agent_id: str, limit: int = 100) -> list[LifecycleCycle]: ...

    async def save_wakeup(self, wakeup: LifecycleWakeup) -> None: ...

    async def schedule_wakeup(self, wakeup: LifecycleWakeup) -> list[LifecycleWakeup]: ...

    async def get_wakeup(self, wakeup_id: str) -> LifecycleWakeup | None: ...

    async def claim_next_wakeup(
        self, *, agent_id: str, due_before: datetime
    ) -> LifecycleWakeup | None: ...

    async def claim_wakeup(
        self, *, wakeup_id: str, claimed_at: datetime
    ) -> LifecycleWakeup | None: ...

    async def cancel_scheduled_wakeups(
        self, *, agent_id: str, replace_key: str
    ) -> list[LifecycleWakeup]: ...

    async def list_wakeups(
        self,
        *,
        agent_id: str,
        status: WakeupStatus | None = None,
        due_before: datetime | None = None,
        limit: int = 100,
    ) -> list[LifecycleWakeup]: ...


class InMemoryLifecycleStore:
    def __init__(self) -> None:
        self._runtimes: dict[str, LifecycleRuntimeRecord] = {}
        self._cycles: dict[str, LifecycleCycle] = {}
        self._wakeups: dict[str, LifecycleWakeup] = {}
        self._lock = asyncio.Lock()

    async def save_runtime(self, runtime: LifecycleRuntimeRecord) -> None:
        async with self._lock:
            self._runtimes[runtime.agent_id] = runtime.model_copy(deep=True)

    async def get_runtime(self, agent_id: str) -> LifecycleRuntimeRecord | None:
        async with self._lock:
            runtime = self._runtimes.get(agent_id)
            return runtime.model_copy(deep=True) if runtime else None

    async def save_cycle(self, cycle: LifecycleCycle) -> None:
        async with self._lock:
            self._cycles[cycle.id] = cycle.model_copy(deep=True)

    async def get_cycle(self, cycle_id: str) -> LifecycleCycle | None:
        async with self._lock:
            cycle = self._cycles.get(cycle_id)
            return cycle.model_copy(deep=True) if cycle else None

    async def list_cycles(self, *, agent_id: str, limit: int = 100) -> list[LifecycleCycle]:
        _validate_limit(limit)
        async with self._lock:
            cycles = [cycle for cycle in self._cycles.values() if cycle.agent_id == agent_id]
            cycles.sort(key=lambda cycle: cycle.woke_at, reverse=True)
            return [cycle.model_copy(deep=True) for cycle in cycles[:limit]]

    async def save_wakeup(self, wakeup: LifecycleWakeup) -> None:
        async with self._lock:
            self._wakeups[wakeup.id] = wakeup.model_copy(deep=True)

    async def schedule_wakeup(self, wakeup: LifecycleWakeup) -> list[LifecycleWakeup]:
        async with self._lock:
            cancelled: list[LifecycleWakeup] = []
            if wakeup.replace_key is not None:
                for existing in self._wakeups.values():
                    if (
                        existing.agent_id == wakeup.agent_id
                        and existing.replace_key == wakeup.replace_key
                        and existing.status == WakeupStatus.SCHEDULED
                    ):
                        existing.status = WakeupStatus.CANCELLED
                        existing.updated_at = utc_now()
                        cancelled.append(existing.model_copy(deep=True))
            self._wakeups[wakeup.id] = wakeup.model_copy(deep=True)
            return cancelled

    async def get_wakeup(self, wakeup_id: str) -> LifecycleWakeup | None:
        async with self._lock:
            wakeup = self._wakeups.get(wakeup_id)
            return wakeup.model_copy(deep=True) if wakeup else None

    async def claim_next_wakeup(
        self, *, agent_id: str, due_before: datetime
    ) -> LifecycleWakeup | None:
        async with self._lock:
            due = [
                wakeup
                for wakeup in self._wakeups.values()
                if wakeup.agent_id == agent_id
                and wakeup.status == WakeupStatus.SCHEDULED
                and wakeup.scheduled_at <= due_before
            ]
            if not due:
                return None
            due.sort(key=lambda wakeup: (-wakeup.priority, wakeup.scheduled_at))
            wakeup = due[0]
            wakeup.status = WakeupStatus.CLAIMED
            wakeup.woke_at = due_before
            wakeup.updated_at = due_before
            return wakeup.model_copy(deep=True)

    async def claim_wakeup(self, *, wakeup_id: str, claimed_at: datetime) -> LifecycleWakeup | None:
        async with self._lock:
            wakeup = self._wakeups.get(wakeup_id)
            if (
                wakeup is None
                or wakeup.status != WakeupStatus.SCHEDULED
                or wakeup.scheduled_at > claimed_at
            ):
                return None
            wakeup.status = WakeupStatus.CLAIMED
            wakeup.woke_at = claimed_at
            wakeup.updated_at = claimed_at
            return wakeup.model_copy(deep=True)

    async def cancel_scheduled_wakeups(
        self, *, agent_id: str, replace_key: str
    ) -> list[LifecycleWakeup]:
        async with self._lock:
            cancelled: list[LifecycleWakeup] = []
            for wakeup in self._wakeups.values():
                if (
                    wakeup.agent_id == agent_id
                    and wakeup.replace_key == replace_key
                    and wakeup.status == WakeupStatus.SCHEDULED
                ):
                    wakeup.status = WakeupStatus.CANCELLED
                    wakeup.updated_at = utc_now()
                    cancelled.append(wakeup.model_copy(deep=True))
            return cancelled

    async def list_wakeups(
        self,
        *,
        agent_id: str,
        status: WakeupStatus | None = None,
        due_before: datetime | None = None,
        limit: int = 100,
    ) -> list[LifecycleWakeup]:
        _validate_limit(limit)
        async with self._lock:
            wakeups = [
                wakeup
                for wakeup in self._wakeups.values()
                if wakeup.agent_id == agent_id
                and (status is None or wakeup.status == status)
                and (due_before is None or wakeup.scheduled_at <= due_before)
            ]
            wakeups.sort(key=lambda wakeup: (wakeup.scheduled_at, -wakeup.priority))
            return [wakeup.model_copy(deep=True) for wakeup in wakeups[:limit]]


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")

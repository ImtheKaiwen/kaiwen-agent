from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.types import utc_now


class WorkloadKind(StrEnum):
    RUN = "run"
    JOB = "job"
    REALTIME_SESSION = "realtime_session"
    EVENT = "event"


class BackpressureLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_queue_depth: int = Field(default=1000, ge=0)
    max_concurrent: int = Field(default=10, ge=1)
    max_admissions_per_minute: int = Field(default=120, ge=1)
    low_priority_threshold: int = Field(default=-100, ge=-1000, le=1000)
    low_priority_queue_depth: int = Field(default=500, ge=0)


class BackpressureDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    admitted: bool
    reason_code: str
    retry_after_seconds: float | None = Field(default=None, ge=0)


class BackpressureLease:
    def __init__(
        self,
        *,
        controller: BackpressureController,
        tenant_id: str,
        kind: WorkloadKind,
        decision: BackpressureDecision,
    ) -> None:
        self.controller = controller
        self.tenant_id = tenant_id
        self.kind = kind
        self.decision = decision
        self._released = not decision.admitted

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self.controller.release(self.tenant_id, self.kind)

    async def __aenter__(self) -> BackpressureLease:
        if not self.decision.admitted:
            raise RuntimeError(f"Workload not admitted: {self.decision.reason_code}")
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.release()


class BackpressureController:
    """Process-local admission controller; production may replace it with Redis."""

    def __init__(
        self,
        *,
        limits: dict[WorkloadKind, BackpressureLimits],
    ) -> None:
        if not limits:
            raise ValueError("At least one workload limit is required")
        self.limits = dict(limits)
        self._active: dict[tuple[str, WorkloadKind], int] = defaultdict(int)
        self._admissions: dict[tuple[str, WorkloadKind], deque[datetime]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        *,
        tenant_id: str,
        kind: WorkloadKind,
        queue_depth: int,
        priority: int = 0,
        resource_available: bool = True,
        now: datetime | None = None,
    ) -> BackpressureLease:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        if queue_depth < 0:
            raise ValueError("queue_depth cannot be negative")
        observed_at = now or utc_now()
        if observed_at.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        async with self._lock:
            decision = self._decide(
                tenant_id=tenant_id,
                kind=kind,
                queue_depth=queue_depth,
                priority=priority,
                resource_available=resource_available,
                now=observed_at,
            )
            if decision.admitted:
                key = (tenant_id, kind)
                self._active[key] += 1
                self._admissions[key].append(observed_at)
        return BackpressureLease(
            controller=self,
            tenant_id=tenant_id,
            kind=kind,
            decision=decision,
        )

    async def release(self, tenant_id: str, kind: WorkloadKind) -> None:
        async with self._lock:
            key = (tenant_id, kind)
            if self._active[key] <= 0:
                raise ValueError("No active workload lease to release")
            self._active[key] -= 1

    async def active(self, tenant_id: str, kind: WorkloadKind) -> int:
        async with self._lock:
            return self._active[(tenant_id, kind)]

    def _decide(
        self,
        *,
        tenant_id: str,
        kind: WorkloadKind,
        queue_depth: int,
        priority: int,
        resource_available: bool,
        now: datetime,
    ) -> BackpressureDecision:
        try:
            limits = self.limits[kind]
        except KeyError as error:
            raise KeyError(f"No backpressure limits configured for {kind.value}") from error
        if not resource_available:
            return BackpressureDecision(
                admitted=False,
                reason_code="resource_capacity",
                retry_after_seconds=1,
            )
        if queue_depth >= limits.max_queue_depth:
            return BackpressureDecision(
                admitted=False,
                reason_code="queue_full",
                retry_after_seconds=1,
            )
        if (
            priority < limits.low_priority_threshold
            and queue_depth >= limits.low_priority_queue_depth
        ):
            return BackpressureDecision(
                admitted=False,
                reason_code="low_priority_dropped",
            )
        key = (tenant_id, kind)
        if self._active[key] >= limits.max_concurrent:
            return BackpressureDecision(
                admitted=False,
                reason_code="concurrency_limit",
                retry_after_seconds=1,
            )
        window = self._admissions[key]
        cutoff = now - timedelta(minutes=1)
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= limits.max_admissions_per_minute:
            retry_after = max(0.0, 60 - (now - window[0]).total_seconds())
            return BackpressureDecision(
                admitted=False,
                reason_code="rate_limit",
                retry_after_seconds=retry_after,
            )
        return BackpressureDecision(admitted=True, reason_code="admitted")

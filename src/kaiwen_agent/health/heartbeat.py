from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kaiwen_agent.types import utc_now


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class HealthThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    degraded_after_seconds: float = Field(default=30, gt=0)
    offline_after_seconds: float = Field(default=90, gt=0)

    @model_validator(mode="after")
    def validate_order(self) -> HealthThresholds:
        if self.offline_after_seconds <= self.degraded_after_seconds:
            raise ValueError("offline threshold must exceed degraded threshold")
        return self


class ComponentHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component_id: str = Field(min_length=1)
    status: HealthStatus
    last_heartbeat_at: datetime | None = None
    detail: str | None = None


class HealthReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: HealthStatus
    components: tuple[ComponentHealth, ...]
    observed_at: datetime = Field(default_factory=utc_now)


class HeartbeatMonitor:
    def __init__(self, components: dict[str, HealthThresholds]) -> None:
        if not components or any(not component_id for component_id in components):
            raise ValueError("At least one named health component is required")
        self.components = dict(components)
        self._heartbeats: dict[str, tuple[datetime, str | None]] = {}
        self._lock = asyncio.Lock()

    async def beat(
        self,
        component_id: str,
        *,
        detail: str | None = None,
        now: datetime | None = None,
    ) -> None:
        if component_id not in self.components:
            raise KeyError(f"Unknown health component: {component_id}")
        observed_at = now or utc_now()
        if observed_at.tzinfo is None:
            raise ValueError("Heartbeat timestamps must be timezone-aware")
        async with self._lock:
            previous = self._heartbeats.get(component_id)
            if previous is not None and observed_at < previous[0]:
                raise ValueError("Heartbeat timestamps cannot move backwards")
            self._heartbeats[component_id] = (observed_at, detail)

    async def report(self, *, now: datetime | None = None) -> HealthReport:
        observed_at = now or utc_now()
        if observed_at.tzinfo is None:
            raise ValueError("Health report timestamps must be timezone-aware")
        async with self._lock:
            components = tuple(
                self._component_health(component_id, observed_at)
                for component_id in sorted(self.components)
            )
        return HealthReport(
            status=_overall_status(components),
            components=components,
            observed_at=observed_at,
        )

    def _component_health(self, component_id: str, observed_at: datetime) -> ComponentHealth:
        heartbeat = self._heartbeats.get(component_id)
        if heartbeat is None:
            return ComponentHealth(
                component_id=component_id,
                status=HealthStatus.UNKNOWN,
            )
        last_seen, detail = heartbeat
        age = observed_at - last_seen
        thresholds = self.components[component_id]
        if age >= timedelta(seconds=thresholds.offline_after_seconds):
            status = HealthStatus.OFFLINE
        elif age >= timedelta(seconds=thresholds.degraded_after_seconds):
            status = HealthStatus.DEGRADED
        else:
            status = HealthStatus.HEALTHY
        return ComponentHealth(
            component_id=component_id,
            status=status,
            last_heartbeat_at=last_seen,
            detail=detail,
        )


def _overall_status(components: tuple[ComponentHealth, ...]) -> HealthStatus:
    statuses = {component.status for component in components}
    if HealthStatus.OFFLINE in statuses:
        return HealthStatus.OFFLINE
    if HealthStatus.DEGRADED in statuses:
        return HealthStatus.DEGRADED
    if HealthStatus.UNKNOWN in statuses:
        return HealthStatus.UNKNOWN
    return HealthStatus.HEALTHY

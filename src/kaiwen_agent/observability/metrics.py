from __future__ import annotations

import asyncio
from collections import deque
from typing import Protocol

from kaiwen_agent.observability.models import (
    MetricContext,
    MetricKind,
    MetricName,
    MetricRecord,
)
from kaiwen_agent.types import utc_now


class MetricsSink(Protocol):
    async def record(self, metric: MetricRecord) -> None: ...


class InMemoryMetricsSink:
    """Bounded reference sink for tests and local development."""

    def __init__(self, *, max_records: int = 10_000) -> None:
        if not 1 <= max_records <= 1_000_000:
            raise ValueError("max_records must be between 1 and 1000000")
        self._records: deque[MetricRecord] = deque(maxlen=max_records)
        self._lock = asyncio.Lock()

    async def record(self, metric: MetricRecord) -> None:
        async with self._lock:
            self._records.append(metric.model_copy(deep=True))

    async def list(
        self,
        *,
        tenant_id: str,
        name: MetricName | None = None,
        limit: int = 100,
    ) -> list[MetricRecord]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        async with self._lock:
            records = [
                record
                for record in reversed(self._records)
                if record.context.tenant_id == tenant_id and (name is None or record.name == name)
            ]
            return [record.model_copy(deep=True) for record in records[:limit]]


_METRIC_DEFINITIONS: dict[MetricName, tuple[MetricKind, str]] = {
    MetricName.AGENT_WAKEUPS_TOTAL: (MetricKind.COUNTER, "count"),
    MetricName.AGENT_CYCLES_TOTAL: (MetricKind.COUNTER, "count"),
    MetricName.AGENT_REASONING_CYCLES_TOTAL: (MetricKind.COUNTER, "count"),
    MetricName.AGENT_SLEEP_DURATION: (MetricKind.HISTOGRAM, "seconds"),
    MetricName.JOBS_ACTIVE: (MetricKind.GAUGE, "count"),
    MetricName.JOBS_FAILED: (MetricKind.COUNTER, "count"),
    MetricName.JOBS_DURATION: (MetricKind.HISTOGRAM, "seconds"),
    MetricName.POLICY_DENIES: (MetricKind.COUNTER, "count"),
    MetricName.POLICY_APPROVALS: (MetricKind.COUNTER, "count"),
    MetricName.MEMORY_CANDIDATES: (MetricKind.COUNTER, "count"),
    MetricName.MEMORY_SAVED: (MetricKind.COUNTER, "count"),
    MetricName.MEMORY_DISCARDED: (MetricKind.COUNTER, "count"),
    MetricName.MEMORY_REVIEW: (MetricKind.COUNTER, "count"),
    MetricName.CONTEXT_TOKENS: (MetricKind.HISTOGRAM, "tokens"),
    MetricName.MODEL_COST: (MetricKind.HISTOGRAM, "usd"),
    MetricName.WATCH_COUNT: (MetricKind.GAUGE, "count"),
    MetricName.OBJECTIVE_ACTIVE: (MetricKind.GAUGE, "count"),
    MetricName.EVENT_QUEUE_DEPTH: (MetricKind.GAUGE, "count"),
}


class RuntimeMetrics:
    """Small vendor-neutral recorder over a fixed, low-cardinality catalog."""

    def __init__(self, sink: MetricsSink) -> None:
        self.sink = sink

    async def increment(
        self,
        name: MetricName,
        context: MetricContext,
        *,
        value: float = 1,
        status: str | None = None,
        error_code: str | None = None,
    ) -> None:
        await self._record(
            name,
            MetricKind.COUNTER,
            value,
            context,
            status=status,
            error_code=error_code,
        )

    async def gauge(
        self,
        name: MetricName,
        value: float,
        context: MetricContext,
        *,
        status: str | None = None,
        error_code: str | None = None,
    ) -> None:
        await self._record(
            name,
            MetricKind.GAUGE,
            value,
            context,
            status=status,
            error_code=error_code,
        )

    async def observe(
        self,
        name: MetricName,
        value: float,
        context: MetricContext,
        *,
        status: str | None = None,
        error_code: str | None = None,
    ) -> None:
        await self._record(
            name,
            MetricKind.HISTOGRAM,
            value,
            context,
            status=status,
            error_code=error_code,
        )

    async def _record(
        self,
        name: MetricName,
        expected_kind: MetricKind,
        value: float,
        context: MetricContext,
        *,
        status: str | None,
        error_code: str | None,
    ) -> None:
        kind, unit = _METRIC_DEFINITIONS[name]
        if kind != expected_kind:
            raise ValueError(f"{name.value} is a {kind.value}, not {expected_kind.value}")
        if kind != MetricKind.GAUGE and value < 0:
            raise ValueError(f"{kind.value} values cannot be negative")
        await self.sink.record(
            MetricRecord(
                name=name,
                kind=kind,
                value=value,
                unit=unit,
                context=context,
                status=status,
                error_code=error_code,
                recorded_at=utc_now(),
            )
        )

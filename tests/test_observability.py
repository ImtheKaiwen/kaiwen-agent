from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from kaiwen_agent.observability import (
    InMemoryMetricsSink,
    MetricContext,
    MetricName,
    RuntimeMetrics,
)


def metric_context(tenant_id: str) -> MetricContext:
    return MetricContext(
        tenant_id=tenant_id,
        agent_id="agent_1",
        trace_id=f"trace_{tenant_id}",
        run_id="run_1",
        job_id="job_1",
        cycle_id="cycle_1",
    )


def test_runtime_metrics_are_catalogued_bounded_and_tenant_scoped() -> None:
    async def scenario() -> None:
        sink = InMemoryMetricsSink(max_records=3)
        metrics = RuntimeMetrics(sink)
        await metrics.increment(MetricName.AGENT_WAKEUPS_TOTAL, metric_context("tenant_a"))
        await metrics.gauge(MetricName.JOBS_ACTIVE, 2, metric_context("tenant_b"))
        await metrics.observe(
            MetricName.JOBS_DURATION,
            1.25,
            metric_context("tenant_a"),
            status="completed",
        )
        await metrics.increment(MetricName.JOBS_FAILED, metric_context("tenant_a"))

        tenant_a = await sink.list(tenant_id="tenant_a")
        tenant_b = await sink.list(tenant_id="tenant_b")
        assert [record.name for record in tenant_a] == [
            MetricName.JOBS_FAILED,
            MetricName.JOBS_DURATION,
        ]
        assert [record.name for record in tenant_b] == [MetricName.JOBS_ACTIVE]
        assert tenant_a[1].unit == "seconds"

    asyncio.run(scenario())


def test_runtime_metrics_reject_wrong_kind_unbounded_fields_and_invalid_values() -> None:
    async def scenario() -> None:
        metrics = RuntimeMetrics(InMemoryMetricsSink())
        with pytest.raises(ValueError, match="histogram"):
            await metrics.increment(MetricName.JOBS_DURATION, metric_context("tenant_a"))
        with pytest.raises(ValueError, match="cannot be negative"):
            await metrics.observe(
                MetricName.JOBS_DURATION,
                -1,
                metric_context("tenant_a"),
            )

    asyncio.run(scenario())
    with pytest.raises(ValidationError, match="Extra inputs"):
        MetricContext(
            tenant_id="tenant_a",
            agent_id="agent_1",
            trace_id="trace_1",
            arbitrary_label="not-allowed",
        )

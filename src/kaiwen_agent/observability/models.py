from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kaiwen_agent.types import utc_now


class MetricKind(StrEnum):
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


class MetricName(StrEnum):
    AGENT_WAKEUPS_TOTAL = "agent_wakeups_total"
    AGENT_CYCLES_TOTAL = "agent_cycles_total"
    AGENT_REASONING_CYCLES_TOTAL = "agent_reasoning_cycles_total"
    AGENT_SLEEP_DURATION = "agent_sleep_duration"
    JOBS_ACTIVE = "jobs_active"
    JOBS_FAILED = "jobs_failed"
    JOBS_DURATION = "jobs_duration"
    POLICY_DENIES = "policy_denies"
    POLICY_APPROVALS = "policy_approvals"
    MEMORY_CANDIDATES = "memory_candidates"
    MEMORY_SAVED = "memory_saved"
    MEMORY_DISCARDED = "memory_discarded"
    MEMORY_REVIEW = "memory_review"
    CONTEXT_TOKENS = "context_tokens"
    MODEL_COST = "model_cost"
    WATCH_COUNT = "watch_count"
    OBJECTIVE_ACTIVE = "objective_active"
    EVENT_QUEUE_DEPTH = "event_queue_depth"


class MetricContext(BaseModel):
    """Trusted runtime identity attached to a metric without arbitrary labels."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1, max_length=200)
    agent_id: str = Field(min_length=1, max_length=200)
    trace_id: str = Field(min_length=1, max_length=200)
    run_id: str | None = Field(default=None, min_length=1, max_length=200)
    job_id: str | None = Field(default=None, min_length=1, max_length=200)
    cycle_id: str | None = Field(default=None, min_length=1, max_length=200)


class MetricRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: MetricName
    kind: MetricKind
    value: float
    unit: str = Field(min_length=1, max_length=32)
    context: MetricContext
    status: str | None = Field(default=None, min_length=1, max_length=64)
    error_code: str | None = Field(default=None, min_length=1, max_length=100)
    recorded_at: datetime = Field(default_factory=utc_now)

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: float) -> float:
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError("Metric value must be finite")
        return value

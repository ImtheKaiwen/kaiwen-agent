from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.types import utc_now


class LifecycleState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    SLEEPING = "sleeping"
    WAKING = "waking"
    OBSERVING = "observing"
    THINKING = "thinking"
    ACTING = "acting"
    PERSISTING = "persisting"
    SCHEDULING = "scheduling"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_JOB = "waiting_job"
    PAUSED = "paused"
    DEGRADED = "degraded"
    FAILED = "failed"


class WakeTriggerType(StrEnum):
    USER_MESSAGE = "user_message"
    SCHEDULE = "schedule"
    EVENT = "event"
    WEBHOOK = "webhook"
    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"
    APPROVAL_COMPLETED = "approval_completed"
    OBJECTIVE_DUE = "objective_due"
    WATCH_MATCHED = "watch_matched"
    SYSTEM_ALERT = "system_alert"
    MANUAL = "manual"


class WakeupStatus(StrEnum):
    SCHEDULED = "scheduled"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LifecycleCycleStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class WakeTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"trg_{uuid4().hex}")
    type: WakeTriggerType
    reason: str = Field(min_length=1)
    tenant_id: str | None = None
    user_id: str | None = None
    conversation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class LifecycleObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)


class LifecycleObservationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observations: list[LifecycleObservation] = Field(default_factory=list)
    requires_reasoning: bool = False


class LifecycleWakeup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"wkp_{uuid4().hex}")
    agent_id: str = Field(min_length=1)
    trigger: WakeTrigger
    scheduled_at: datetime
    replace_key: str | None = None
    priority: int = Field(default=0, ge=-100, le=100)
    status: WakeupStatus = WakeupStatus.SCHEDULED
    cycle_id: str | None = None
    woke_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class LifecycleCycle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"cyc_{uuid4().hex}")
    trace_id: str = Field(default_factory=lambda: f"trc_{uuid4().hex}")
    agent_id: str = Field(min_length=1)
    trigger: WakeTrigger
    wakeup_id: str | None = None
    state: LifecycleState = LifecycleState.WAKING
    status: LifecycleCycleStatus = LifecycleCycleStatus.RUNNING
    woke_at: datetime = Field(default_factory=utc_now)
    observe_started_at: datetime | None = None
    think_started_at: datetime | None = None
    act_started_at: datetime | None = None
    completed_at: datetime | None = None
    observations_count: int = Field(default=0, ge=0)
    reasoning_used: bool = False
    actions_count: int = Field(default=0, ge=0)
    jobs_created: int = Field(default=0, ge=0)
    next_wakeup_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class LifecycleRuntimeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1)
    state: LifecycleState = LifecycleState.STOPPED
    paused_from: LifecycleState | None = None
    active_cycle_id: str | None = None
    last_cycle_id: str | None = None
    last_woke_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class LifecycleRuntimeStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    state: LifecycleState
    active_cycle_id: str | None = None
    last_cycle_id: str | None = None
    last_woke_at: datetime | None = None
    next_wakeup: LifecycleWakeup | None = None
    paused: bool = False


class LifecycleRuntimeCard(BaseModel):
    """Stable, read-only contract for an admin runtime status card."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    state: LifecycleState
    last_wake_at: datetime | None = None
    next_wake_at: datetime | None = None
    active_cycle_id: str | None = None
    paused: bool = False

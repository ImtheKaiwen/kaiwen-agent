from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kaiwen_agent.quality import CompletionGateResult
from kaiwen_agent.runtime import RuntimeRunRecord
from kaiwen_agent.tasks import AgentTask


class WorkflowRetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_attempts: int = Field(default=1, ge=1, le=10)
    backoff_seconds: float = Field(default=0, ge=0, le=3_600)
    backoff_multiplier: float = Field(default=1, ge=1, le=10)
    retry_on: frozenset[str] = Field(default_factory=lambda: frozenset({"timeout"}))


class WorkflowTaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=4_000)
    worker_id: str = Field(min_length=1, max_length=200)
    dependencies: tuple[str, ...] = ()
    input: dict[str, Any] = Field(default_factory=dict)
    resource_keys: tuple[str, ...] = ()
    capabilities: frozenset[str] = Field(default_factory=frozenset)
    retry_policy: WorkflowRetryPolicy = Field(default_factory=WorkflowRetryPolicy)
    timeout_seconds: float | None = Field(default=None, gt=0, le=86_400)

    @model_validator(mode="after")
    def validate_sets(self) -> WorkflowTaskSpec:
        if len(self.dependencies) != len(set(self.dependencies)):
            raise ValueError("dependencies must not contain duplicates")
        if self.id in self.dependencies:
            raise ValueError("A workflow task cannot depend on itself")
        if len(self.resource_keys) != len(set(self.resource_keys)):
            raise ValueError("resource_keys must not contain duplicates")
        return self


class WorkflowPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(default=1, ge=1)
    objective: str = Field(min_length=1, max_length=10_000)
    tasks: tuple[WorkflowTaskSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_task_ids(self) -> WorkflowPlan:
        ids = [task.id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("Workflow task ids must be unique")
        return self


class WorkflowLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_tasks_per_plan: int = Field(default=50, ge=1, le=1_000)
    max_dependencies_per_task: int = Field(default=20, ge=0, le=1_000)
    max_input_bytes_per_task: int = Field(default=256_000, ge=1, le=10_000_000)
    max_cycles: int = Field(default=5, ge=1, le=100)


class WorkflowRevisionReason(StrEnum):
    EXECUTION_FAILED = "execution_failed"
    QUALITY_BLOCKED = "quality_blocked"


class WorkflowTaskFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    logical_task_id: str
    error_code: str | None = None


class WorkflowRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    cycle: int = Field(ge=1)
    reason: WorkflowRevisionReason
    prior_plan: WorkflowPlan
    task_failures: tuple[WorkflowTaskFailure, ...] = ()
    quality_result: CompletionGateResult | None = None


class WorkflowOutcomeStatus(StrEnum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class WorkflowOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: WorkflowOutcomeStatus
    run: RuntimeRunRecord
    cycles: int = Field(ge=1)
    tasks: tuple[AgentTask, ...]
    quality_result: CompletionGateResult | None = None
    reason: str | None = None

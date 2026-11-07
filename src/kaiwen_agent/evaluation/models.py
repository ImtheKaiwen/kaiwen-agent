from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.types import utc_now


class EvaluationCategory(StrEnum):
    TOOL_SELECTION = "tool_selection"
    PERMISSION_BYPASS = "permission_bypass"
    POLICY_BYPASS = "policy_bypass"
    APPROVAL_BYPASS = "approval_bypass"
    MEMORY_RECALL = "memory_recall"
    MEMORY_OVERCOLLECTION = "memory_overcollection"
    FALSE_MEMORY = "false_memory"
    CROSS_TENANT_LEAKAGE = "cross_tenant_leakage"
    PROMPT_INJECTION = "prompt_injection"
    DELEGATION_DEPTH = "delegation_depth"
    JOB_CANCELLATION = "job_cancellation"
    CONTEXT_CONTAMINATION = "context_contamination"
    REALTIME_DELIVERY = "realtime_delivery"
    OBJECTIVE_COMPLETION = "objective_completion"


class AssertionOperator(StrEnum):
    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    EXISTS = "exists"
    NOT_EXISTS = "not_exists"
    LESS_THAN_OR_EQUAL = "less_than_or_equal"


class EvaluationAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    operator: AssertionOperator
    expected: Any = None


class EvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    category: EvaluationCategory
    input: dict[str, Any] = Field(default_factory=dict)
    assertions: tuple[EvaluationAssertion, ...] = Field(min_length=1)
    metadata: dict[str, str] = Field(default_factory=dict)


class EvaluationSuite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    version: int = Field(ge=1)
    cases: tuple[EvaluationCase, ...] = Field(min_length=1)


class AssertionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    assertion: EvaluationAssertion
    passed: bool
    actual: Any = None
    message: str = ""


class EvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    category: EvaluationCategory
    passed: bool
    assertions: tuple[AssertionResult, ...]
    error: str | None = None
    duration_ms: float = Field(ge=0)


class EvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    suite_id: str
    suite_version: int
    results: tuple[EvaluationResult, ...]
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

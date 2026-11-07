from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.model_routing import ModelTier


class BudgetScope(StrEnum):
    RUN = "run"
    JOB = "job"
    AGENT = "agent"
    USER = "user"
    TENANT = "tenant"
    DAY = "day"


class BudgetAction(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    DEGRADE_MODEL = "degrade_model"
    REQUEST_APPROVAL = "request_approval"
    PAUSE = "pause"


class BudgetLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_input_tokens: int | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, ge=0)
    max_tool_calls: int | None = Field(default=None, ge=0)
    max_cost_usd: float | None = Field(default=None, ge=0)
    max_jobs: int | None = Field(default=None, ge=0)


class BudgetUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0, ge=0)
    jobs: int = Field(default=0, ge=0)

    def plus(self, other: BudgetUsage) -> BudgetUsage:
        return BudgetUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            tool_calls=self.tool_calls + other.tool_calls,
            cost_usd=self.cost_usd + other.cost_usd,
            jobs=self.jobs + other.jobs,
        )


class BudgetRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    scope: BudgetScope
    limits: BudgetLimits


class BudgetContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    run_id: str | None = None
    job_id: str | None = None
    conversation_id: str | None = None


class BudgetOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool
    action: BudgetAction
    exceeded_rule_ids: list[str] = Field(default_factory=list)
    allowed_model_tiers: frozenset[ModelTier] | None = None

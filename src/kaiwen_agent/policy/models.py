from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PolicyDecisionType(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"
    REVIEW = "review"
    MODIFY = "modify"


class PolicyContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str | None = None
    user_id: str | None = None
    agent_id: str
    session_id: str
    conversation_id: str | None = None
    run_id: str | None = None
    job_id: str | None = None
    trace_id: str
    environment: str = "development"
    action: str = Field(min_length=1)
    resource: str | None = None
    capabilities: frozenset[str] = Field(default_factory=frozenset)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: PolicyDecisionType
    policy_id: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    modifications: dict[str, Any] | None = None
    approval_scope: str | None = None

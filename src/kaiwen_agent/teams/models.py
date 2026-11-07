from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kaiwen_agent.agents import VersionedReference
from kaiwen_agent.jobs import JobStatus


class TeamMemberDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str = Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    agent_ref: VersionedReference
    capabilities: frozenset[str] = Field(default_factory=frozenset)
    may_delegate: bool = False


class TeamDefinition(BaseModel):
    """Immutable composition metadata; it grants no authority by itself."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=1)
    description: str = Field(default="", max_length=2_000)
    lead_agent_ref: VersionedReference
    members: tuple[TeamMemberDefinition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_members(self) -> TeamDefinition:
        roles = [member.role for member in self.members]
        if len(roles) != len(set(roles)):
            raise ValueError("Team member roles must be unique")
        refs = [(member.agent_ref.id, member.agent_ref.version) for member in self.members]
        if len(refs) != len(set(refs)):
            raise ValueError("Team member agent references must be unique")
        return self


class TeamContextReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    reference: str = Field(min_length=1, max_length=2_000)


class TeamDelegationRequest(BaseModel):
    """Untrusted delegation intent without owner or caller identity fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    objective: str = Field(min_length=1, max_length=2_000)
    role: str | None = Field(default=None, min_length=1, max_length=200)
    input: dict[str, Any] = Field(default_factory=dict)
    context_refs: tuple[TeamContextReference, ...] = ()
    requested_capabilities: frozenset[str] = Field(default_factory=frozenset)
    parent_job_id: str | None = None
    priority: int = Field(default=0, ge=-1000, le=1000)
    timeout_seconds: float | None = Field(default=None, gt=0)
    resource_keys: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_references(self) -> TeamDelegationRequest:
        names = [item.name for item in self.context_refs]
        if len(names) != len(set(names)):
            raise ValueError("Context reference names must be unique")
        if any(name.startswith("kaiwen.") for name in names):
            raise ValueError("Context reference name uses a reserved prefix")
        if len(self.resource_keys) != len(set(self.resource_keys)):
            raise ValueError("Resource keys must be unique")
        try:
            json.dumps(self.input, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("Team delegation input must be strict JSON") from error
        return self


class TeamHandoff(BaseModel):
    """Bounded child result returned without private worker context or logs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str
    role: str
    agent_ref: VersionedReference
    status: JobStatus
    summary: str | None = None
    artifact_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    follow_up_suggestions: tuple[str, ...] = ()

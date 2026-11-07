from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kaiwen_agent.agents import VersionedReference


class RequirementObligation(StrEnum):
    MUST = "must"
    SHOULD = "should"
    CONDITIONAL = "conditional"


class RequirementSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class RequirementDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=200)
    version: int = Field(default=1, ge=1)
    description: str = Field(min_length=1)
    obligation: RequirementObligation
    severity: RequirementSeverity = RequirementSeverity.ERROR
    applies_when: str | None = Field(default=None, min_length=1, max_length=200)
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    verifier_ref: str = Field(min_length=1, max_length=200)
    evidence_types: frozenset[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_condition(self) -> RequirementDefinition:
        if self.obligation == RequirementObligation.CONDITIONAL and self.applies_when is None:
            raise ValueError("Conditional requirements need applies_when")
        if self.obligation != RequirementObligation.CONDITIONAL and self.applies_when is not None:
            raise ValueError("Only conditional requirements may declare applies_when")
        return self


class StandardsPack(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=1)
    description: str = ""
    prompt_refs: tuple[VersionedReference, ...] = ()
    requirements: tuple[RequirementDefinition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_requirements(self) -> StandardsPack:
        ids = [requirement.id for requirement in self.requirements]
        if len(ids) != len(set(ids)):
            raise ValueError("Requirement ids must be unique within a standards pack")
        return self


class ResolvedRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    definition: RequirementDefinition
    source_pack_refs: tuple[VersionedReference, ...] = Field(min_length=1)
    applies: bool | None

    @property
    def mandatory(self) -> bool:
        return self.definition.obligation in {
            RequirementObligation.MUST,
            RequirementObligation.CONDITIONAL,
        }


class StandardResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pack_refs: tuple[VersionedReference, ...]
    requirements: tuple[ResolvedRequirement, ...]
    prompt_refs: tuple[VersionedReference, ...]
    conflicts: tuple[str, ...] = ()
    unresolved_conditions: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.conflicts

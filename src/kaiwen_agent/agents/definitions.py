from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class VersionedReference(BaseModel):
    """An immutable reference to one exact registry entry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=1)


class AgentDefinition(BaseModel):
    """Versioned blueprint used to compose, but never execute, an agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=1)
    description: str = ""
    role: str = Field(min_length=1, max_length=200)
    instructions_ref: VersionedReference
    provider_ref: str = Field(min_length=1, max_length=200)
    tool_refs: frozenset[str] = Field(default_factory=frozenset)
    capability_manifest_ref: VersionedReference
    memory_namespaces: frozenset[str] = Field(default_factory=frozenset)
    permissions: frozenset[str] = Field(default_factory=frozenset)
    max_steps: int = Field(default=8, ge=1, le=100)

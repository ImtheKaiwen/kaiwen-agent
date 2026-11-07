from __future__ import annotations

from hashlib import sha256
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kaiwen_agent.agents import VersionedReference
from kaiwen_agent.runtime.models import AutonomyLevel


class WorkspaceProfile(BaseModel):
    """Strict host-authored composition limits for one workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=1)
    agent_refs: tuple[VersionedReference, ...] = Field(min_length=1)
    default_agent_ref: VersionedReference
    provider_allowlist: frozenset[str] = Field(min_length=1)
    tool_allowlist: frozenset[str] = Field(default_factory=frozenset)
    standards_pack_refs: tuple[VersionedReference, ...] = ()
    default_autonomy: AutonomyLevel = AutonomyLevel.SUPERVISED

    @model_validator(mode="after")
    def validate_references(self) -> WorkspaceProfile:
        keys = [(reference.id, reference.version) for reference in self.agent_refs]
        if len(keys) != len(set(keys)):
            raise ValueError("agent_refs must not contain duplicates")
        default_key = (self.default_agent_ref.id, self.default_agent_ref.version)
        if default_key not in set(keys):
            raise ValueError("default_agent_ref must be present in agent_refs")
        standard_keys = [
            (reference.id, reference.version) for reference in self.standards_pack_refs
        ]
        if len(standard_keys) != len(set(standard_keys)):
            raise ValueError("standards_pack_refs must not contain duplicates")
        return self


class RuntimeVersionPins(BaseModel):
    """Exact configuration selected before a runtime starts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str
    workspace_version: int
    agent_id: str
    agent_version: int
    prompt_id: str
    prompt_version: int
    prompt_checksum: str
    capability_manifest_id: str
    capability_manifest_version: int
    provider_ref: str
    tool_names: tuple[str, ...]
    toolset_checksum: str
    standards_pack_refs: tuple[str, ...] = ()

    @classmethod
    def toolset_hash(cls, records: list[dict[str, Any]]) -> str:
        import json

        encoded = json.dumps(records, sort_keys=True, separators=(",", ":"), default=str)
        return sha256(encoded.encode()).hexdigest()

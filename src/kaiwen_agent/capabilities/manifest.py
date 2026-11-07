from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CapabilityManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    tools: frozenset[str] = Field(default_factory=frozenset)
    memory_namespaces: frozenset[str] = Field(default_factory=frozenset)
    knowledge_sources: frozenset[str] = Field(default_factory=frozenset)
    model_tiers: frozenset[str] = Field(default_factory=frozenset)
    delegates: frozenset[str] = Field(default_factory=frozenset)
    client_actions: frozenset[str] = Field(default_factory=frozenset)

    def contains(self, requested: CapabilityManifest) -> bool:
        return (
            requested.tools <= self.tools
            and requested.memory_namespaces <= self.memory_namespaces
            and requested.knowledge_sources <= self.knowledge_sources
            and requested.model_tiers <= self.model_tiers
            and requested.delegates <= self.delegates
            and requested.client_actions <= self.client_actions
        )


class CapabilityRegistry:
    def __init__(self) -> None:
        self._manifests: dict[tuple[str, int], CapabilityManifest] = {}
        self._latest: dict[str, int] = {}

    def register(self, manifest: CapabilityManifest) -> None:
        key = (manifest.agent_id, manifest.version)
        if key in self._manifests:
            raise ValueError(f"Capability manifest already registered: {key}")
        self._manifests[key] = manifest
        self._latest[manifest.agent_id] = max(
            manifest.version, self._latest.get(manifest.agent_id, 0)
        )

    def get(self, agent_id: str, version: int | None = None) -> CapabilityManifest:
        selected = version if version is not None else self._latest.get(agent_id)
        if selected is None:
            raise KeyError(f"Unknown capability manifest: {agent_id}")
        try:
            return self._manifests[(agent_id, selected)]
        except KeyError as error:
            raise KeyError(f"Unknown capability manifest: {agent_id}@{selected}") from error

    def list(self) -> tuple[CapabilityManifest, ...]:
        return tuple(
            self._manifests[key]
            for key in sorted(self._manifests, key=lambda item: (item[0], item[1]))
        )

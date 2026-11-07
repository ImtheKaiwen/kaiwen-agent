from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.agents import VersionedReference


class KitRoleDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str = Field(min_length=1, max_length=100)
    prompt_ref: VersionedReference
    tool_bundle_refs: tuple[VersionedReference, ...] = ()


class KitDefinition(BaseModel):
    """Immutable composition metadata; a kit never grants runtime authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=1)
    schema_version: int = Field(default=1, ge=1)
    description: str = Field(default="", max_length=2_000)
    compatible_runtime: str = Field(min_length=1, max_length=100)
    prompt_refs: tuple[VersionedReference, ...] = ()
    standards_refs: tuple[VersionedReference, ...] = ()
    skill_refs: tuple[VersionedReference, ...] = ()
    tool_bundle_refs: tuple[VersionedReference, ...] = ()
    roles: tuple[KitRoleDefinition, ...] = ()
    design_preset_refs: tuple[VersionedReference, ...] = ()
    supported_modes: frozenset[str] = Field(default_factory=frozenset)


class KitRegistry:
    def __init__(self, definitions: Iterable[KitDefinition] = ()) -> None:
        self._definitions: dict[tuple[str, int], KitDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: KitDefinition) -> None:
        key = (definition.id, definition.version)
        existing = self._definitions.get(key)
        if existing is not None:
            if existing == definition:
                return
            raise ValueError(
                f"Conflicting kit already registered: {definition.id}@{definition.version}"
            )
        self._definitions[key] = definition

    def get(self, kit_id: str, version: int) -> KitDefinition:
        try:
            return self._definitions[(kit_id, version)]
        except KeyError as error:
            raise KeyError(f"Unknown kit: {kit_id}@{version}") from error

    def list(self) -> tuple[KitDefinition, ...]:
        return tuple(self._definitions[key] for key in sorted(self._definitions))

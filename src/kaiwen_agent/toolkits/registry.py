from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field


class ToolBundleDefinition(BaseModel):
    """A versioned tool-name selection that never grants authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=1)
    description: str = Field(default="", max_length=2_000)
    tool_names: frozenset[str] = Field(min_length=1)


class ToolBundleRegistry:
    def __init__(self, definitions: Iterable[ToolBundleDefinition] = ()) -> None:
        self._definitions: dict[tuple[str, int], ToolBundleDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: ToolBundleDefinition) -> None:
        key = (definition.id, definition.version)
        existing = self._definitions.get(key)
        if existing is not None:
            if existing == definition:
                return
            raise ValueError(
                f"Conflicting tool bundle already registered: {definition.id}@{definition.version}"
            )
        self._definitions[key] = definition

    def get(self, bundle_id: str, version: int) -> ToolBundleDefinition:
        try:
            return self._definitions[(bundle_id, version)]
        except KeyError as error:
            raise KeyError(f"Unknown tool bundle: {bundle_id}@{version}") from error

    def list(self) -> tuple[ToolBundleDefinition, ...]:
        return tuple(self._definitions[key] for key in sorted(self._definitions))

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ModelToolStrategy(StrEnum):
    NATIVE = "native"
    STRUCTURED = "structured"
    DELEGATED = "delegated"
    TEXT_ONLY = "text_only"


class DataLocality(StrEnum):
    LOCAL_ONLY = "local_only"
    REGION_RESTRICTED = "region_restricted"
    CLOUD_ALLOWED = "cloud_allowed"


class ModelCapabilities(BaseModel):
    """Technical provider/model behavior; this model grants no authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=300)
    context_window: int = Field(ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    tool_strategy: ModelToolStrategy = ModelToolStrategy.TEXT_ONLY
    parallel_tool_calls: bool = False
    structured_output: bool = False
    streaming: bool = False
    vision: bool = False
    audio_input: bool = False
    data_locality: DataLocality = DataLocality.CLOUD_ALLOWED
    regions: frozenset[str] = Field(default_factory=frozenset)


class ModelCapabilitiesRegistry:
    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], ModelCapabilities] = {}

    def register(self, capabilities: ModelCapabilities) -> None:
        key = (capabilities.provider, capabilities.model)
        if key in self._entries:
            raise ValueError(f"Model capabilities already registered: {key}")
        self._entries[key] = capabilities

    def get(self, provider: str, model: str) -> ModelCapabilities:
        try:
            return self._entries[(provider, model)]
        except KeyError as error:
            raise KeyError(f"Unknown model capabilities: {provider}/{model}") from error

    def list(self) -> tuple[ModelCapabilities, ...]:
        return tuple(self._entries[key] for key in sorted(self._entries))

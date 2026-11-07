from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.models import (
    DataLocality,
    ModelCapabilities,
    ModelCapabilitiesRegistry,
    ModelProvider,
    ModelToolStrategy,
    ProviderRegistry,
)


class CodingProviderPreset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=200)
    provider_ref: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=300)
    prompt_budget_tokens: int = Field(ge=1)


class ProviderRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requires_tools: bool = False
    requires_structured_output: bool = False
    locality: DataLocality = DataLocality.CLOUD_ALLOWED


class ProviderCompatibilityIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str


class ProviderCompatibility(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    preset: CodingProviderPreset
    capabilities: ModelCapabilities
    issues: tuple[ProviderCompatibilityIssue, ...] = ()

    @property
    def compatible(self) -> bool:
        return not self.issues


class RoleUsageSummary(BaseModel):
    """Unknown provider usage remains None and is never reported as zero."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    duration_seconds: float | None = Field(default=None, ge=0)


class CodingProviderCatalog:
    def __init__(self) -> None:
        self._presets: dict[str, CodingProviderPreset] = {}

    def register(self, preset: CodingProviderPreset) -> None:
        existing = self._presets.get(preset.id)
        if existing is not None and existing != preset:
            raise ValueError(f"Conflicting coding provider preset: {preset.id}")
        self._presets[preset.id] = preset

    def get(self, preset_id: str) -> CodingProviderPreset:
        try:
            return self._presets[preset_id]
        except KeyError as error:
            raise KeyError(f"Unknown coding provider preset: {preset_id}") from error

    def list(self) -> tuple[CodingProviderPreset, ...]:
        return tuple(self._presets[key] for key in sorted(self._presets))


def register_coding_provider(
    *,
    providers: ProviderRegistry,
    capabilities: ModelCapabilitiesRegistry,
    catalog: CodingProviderCatalog,
    preset: CodingProviderPreset,
    provider_factory: Callable[[str], ModelProvider],
    model_capabilities: ModelCapabilities,
) -> None:
    if (preset.provider, preset.model) != (
        model_capabilities.provider,
        model_capabilities.model,
    ):
        raise ValueError("Preset and model capabilities must describe the same provider/model")
    if providers.contains(preset.provider_ref):
        raise ValueError(f"Provider already registered: {preset.provider_ref}")
    providers.register(preset.provider_ref, provider_factory)
    capabilities.register(model_capabilities)
    catalog.register(preset)


def check_provider_compatibility(
    preset: CodingProviderPreset,
    *,
    capabilities: ModelCapabilitiesRegistry,
    requirement: ProviderRequirement,
) -> ProviderCompatibility:
    try:
        model = capabilities.get(preset.provider, preset.model)
    except KeyError as error:
        raise ValueError(
            f"model_not_installed: register capabilities for {preset.provider}/{preset.model}"
        ) from error
    issues: list[ProviderCompatibilityIssue] = []
    if requirement.requires_tools and model.tool_strategy is ModelToolStrategy.TEXT_ONLY:
        issues.append(
            ProviderCompatibilityIssue(
                code="model_tool_unsupported",
                message="Select a tool-capable model or run a chat-only workflow.",
            )
        )
    if requirement.requires_structured_output and not model.structured_output:
        issues.append(
            ProviderCompatibilityIssue(
                code="structured_plan_unsupported",
                message="Select a model with validated structured-output support.",
            )
        )
    if (
        requirement.locality is DataLocality.LOCAL_ONLY
        and model.data_locality is not DataLocality.LOCAL_ONLY
    ):
        issues.append(
            ProviderCompatibilityIssue(
                code="locality_violation",
                message="Local-only data cannot be routed to this provider.",
            )
        )
    if preset.prompt_budget_tokens >= model.context_window:
        issues.append(
            ProviderCompatibilityIssue(
                code="budget_too_small",
                message="Prompt budget leaves no model context for the request and response.",
            )
        )
    return ProviderCompatibility(preset=preset, capabilities=model, issues=tuple(issues))

from __future__ import annotations

import pytest

from kaiwen_agent.kits.coding import (
    CodingProviderCatalog,
    CodingProviderPreset,
    ProviderRequirement,
    RoleUsageSummary,
    check_provider_compatibility,
    register_coding_provider,
)
from kaiwen_agent.models import (
    DataLocality,
    FakeModelProvider,
    ModelCapabilities,
    ModelCapabilitiesRegistry,
    ModelToolStrategy,
    ProviderRegistry,
)
from kaiwen_agent.types import ModelResponse


@pytest.mark.parametrize(
    ("provider", "locality"),
    [
        ("openai", DataLocality.CLOUD_ALLOWED),
        ("gemini", DataLocality.CLOUD_ALLOWED),
        ("ollama", DataLocality.LOCAL_ONLY),
    ],
)
def test_provider_presets_use_existing_registries(provider: str, locality: DataLocality) -> None:
    providers = ProviderRegistry()
    capabilities = ModelCapabilitiesRegistry()
    catalog = CodingProviderCatalog()
    preset = CodingProviderPreset(
        id=f"{provider}.test",
        provider_ref=f"{provider}:test",
        provider=provider,
        model="test-model",
        prompt_budget_tokens=1_000,
    )
    model = ModelCapabilities(
        provider=provider,
        model="test-model",
        context_window=16_000,
        tool_strategy=ModelToolStrategy.NATIVE,
        structured_output=True,
        data_locality=locality,
    )

    register_coding_provider(
        providers=providers,
        capabilities=capabilities,
        catalog=catalog,
        preset=preset,
        provider_factory=lambda instructions: FakeModelProvider(
            [ModelResponse(text=instructions[:1] or "ok")]
        ),
        model_capabilities=model,
    )

    result = check_provider_compatibility(
        preset,
        capabilities=capabilities,
        requirement=ProviderRequirement(requires_tools=True, requires_structured_output=True),
    )
    assert result.compatible
    assert providers.contains(preset.provider_ref)


def test_text_only_ollama_fails_tool_preflight_but_not_plain_chat() -> None:
    capabilities = ModelCapabilitiesRegistry()
    capabilities.register(
        ModelCapabilities(
            provider="ollama",
            model="text",
            context_window=8_000,
            tool_strategy=ModelToolStrategy.TEXT_ONLY,
            data_locality=DataLocality.LOCAL_ONLY,
        )
    )
    preset = CodingProviderPreset(
        id="ollama.text",
        provider_ref="ollama:text",
        provider="ollama",
        model="text",
        prompt_budget_tokens=1_000,
    )

    tool_result = check_provider_compatibility(
        preset,
        capabilities=capabilities,
        requirement=ProviderRequirement(requires_tools=True),
    )
    chat_result = check_provider_compatibility(
        preset,
        capabilities=capabilities,
        requirement=ProviderRequirement(),
    )
    assert {issue.code for issue in tool_result.issues} == {"model_tool_unsupported"}
    assert chat_result.compatible


def test_local_only_never_silently_routes_to_cloud() -> None:
    capabilities = ModelCapabilitiesRegistry()
    capabilities.register(
        ModelCapabilities(provider="openai", model="cloud", context_window=16_000)
    )
    preset = CodingProviderPreset(
        id="openai.cloud",
        provider_ref="openai:cloud",
        provider="openai",
        model="cloud",
        prompt_budget_tokens=1_000,
    )

    result = check_provider_compatibility(
        preset,
        capabilities=capabilities,
        requirement=ProviderRequirement(locality=DataLocality.LOCAL_ONLY),
    )
    assert {issue.code for issue in result.issues} == {"locality_violation"}


def test_unknown_usage_is_not_spoofed_as_zero() -> None:
    usage = RoleUsageSummary(role="coder")
    assert usage.input_tokens is None
    assert usage.total_tokens is None
    assert usage.cost_usd is None

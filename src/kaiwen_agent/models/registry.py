from __future__ import annotations

from collections.abc import Callable

from kaiwen_agent.models.base import ModelProvider

ModelProviderFactory = Callable[[str], ModelProvider]


class ProviderRegistry:
    """Maps stable provider references to host-owned provider factories."""

    def __init__(self) -> None:
        self._factories: dict[str, ModelProviderFactory] = {}

    def register(self, provider_ref: str, factory: ModelProviderFactory) -> None:
        if not provider_ref:
            raise ValueError("provider_ref must not be empty")
        if provider_ref in self._factories:
            raise ValueError(f"Provider already registered: {provider_ref}")
        self._factories[provider_ref] = factory

    def create(self, provider_ref: str, *, instructions: str) -> ModelProvider:
        try:
            factory = self._factories[provider_ref]
        except KeyError as error:
            raise KeyError(f"Unknown provider: {provider_ref}") from error
        return factory(instructions)

    def contains(self, provider_ref: str) -> bool:
        return provider_ref in self._factories

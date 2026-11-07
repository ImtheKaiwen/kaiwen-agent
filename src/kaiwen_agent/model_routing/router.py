from __future__ import annotations

from kaiwen_agent.model_routing.models import (
    ModelComplexity,
    ModelRoute,
    ModelSelection,
    ModelTier,
)
from kaiwen_agent.models.capabilities import DataLocality
from kaiwen_agent.policy import PolicyContext, PolicyDecisionType, PolicyEngine


class ModelRouteUnavailableError(LookupError):
    pass


class ModelRouter:
    """Resolves model identifiers; it never creates provider clients."""

    def __init__(self, *, routes: list[ModelRoute], policy_engine: PolicyEngine) -> None:
        ids = [route.id for route in routes]
        if len(ids) != len(set(ids)):
            raise ValueError("Model route ids must be unique")
        self.routes = tuple(routes)
        self.policy_engine = policy_engine

    async def resolve(
        self,
        *,
        capability: str,
        complexity: ModelComplexity,
        policy_context: PolicyContext,
        allowed_tiers: frozenset[ModelTier] | None = None,
        required_locality: DataLocality = DataLocality.CLOUD_ALLOWED,
        required_region: str | None = None,
    ) -> ModelSelection:
        selections = await self._resolve_candidates(
            capability=capability,
            complexity=complexity,
            policy_context=policy_context,
            allowed_tiers=allowed_tiers,
            required_locality=required_locality,
            required_region=required_region,
            first_only=True,
        )
        return selections[0]

    async def resolve_candidates(
        self,
        *,
        capability: str,
        complexity: ModelComplexity,
        policy_context: PolicyContext,
        allowed_tiers: frozenset[ModelTier] | None = None,
        required_locality: DataLocality = DataLocality.CLOUD_ALLOWED,
        required_region: str | None = None,
    ) -> tuple[ModelSelection, ...]:
        """Return every compatible, policy-approved route in priority order."""

        return await self._resolve_candidates(
            capability=capability,
            complexity=complexity,
            policy_context=policy_context,
            allowed_tiers=allowed_tiers,
            required_locality=required_locality,
            required_region=required_region,
            first_only=False,
        )

    async def _resolve_candidates(
        self,
        *,
        capability: str,
        complexity: ModelComplexity,
        policy_context: PolicyContext,
        allowed_tiers: frozenset[ModelTier] | None,
        required_locality: DataLocality,
        required_region: str | None,
        first_only: bool,
    ) -> tuple[ModelSelection, ...]:

        candidates = [
            route
            for route in self.routes
            if route.enabled
            and capability in route.capabilities
            and complexity in route.complexities
            and (allowed_tiers is None or route.tier in allowed_tiers)
            and locality_satisfies(
                route.data_locality,
                route.regions,
                required=required_locality,
                region=required_region,
            )
        ]
        candidates.sort(key=lambda route: route.priority, reverse=True)
        selections: list[ModelSelection] = []
        for route in candidates:
            decision = await self.policy_engine.evaluate(
                policy_context.model_copy(
                    update={
                        "action": "model.resolve",
                        "resource": route.id,
                        "metadata": {
                            **policy_context.metadata,
                            "capability": capability,
                            "complexity": complexity.value,
                            "provider": route.provider,
                            "model": route.model,
                            "tier": route.tier.value,
                            "data_locality": route.data_locality.value,
                            "required_locality": required_locality.value,
                            "required_region": required_region,
                        },
                    }
                )
            )
            if decision.decision != PolicyDecisionType.ALLOW:
                continue
            selections.append(
                ModelSelection(
                    route_id=route.id,
                    provider=route.provider,
                    model=route.model,
                    tier=route.tier,
                    capability=capability,
                    complexity=complexity,
                    data_locality=route.data_locality,
                    regions=route.regions,
                )
            )
            if first_only:
                return tuple(selections)
        if not selections:
            raise ModelRouteUnavailableError(
                f"No policy-allowed model route for {capability}/{complexity.value}"
            )
        return tuple(selections)


def locality_satisfies(
    locality: DataLocality,
    regions: frozenset[str],
    *,
    required: DataLocality,
    region: str | None,
) -> bool:
    if required == DataLocality.LOCAL_ONLY:
        return locality == DataLocality.LOCAL_ONLY
    if required == DataLocality.REGION_RESTRICTED:
        if locality == DataLocality.LOCAL_ONLY:
            return True
        if locality != DataLocality.REGION_RESTRICTED:
            return False
        return region is None or region in regions
    return True

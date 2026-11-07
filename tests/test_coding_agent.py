from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kaiwen_agent.agents import AgentDefinitionRegistry
from kaiwen_agent.capabilities import CapabilityRegistry
from kaiwen_agent.kits.coding import (
    CodingKitConfig,
    CodingKitFactory,
    CodingMode,
    CodingPlanningError,
    CodingProfile,
    ObjectivePlanner,
    PlanningConstraints,
    coding_profile_settings,
)
from kaiwen_agent.models import FakeModelProvider, ProviderRegistry
from kaiwen_agent.prompts import PromptRegistry
from kaiwen_agent.runtime import AgentFactory, RuntimeIdentity
from kaiwen_agent.standards import StandardsRegistry
from kaiwen_agent.tools.registry import ToolRegistry
from kaiwen_agent.types import ModelResponse


class QueuePlanner:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.corrections: list[str | None] = []

    async def propose_plan(
        self,
        *,
        objective: str,
        context: dict[str, Any],
        correction: str | None,
    ) -> Any:
        del objective, context
        self.corrections.append(correction)
        return self.responses.pop(0)


def _factory() -> AgentFactory:
    providers = ProviderRegistry()
    providers.register(
        "fake",
        lambda instructions: FakeModelProvider([ModelResponse(text="Merhaba")]),
    )
    return AgentFactory(
        agents=AgentDefinitionRegistry(),
        prompts=PromptRegistry(),
        capabilities=CapabilityRegistry(),
        providers=providers,
        tools=ToolRegistry(),
        standards=StandardsRegistry(),
    )


def test_single_coding_agent_composes_through_existing_runtime() -> None:
    async def scenario() -> None:
        factory = _factory()
        kit = CodingKitFactory(factory)
        config = CodingKitConfig(
            provider_ref="fake",
            profile=CodingProfile.FAST,
            mode=CodingMode.MANUAL,
        )
        agent = kit.create(config)
        result = await agent.chat("Türkçe yanıtla", identity=RuntimeIdentity(session_id="s1"))

        assert result.text == "Merhaba"
        assert agent.configured.definition.role == "solo"
        assert agent.configured.prompt.id == "coding.compact"
        assert agent.configured.profile.default_autonomy.value == "manual"
        assert factory.agents.get("coding.solo", 1) == agent.configured.definition
        assert kit.create(config).configured.pins == agent.configured.pins

    asyncio.run(scenario())


def test_preflight_is_actionable_and_has_no_side_effects() -> None:
    kit = CodingKitFactory(_factory())
    result = kit.validate_config(
        CodingKitConfig(provider_ref="missing", tool_refs=frozenset({"missing.tool"}))
    )

    assert {issue.code for issue in result.issues} == {
        "provider_not_registered",
        "tool_not_registered",
    }


def test_objective_planner_corrects_once_and_enforces_trusted_scope() -> None:
    objective = "Create a small CLI"
    valid = {
        "objective": objective,
        "tasks": [
            {
                "id": "implement",
                "name": "Implement",
                "worker_id": "solo",
                "resource_keys": ["repo:working-tree"],
                "capabilities": ["workspace.write"],
            }
        ],
    }
    backend = QueuePlanner([{"objective": "changed", "tasks": []}, valid])
    planner = ObjectivePlanner(
        backend,
        constraints=PlanningConstraints(
            worker_allowlist=frozenset({"solo"}),
            resource_allowlist=frozenset({"repo:working-tree"}),
            capability_allowlist=frozenset({"workspace.write"}),
        ),
    )

    plan = asyncio.run(planner.plan(objective, context={"project": "python"}))

    assert plan.objective == objective
    assert backend.corrections[0] is None
    assert backend.corrections[1]


def test_objective_planner_never_turns_unknown_goal_into_fixture() -> None:
    backend = QueuePlanner(
        [
            {
                "objective": "Todo fixture",
                "tasks": [{"id": "x", "name": "x", "worker_id": "solo"}],
            }
        ]
    )
    planner = ObjectivePlanner(
        backend,
        constraints=PlanningConstraints(worker_allowlist=frozenset({"solo"})),
        max_corrections=0,
    )

    with pytest.raises(CodingPlanningError, match="trusted objective"):
        asyncio.run(planner.plan("Legal research", context={}))


def test_profiles_change_method_not_quality_contract() -> None:
    fast = coding_profile_settings(CodingProfile.FAST)
    thorough = coding_profile_settings(CodingProfile.THOROUGH)

    assert fast.max_agent_steps < thorough.max_agent_steps
    assert fast.workflow_limits.max_cycles < thorough.workflow_limits.max_cycles

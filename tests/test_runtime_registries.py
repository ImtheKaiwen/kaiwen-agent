from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from kaiwen_agent.artifacts import (
    ArtifactContext,
    ArtifactCreate,
    ArtifactManager,
    ArtifactNotFoundError,
    ArtifactType,
)
from kaiwen_agent.budgets import (
    BudgetAction,
    BudgetContext,
    BudgetEngine,
    BudgetLimits,
    BudgetRule,
    BudgetScope,
    BudgetUsage,
    SQLiteBudgetStore,
)
from kaiwen_agent.capabilities import CapabilityManifest, CapabilityRegistry
from kaiwen_agent.model_routing import (
    ModelComplexity,
    ModelRoute,
    ModelRouter,
    ModelRouteUnavailableError,
    ModelTier,
)
from kaiwen_agent.policy import (
    PolicyContext,
    PolicyDecision,
    PolicyDecisionType,
    PolicyEngine,
    PolicyRegistry,
)
from kaiwen_agent.prompts import PromptDefinition, PromptRegistry, PromptRunRecord
from kaiwen_agent.skills import SkillDefinition, SkillRegistry, SkillStep


class ActionPolicy:
    policy_id = "test-policy"

    def __init__(
        self,
        decisions: dict[str, PolicyDecisionType],
        *,
        modifications: dict[str, object] | None = None,
    ) -> None:
        self.decisions = decisions
        self.modifications = modifications

    async def evaluate(self, context: PolicyContext) -> PolicyDecision | None:
        decision = self.decisions.get(context.action)
        if decision is None:
            return None
        return PolicyDecision(
            decision=decision,
            policy_id=self.policy_id,
            rule_id=context.action,
            reason="test",
            modifications=self.modifications,
        )


def policy_context(*, tenant_id: str = "tenant_1") -> PolicyContext:
    return PolicyContext(
        tenant_id=tenant_id,
        user_id="user_1",
        agent_id="agent_1",
        session_id="session_1",
        trace_id="trace_1",
        action="test",
    )


def budget_context(*, tenant_id: str = "tenant_1") -> BudgetContext:
    return BudgetContext(
        tenant_id=tenant_id,
        user_id="user_1",
        agent_id="agent_1",
        session_id="session_1",
        trace_id="trace_1",
        run_id="run_1",
        job_id="job_1",
    )


def test_model_router_is_policy_gated_and_provider_neutral() -> None:
    routes = [
        ModelRoute(
            id="strong",
            provider="provider-a",
            model="model-strong",
            tier=ModelTier.STRONG,
            capabilities=frozenset({"reasoning"}),
            complexities=frozenset({ModelComplexity.HIGH}),
            priority=10,
        ),
        ModelRoute(
            id="cheap",
            provider="provider-b",
            model="model-cheap",
            tier=ModelTier.CHEAP,
            capabilities=frozenset({"reasoning"}),
            complexities=frozenset({ModelComplexity.HIGH}),
        ),
    ]

    async def scenario() -> None:
        engine = PolicyEngine(
            registry=PolicyRegistry([ActionPolicy({"model.resolve": PolicyDecisionType.ALLOW})])
        )
        router = ModelRouter(routes=routes, policy_engine=engine)
        selected = await router.resolve(
            capability="reasoning",
            complexity=ModelComplexity.HIGH,
            policy_context=policy_context(),
            allowed_tiers=frozenset({ModelTier.CHEAP}),
        )
        assert selected.route_id == "cheap"
        denied = ModelRouter(routes=routes, policy_engine=PolicyEngine())
        with pytest.raises(ModelRouteUnavailableError):
            await denied.resolve(
                capability="reasoning",
                complexity=ModelComplexity.HIGH,
                policy_context=policy_context(),
            )

    asyncio.run(scenario())


def test_budget_engine_is_atomic_within_runtime_and_tenant_scoped() -> None:
    async def scenario() -> None:
        engine = BudgetEngine(
            rules=[
                BudgetRule(
                    id="tenant-tools",
                    scope=BudgetScope.TENANT,
                    limits=BudgetLimits(max_tool_calls=1),
                )
            ],
            policy_engine=PolicyEngine(),
        )
        first, second = await asyncio.gather(
            engine.consume(budget_context(), BudgetUsage(tool_calls=1)),
            engine.consume(budget_context(), BudgetUsage(tool_calls=1)),
        )
        assert sorted([first.allowed, second.allowed]) == [False, True]
        other_tenant = await engine.consume(
            budget_context(tenant_id="tenant_2"), BudgetUsage(tool_calls=1)
        )
        assert other_tenant.allowed

    asyncio.run(scenario())


def test_budget_overage_actions_and_sqlite_restart(tmp_path) -> None:
    async def scenario() -> None:
        rule = BudgetRule(
            id="run-tokens",
            scope=BudgetScope.RUN,
            limits=BudgetLimits(max_input_tokens=2),
        )
        degrade = BudgetEngine(
            rules=[rule],
            policy_engine=PolicyEngine(
                registry=PolicyRegistry(
                    [
                        ActionPolicy(
                            {"budget.exceeded": PolicyDecisionType.MODIFY},
                            modifications={"allowed_model_tiers": ["cheap"]},
                        )
                    ]
                )
            ),
        )
        outcome = await degrade.consume(budget_context(), BudgetUsage(input_tokens=3))
        assert outcome.action == BudgetAction.DEGRADE_MODEL
        assert not outcome.allowed
        assert outcome.allowed_model_tiers == frozenset({ModelTier.CHEAP})

        invalid_degrade = BudgetEngine(
            rules=[rule],
            policy_engine=PolicyEngine(
                registry=PolicyRegistry(
                    [
                        ActionPolicy(
                            {"budget.exceeded": PolicyDecisionType.MODIFY},
                            modifications={"allowed_model_tiers": ["unknown"]},
                        )
                    ]
                )
            ),
        )
        invalid = await invalid_degrade.consume(budget_context(), BudgetUsage(input_tokens=3))
        assert not invalid.allowed and invalid.action == BudgetAction.DENY

        path = tmp_path / "budgets.db"
        store = SQLiteBudgetStore(path)
        engine = BudgetEngine(rules=[rule], policy_engine=PolicyEngine(), store=store)
        assert (await engine.consume(budget_context(), BudgetUsage(input_tokens=2))).allowed
        restarted = BudgetEngine(
            rules=[rule], policy_engine=PolicyEngine(), store=SQLiteBudgetStore(path)
        )
        denied = await restarted.consume(budget_context(), BudgetUsage(input_tokens=1))
        assert not denied.allowed and denied.action == BudgetAction.DENY

    asyncio.run(scenario())


def test_sqlite_budget_reservation_is_atomic_across_engines(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "shared-budgets.db"
        rule = BudgetRule(
            id="tenant-job",
            scope=BudgetScope.TENANT,
            limits=BudgetLimits(max_jobs=1),
        )
        engines = [
            BudgetEngine(
                rules=[rule],
                policy_engine=PolicyEngine(),
                store=SQLiteBudgetStore(path),
            )
            for _ in range(2)
        ]
        outcomes = await asyncio.gather(
            *(engine.consume(budget_context(), BudgetUsage(jobs=1)) for engine in engines)
        )
        assert sorted(outcome.allowed for outcome in outcomes) == [False, True]

    asyncio.run(scenario())


def test_sqlite_budget_reservation_honors_limit_under_load(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "shared-budget-load.db"
        rule = BudgetRule(
            id="tenant-jobs-load",
            scope=BudgetScope.TENANT,
            limits=BudgetLimits(max_jobs=7),
        )
        engines = [
            BudgetEngine(
                rules=[rule],
                policy_engine=PolicyEngine(),
                store=SQLiteBudgetStore(path),
            )
            for _ in range(32)
        ]
        outcomes = await asyncio.gather(
            *(engine.consume(budget_context(), BudgetUsage(jobs=1)) for engine in engines)
        )
        assert sum(outcome.allowed for outcome in outcomes) == 7

        usage = await SQLiteBudgetStore(path).get_usage("tenant_1:tenant:tenant_1")
        assert usage.jobs == 7

    asyncio.run(scenario())


def test_prompt_registry_versions_rendering_and_run_metadata() -> None:
    registry = PromptRegistry()
    prompt = PromptDefinition(
        id="summary",
        version=1,
        template="Summarize {topic}",
        variables=frozenset({"topic"}),
    )
    registry.register(prompt)
    rendered = registry.render("summary", {"topic": "budgets"})
    assert rendered.text == "Summarize budgets"
    registry.record_run(
        PromptRunRecord(
            run_id="run_1",
            prompt_id="summary",
            prompt_version=1,
            prompt_checksum=prompt.checksum,
            model="model-a",
            toolset_version="1",
            policy_version="1",
            memory_policy_version="1",
        )
    )
    assert registry.get_run("run_1").prompt_checksum == prompt.checksum
    with pytest.raises(ValueError, match="already registered"):
        registry.register(prompt)
    with pytest.raises(ValueError, match="exactly"):
        registry.render("summary", {})


def test_skill_registry_validates_dag_and_compiles_declarative_tasks() -> None:
    registry = SkillRegistry()
    definition = SkillDefinition(
        id="research",
        version=1,
        variables=frozenset({"topic"}),
        steps=(
            SkillStep(
                id="collect",
                name="Collect",
                worker_agent_id="research-agent",
                description_template="Collect {topic}",
                required_tools=frozenset({"search"}),
                capabilities=frozenset({"research"}),
            ),
            SkillStep(
                id="write",
                name="Write",
                worker_agent_id="writer-agent",
                description_template="Write about {topic}",
                depends_on=("collect",),
            ),
        ),
    )
    registry.register(definition)
    graph = registry.compile(
        "research",
        {"topic": "runtime"},
        session_id="session_1",
        trace_id="trace_1",
        tenant_id="tenant_1",
        user_id="user_1",
    )
    tasks = {task.metadata["skill_step_id"]: task for task in graph.tasks()}
    assert tasks["write"].dependencies == [tasks["collect"].id]
    assert tasks["collect"].metadata["required_tools"] == ["search"]
    with pytest.raises(ValidationError, match="cycle"):
        SkillDefinition(
            id="bad",
            version=1,
            steps=(
                SkillStep(id="a", name="A", worker_agent_id="a", depends_on=("b",)),
                SkillStep(id="b", name="B", worker_agent_id="b", depends_on=("a",)),
            ),
        )


def test_artifact_manager_enforces_owner_size_checksum_and_expiry() -> None:
    async def scenario() -> None:
        manager = ArtifactManager(max_artifact_bytes=4)
        owner = ArtifactContext(
            tenant_id="tenant_1",
            user_id="user_1",
            session_id="session_1",
            trace_id="trace_1",
        )
        now = datetime(2026, 1, 1, tzinfo=UTC)
        metadata = await manager.create(
            owner,
            ArtifactCreate(
                type=ArtifactType.REPORT,
                mime_type="text/plain",
                content=b"safe",
                expires_at=now + timedelta(hours=1),
            ),
            now=now,
        )
        assert metadata.size_bytes == 4 and len(metadata.checksum_sha256) == 64
        assert (await manager.read(owner, metadata.id, now=now)).content == b"safe"
        with pytest.raises(ArtifactNotFoundError):
            await manager.read(
                ArtifactContext(
                    tenant_id="tenant_1",
                    user_id="user_2",
                    session_id="session_1",
                    trace_id="trace_1",
                ),
                metadata.id,
                now=now,
            )
        with pytest.raises(ValueError, match="size limit"):
            await manager.create(
                owner,
                ArtifactCreate(
                    type=ArtifactType.FILE,
                    mime_type="text/plain",
                    content=b"large",
                ),
            )
        with pytest.raises(ArtifactNotFoundError):
            await manager.read(owner, metadata.id, now=now + timedelta(hours=2))

    asyncio.run(scenario())


def test_capability_manifest_is_immutable_versioned_and_subset_aware() -> None:
    registry = CapabilityRegistry()
    manifest = CapabilityManifest(
        agent_id="admin-agent",
        version=1,
        tools=frozenset({"projects.read", "projects.write"}),
        model_tiers=frozenset({"cheap", "standard"}),
        delegates=frozenset({"research-agent"}),
    )
    registry.register(manifest)
    requested = CapabilityManifest(
        agent_id="child-agent",
        version=1,
        tools=frozenset({"projects.read"}),
        model_tiers=frozenset({"cheap"}),
    )
    assert manifest.contains(requested)
    assert registry.get("admin-agent") == manifest
    with pytest.raises(ValueError, match="already registered"):
        registry.register(manifest)

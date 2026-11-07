from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from kaiwen_agent import AgentContext, tool
from kaiwen_agent.agents import (
    AgentDefinition,
    AgentDefinitionRegistry,
    VersionedReference,
)
from kaiwen_agent.capabilities import CapabilityManifest, CapabilityRegistry
from kaiwen_agent.exceptions import ToolNotFoundError
from kaiwen_agent.models import ProviderRegistry
from kaiwen_agent.prompts import PromptDefinition, PromptRegistry
from kaiwen_agent.runtime import (
    AgentFactory,
    RuntimeCompositionError,
    RuntimeIdentity,
    WorkspaceProfile,
)
from kaiwen_agent.standards import (
    RequirementDefinition,
    RequirementObligation,
    StandardsPack,
    StandardsRegistry,
)
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.tools.registry import ToolRegistry
from kaiwen_agent.types import AgentInput, ModelResponse, ToolCall, ToolResult


class ToolInput(BaseModel):
    value: str


@tool(
    name="records.read",
    description="Read records",
    input_model=ToolInput,
    permissions={"records.read"},
)
async def read_records(context: AgentContext, arguments: ToolInput) -> dict[str, str]:
    return {"value": arguments.value, "agent": context.agent_id or ""}


@tool(
    name="records.write",
    description="Write records",
    input_model=ToolInput,
    permissions={"records.write"},
    side_effect="reversible",
    requires_approval=True,
)
async def write_records(context: AgentContext, arguments: ToolInput) -> dict[str, str]:
    return {"value": arguments.value, "agent": context.agent_id or ""}


class CapturingProvider:
    def __init__(self, instructions: str) -> None:
        self.instructions = instructions
        self.calls: list[dict[str, Any]] = []

    async def respond(
        self,
        agent_input: AgentInput,
        *,
        context: AgentContext,
        tools: Sequence[ToolDefinition],
        tool_results: Sequence[ToolResult],
        provider_state: Any | None,
    ) -> ModelResponse:
        self.calls.append({"context": context, "tools": tuple(tools)})
        if tools and not tool_results:
            return ModelResponse(
                tool_calls=[ToolCall(id="call_1", name=tools[0].name, arguments={"value": "ok"})]
            )
        return ModelResponse(text="done")


def ref(identifier: str, version: int = 1) -> VersionedReference:
    return VersionedReference(id=identifier, version=version)


def composition() -> tuple[AgentFactory, WorkspaceProfile, list[CapturingProvider]]:
    prompts = PromptRegistry()
    prompts.register(
        PromptDefinition(id="reader-prompt", version=1, template="Read only", variables=frozenset())
    )
    prompts.register(
        PromptDefinition(
            id="writer-prompt", version=1, template="Write safely", variables=frozenset()
        )
    )
    capabilities = CapabilityRegistry()
    capabilities.register(
        CapabilityManifest(
            agent_id="reader",
            version=1,
            tools=frozenset({"records.read"}),
            memory_namespaces=frozenset({"shared"}),
        )
    )
    capabilities.register(
        CapabilityManifest(
            agent_id="writer",
            version=1,
            tools=frozenset({"records.write"}),
            memory_namespaces=frozenset({"private"}),
        )
    )
    agents = AgentDefinitionRegistry(
        [
            AgentDefinition(
                id="reader",
                version=1,
                role="reader",
                instructions_ref=ref("reader-prompt"),
                provider_ref="fake",
                tool_refs=frozenset({"records.read"}),
                capability_manifest_ref=ref("reader"),
                memory_namespaces=frozenset({"shared"}),
                permissions=frozenset({"records.read"}),
            ),
            AgentDefinition(
                id="writer",
                version=1,
                role="writer",
                instructions_ref=ref("writer-prompt"),
                provider_ref="fake",
                tool_refs=frozenset({"records.write"}),
                capability_manifest_ref=ref("writer"),
                memory_namespaces=frozenset({"private"}),
                permissions=frozenset({"records.write"}),
            ),
        ]
    )
    created: list[CapturingProvider] = []
    providers = ProviderRegistry()

    def create_provider(instructions: str) -> CapturingProvider:
        provider = CapturingProvider(instructions)
        created.append(provider)
        return provider

    providers.register("fake", create_provider)
    profile = WorkspaceProfile(
        id="workspace",
        version=1,
        agent_refs=(ref("reader"), ref("writer")),
        default_agent_ref=ref("reader"),
        provider_allowlist=frozenset({"fake"}),
        tool_allowlist=frozenset({"records.read", "records.write"}),
    )
    factory = AgentFactory(
        agents=agents,
        prompts=prompts,
        capabilities=capabilities,
        providers=providers,
        tools=ToolRegistry([read_records, write_records]),
    )
    return factory, profile, created


def test_factory_isolates_agent_prompt_tools_memory_and_pins_versions() -> None:
    async def scenario() -> None:
        factory, profile, providers = composition()
        reader = factory.create(profile)
        writer = factory.create(profile, agent_ref=ref("writer"))
        assert providers[0].instructions == "Read only"
        assert providers[1].instructions == "Write safely"
        assert reader.pins.agent_version == 1
        assert reader.pins.prompt_checksum == reader.prompt.checksum
        assert reader.pins.tool_names == ("records.read",)
        assert writer.pins.tool_names == ("records.write",)
        assert reader.pins.toolset_checksum != writer.pins.toolset_checksum

        reader_result = await reader.runtime.action(
            "read",
            identity=RuntimeIdentity(
                session_id="reader-session", permissions=frozenset({"records.read"})
            ),
            tool_names=("records.read",),
        )
        assert reader_result.tool_results[0].output["agent"] == "reader"
        context = providers[0].calls[0]["context"]
        assert context.metadata["memory_namespaces"] == ["shared"]
        assert context.metadata["runtime_version_pins"]["agent_id"] == "reader"
        with pytest.raises(ToolNotFoundError, match="records.write"):
            await reader.runtime.action(
                "write",
                identity=RuntimeIdentity(
                    session_id="reader-session", permissions=frozenset({"records.write"})
                ),
                tool_names=("records.write",),
            )

    asyncio.run(scenario())


def test_definition_registry_is_immutable_versioned_and_rejects_duplicates() -> None:
    definition = AgentDefinition(
        id="agent",
        version=1,
        role="role",
        instructions_ref=ref("prompt"),
        provider_ref="fake",
        capability_manifest_ref=ref("agent"),
    )
    registry = AgentDefinitionRegistry([definition])
    assert registry.get("agent") == definition
    with pytest.raises(ValueError, match="already registered"):
        registry.register(definition)
    with pytest.raises(ValidationError):
        definition.version = 2  # type: ignore[misc]


def test_workspace_and_composition_fail_closed_for_invalid_references() -> None:
    factory, profile, _ = composition()
    with pytest.raises(RuntimeCompositionError, match="not allowed"):
        factory.create(profile, agent_ref=ref("reader", 2))
    with pytest.raises(ValidationError, match="default_agent_ref"):
        WorkspaceProfile(
            id="invalid",
            version=1,
            agent_refs=(ref("reader"),),
            default_agent_ref=ref("writer"),
            provider_allowlist=frozenset({"fake"}),
        )


def test_composition_rejects_capability_tool_and_permission_escalation() -> None:
    factory, profile, _ = composition()
    reader = factory.agents.get("reader")
    cases = (
        reader.model_copy(update={"tool_refs": frozenset({"records.write"})}),
        reader.model_copy(
            update={
                "tool_refs": frozenset({"records.read"}),
                "permissions": frozenset(),
            }
        ),
    )
    messages = ("capability manifest", "cannot execute")
    for definition, message in zip(cases, messages, strict=True):
        agents = AgentDefinitionRegistry([definition])
        invalid_factory = AgentFactory(
            agents=agents,
            prompts=factory.prompts,
            capabilities=factory.capabilities,
            providers=factory.providers,
            tools=factory.tools,
        )
        invalid_profile = profile.model_copy(
            update={"agent_refs": (ref("reader"),), "default_agent_ref": ref("reader")}
        )
        with pytest.raises(RuntimeCompositionError, match=message):
            invalid_factory.create(invalid_profile)

    missing_tool_definition = reader.model_copy(
        update={
            "tool_refs": frozenset({"records.missing"}),
            "permissions": frozenset({"records.missing"}),
        }
    )
    missing_capabilities = CapabilityRegistry()
    missing_capabilities.register(
        CapabilityManifest(
            agent_id="reader",
            version=1,
            tools=frozenset({"records.missing"}),
            memory_namespaces=frozenset({"shared"}),
        )
    )
    missing_factory = AgentFactory(
        agents=AgentDefinitionRegistry([missing_tool_definition]),
        prompts=factory.prompts,
        capabilities=missing_capabilities,
        providers=factory.providers,
        tools=factory.tools,
    )
    missing_profile = profile.model_copy(
        update={
            "agent_refs": (ref("reader"),),
            "default_agent_ref": ref("reader"),
            "tool_allowlist": frozenset({"records.missing"}),
        }
    )
    with pytest.raises(RuntimeCompositionError, match="Unknown tool"):
        missing_factory.create(missing_profile)


def test_provider_registry_requires_explicit_unique_registration() -> None:
    providers = ProviderRegistry()
    providers.register("fake", CapturingProvider)
    assert providers.contains("fake")
    assert providers.create("fake", instructions="safe").instructions == "safe"
    with pytest.raises(ValueError, match="already registered"):
        providers.register("fake", CapturingProvider)
    with pytest.raises(KeyError, match="Unknown provider"):
        providers.create("missing", instructions="safe")


def test_workspace_standards_are_exactly_resolved_and_pinned() -> None:
    factory, profile, _ = composition()
    standards_profile = profile.model_copy(update={"standards_pack_refs": (ref("quality"),)})
    with pytest.raises(RuntimeCompositionError, match="StandardsRegistry"):
        factory.create(standards_profile)

    standards = StandardsRegistry(
        [
            StandardsPack(
                id="quality",
                version=1,
                requirements=(
                    RequirementDefinition(
                        id="tests",
                        description="Tests pass",
                        obligation=RequirementObligation.MUST,
                        acceptance_criteria=("exit code is zero",),
                        verifier_ref="tests.verify",
                        evidence_types=frozenset({"test_report"}),
                    ),
                ),
            )
        ]
    )
    with_standards = AgentFactory(
        agents=factory.agents,
        prompts=factory.prompts,
        capabilities=factory.capabilities,
        providers=factory.providers,
        tools=factory.tools,
        standards=standards,
    )
    configured = with_standards.create(standards_profile)
    assert configured.standards_packs[0].id == "quality"
    assert configured.pins.standards_pack_refs == ("quality@1",)

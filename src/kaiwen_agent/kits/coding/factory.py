from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.agents import AgentDefinition, VersionedReference
from kaiwen_agent.capabilities import CapabilityManifest
from kaiwen_agent.kits.coding.profiles import CodingMode, CodingProfile, coding_profile_settings
from kaiwen_agent.kits.coding.prompting import install_coding_prompts
from kaiwen_agent.kits.coding.standards import install_coding_standards
from kaiwen_agent.runtime import (
    ConfiguredAgentRuntime,
    RuntimeCompositionError,
    RuntimeIdentity,
    RuntimeRunRecord,
    WorkspaceProfile,
)
from kaiwen_agent.standards import StandardResolution, StandardsRegistry
from kaiwen_agent.types import AgentResult
from kaiwen_agent.workflow import WorkflowController, WorkflowOutcome, WorkflowPlan


class CodingKitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default="coding.solo", min_length=1, max_length=200)
    version: int = Field(default=1, ge=1)
    provider_ref: str = Field(min_length=1, max_length=200)
    tool_refs: frozenset[str] = Field(default_factory=frozenset)
    permissions: frozenset[str] = Field(default_factory=frozenset)
    memory_namespaces: frozenset[str] = Field(default_factory=frozenset)
    standards_refs: tuple[VersionedReference, ...] = (
        VersionedReference(id="coding.foundation", version=1),
        VersionedReference(id="coding.tests", version=1),
    )
    profile: CodingProfile = CodingProfile.BALANCED
    mode: CodingMode = CodingMode.SUPERVISED


class CodingPreflightIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str


class CodingPreflightResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issues: tuple[CodingPreflightIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.issues


class CodingAgent:
    """Small public facade over one existing configured AgentRuntime."""

    def __init__(self, configured: ConfiguredAgentRuntime) -> None:
        self.configured = configured

    async def chat(self, text: str, *, identity: RuntimeIdentity) -> AgentResult:
        return await self.configured.runtime.chat(text, identity=identity)

    async def action(
        self,
        text: str,
        *,
        identity: RuntimeIdentity,
        tool_names: Sequence[str],
    ) -> AgentResult:
        return await self.configured.runtime.action(
            text,
            identity=identity,
            tool_names=tool_names,
        )

    async def execute_workflow(
        self,
        *,
        controller: WorkflowController,
        run: RuntimeRunRecord,
        identity: RuntimeIdentity,
        plan: WorkflowPlan,
        standards: StandardResolution,
        protected_input_fingerprint: str,
    ) -> WorkflowOutcome:
        """Use the platform workflow controller; the kit owns no execution loop."""

        return await controller.execute(
            run=run,
            identity=identity,
            plan=plan,
            standards=standards,
            protected_input_fingerprint=protected_input_fingerprint,
        )


class CodingKitFactory:
    """Installs immutable kit definitions and delegates composition to AgentFactory."""

    def __init__(self, agent_factory: object) -> None:
        from kaiwen_agent.runtime import AgentFactory

        if not isinstance(agent_factory, AgentFactory):
            raise TypeError("agent_factory must be an AgentFactory")
        self.agent_factory = agent_factory

    def validate_config(self, config: CodingKitConfig) -> CodingPreflightResult:
        issues: list[CodingPreflightIssue] = []
        if not self.agent_factory.providers.contains(config.provider_ref):
            issues.append(
                CodingPreflightIssue(
                    code="provider_not_registered",
                    message=f"Register provider_ref '{config.provider_ref}' before composition.",
                )
            )
        available_tools = {tool.name for tool in self.agent_factory.tools.definitions()}
        for name in sorted(config.tool_refs - available_tools):
            issues.append(
                CodingPreflightIssue(
                    code="tool_not_registered",
                    message=f"Register tool '{name}' before composition.",
                )
            )
        if config.standards_refs and self.agent_factory.standards is None:
            issues.append(
                CodingPreflightIssue(
                    code="verifier_not_registered",
                    message="Configure a StandardsRegistry for requested coding standards.",
                )
            )
        return CodingPreflightResult(issues=tuple(issues))

    def create(self, config: CodingKitConfig) -> CodingAgent:
        preflight = self.validate_config(config)
        if not preflight.ok:
            raise RuntimeCompositionError(
                "; ".join(f"{item.code}: {item.message}" for item in preflight.issues)
            )
        install_coding_prompts(self.agent_factory.prompts)
        if isinstance(self.agent_factory.standards, StandardsRegistry):
            install_coding_standards(self.agent_factory.standards)
        settings = coding_profile_settings(config.profile)
        prompt_id = "coding.compact" if settings.prompt_variant == "compact" else "coding.solo"
        definition = AgentDefinition(
            id=config.id,
            version=config.version,
            description="First-party opt-in single coding agent.",
            role="solo",
            instructions_ref=VersionedReference(id=prompt_id, version=1),
            provider_ref=config.provider_ref,
            tool_refs=config.tool_refs,
            capability_manifest_ref=VersionedReference(id=config.id, version=config.version),
            memory_namespaces=config.memory_namespaces,
            permissions=config.permissions,
            max_steps=settings.max_agent_steps,
        )
        manifest = CapabilityManifest(
            agent_id=config.id,
            version=config.version,
            tools=config.tool_refs,
            memory_namespaces=config.memory_namespaces,
        )
        _register_agent(self.agent_factory.agents, definition)
        _register_manifest(self.agent_factory.capabilities, manifest)
        profile = WorkspaceProfile(
            id=f"{config.id}.workspace",
            version=config.version,
            agent_refs=(VersionedReference(id=config.id, version=config.version),),
            default_agent_ref=VersionedReference(id=config.id, version=config.version),
            provider_allowlist=frozenset({config.provider_ref}),
            tool_allowlist=config.tool_refs,
            standards_pack_refs=config.standards_refs,
            default_autonomy=config.mode.autonomy,
        )
        return CodingAgent(self.agent_factory.create(profile))


def _register_agent(registry: object, definition: AgentDefinition) -> None:
    from kaiwen_agent.agents import AgentDefinitionRegistry

    if not isinstance(registry, AgentDefinitionRegistry):
        raise TypeError("registry must be an AgentDefinitionRegistry")
    try:
        existing = registry.get(definition.id, definition.version)
    except KeyError:
        registry.register(definition)
        return
    if existing != definition:
        raise ValueError(
            f"Conflicting coding agent definition: {definition.id}@{definition.version}"
        )


def _register_manifest(registry: object, manifest: CapabilityManifest) -> None:
    from kaiwen_agent.capabilities import CapabilityRegistry

    if not isinstance(registry, CapabilityRegistry):
        raise TypeError("registry must be a CapabilityRegistry")
    try:
        existing = registry.get(manifest.agent_id, manifest.version)
    except KeyError:
        registry.register(manifest)
        return
    if existing != manifest:
        raise ValueError(
            f"Conflicting coding capability manifest: {manifest.agent_id}@{manifest.version}"
        )

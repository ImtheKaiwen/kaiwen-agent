from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kaiwen_agent.agents import AgentDefinition, VersionedReference
from kaiwen_agent.capabilities import CapabilityManifest
from kaiwen_agent.kits.coding.discovery import CodingProjectSnapshot
from kaiwen_agent.kits.coding.factory import _register_agent, _register_manifest
from kaiwen_agent.kits.coding.prompting import install_coding_prompts
from kaiwen_agent.runtime import AgentFactory, ConfiguredAgentRuntime, WorkspaceProfile
from kaiwen_agent.teams import (
    TeamDefinition,
    TeamDefinitionRegistry,
    TeamMemberDefinition,
)

_CORE_ROLES = ("planner", "coder", "tester", "reviewer")
_OPTIONAL_ROLES = ("architect", "designer", "devops")
_ALL_ROLES = frozenset({"lead", *_CORE_ROLES, *_OPTIONAL_ROLES})


class CodingRoleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_ref: str = Field(min_length=1, max_length=200)
    tool_refs: frozenset[str] = Field(default_factory=frozenset)
    permissions: frozenset[str] = Field(default_factory=frozenset)
    capabilities: frozenset[str] = Field(default_factory=frozenset)
    max_steps: int = Field(default=10, ge=1, le=100)

    @model_validator(mode="after")
    def capabilities_do_not_grant_authority(self) -> CodingRoleConfig:
        if not self.capabilities <= self.permissions:
            raise ValueError("Role capabilities cannot exceed role permissions")
        return self


class CodingTeamConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default="coding.team", min_length=1, max_length=200)
    version: int = Field(default=1, ge=1)
    roles: Mapping[str, CodingRoleConfig]

    @model_validator(mode="after")
    def validate_roles(self) -> CodingTeamConfig:
        unknown = set(self.roles) - _ALL_ROLES
        missing = {"lead", *_CORE_ROLES} - set(self.roles)
        if unknown:
            raise ValueError(f"Unknown coding roles: {sorted(unknown)}")
        if missing:
            raise ValueError(f"Missing required coding roles: {sorted(missing)}")
        return self


class ConfiguredCodingTeam(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    definition: TeamDefinition
    runtimes: Mapping[str, ConfiguredAgentRuntime]
    active_roles: tuple[str, ...]
    write_execution_strategy: str = "serialized"


def select_coding_roles(snapshot: CodingProjectSnapshot) -> tuple[str, ...]:
    roles = list(_CORE_ROLES)
    if snapshot.condition_facts.get("is_multi_module") is True:
        roles.append("architect")
    if snapshot.condition_facts.get("has_ui") is True:
        roles.append("designer")
    if snapshot.condition_facts.get("uses_ci") is True:
        roles.append("devops")
    return tuple(roles)


class CodingTeamFactory:
    """Composes exact role runtimes; execution remains in TeamRuntime/JobManager."""

    def __init__(
        self,
        *,
        agent_factory: AgentFactory,
        teams: TeamDefinitionRegistry,
    ) -> None:
        self.agent_factory = agent_factory
        self.teams = teams

    def create(
        self,
        config: CodingTeamConfig,
        *,
        snapshot: CodingProjectSnapshot,
    ) -> ConfiguredCodingTeam:
        install_coding_prompts(self.agent_factory.prompts)
        active_roles = select_coding_roles(snapshot)
        role_names = ("lead", *active_roles)
        runtimes: dict[str, ConfiguredAgentRuntime] = {}
        references: dict[str, VersionedReference] = {}
        for role in role_names:
            role_config = config.roles[role]
            agent_id = f"{config.id}.{role}"
            reference = VersionedReference(id=agent_id, version=config.version)
            references[role] = reference
            definition = AgentDefinition(
                id=agent_id,
                version=config.version,
                description=f"First-party coding {role} role.",
                role=role,
                instructions_ref=VersionedReference(id=f"coding.{role}", version=1),
                provider_ref=role_config.provider_ref,
                tool_refs=role_config.tool_refs,
                capability_manifest_ref=reference,
                permissions=role_config.permissions,
                max_steps=role_config.max_steps,
            )
            manifest = CapabilityManifest(
                agent_id=agent_id,
                version=config.version,
                tools=role_config.tool_refs,
            )
            _register_agent(self.agent_factory.agents, definition)
            _register_manifest(self.agent_factory.capabilities, manifest)
            profile = WorkspaceProfile(
                id=f"{agent_id}.workspace",
                version=config.version,
                agent_refs=(reference,),
                default_agent_ref=reference,
                provider_allowlist=frozenset({role_config.provider_ref}),
                tool_allowlist=role_config.tool_refs,
            )
            runtimes[role] = self.agent_factory.create(profile)
        team_definition = TeamDefinition(
            id=config.id,
            version=config.version,
            description="Adaptive first-party coding team.",
            lead_agent_ref=references["lead"],
            members=tuple(
                TeamMemberDefinition(
                    role=role,
                    agent_ref=references[role],
                    capabilities=config.roles[role].capabilities,
                    may_delegate=False,
                )
                for role in active_roles
            ),
        )
        _register_team(self.teams, team_definition)
        return ConfiguredCodingTeam(
            definition=team_definition,
            runtimes=runtimes,
            active_roles=active_roles,
        )


def _register_team(registry: TeamDefinitionRegistry, definition: TeamDefinition) -> None:
    try:
        existing = registry.get(definition.id, definition.version)
    except KeyError:
        registry.register(definition)
        return
    if existing != definition:
        raise ValueError(
            f"Conflicting coding team definition: {definition.id}@{definition.version}"
        )

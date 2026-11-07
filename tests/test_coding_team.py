from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from kaiwen_agent.agents import AgentDefinitionRegistry
from kaiwen_agent.capabilities import CapabilityRegistry
from kaiwen_agent.integrations import WorkspaceFilesystem
from kaiwen_agent.kits.coding import (
    CodingRoleConfig,
    CodingTeamConfig,
    CodingTeamFactory,
    ProjectDiscovery,
    select_coding_roles,
)
from kaiwen_agent.models import FakeModelProvider, ProviderRegistry
from kaiwen_agent.prompts import PromptRegistry
from kaiwen_agent.runtime import AgentFactory
from kaiwen_agent.teams import TeamDefinitionRegistry
from kaiwen_agent.tools.registry import ToolRegistry
from kaiwen_agent.types import ModelResponse


def _role(*, write: bool = False) -> CodingRoleConfig:
    permission = "workspace.write" if write else "workspace.read"
    return CodingRoleConfig(
        provider_ref="fake",
        permissions=frozenset({permission}),
        capabilities=frozenset({permission}),
    )


def _config() -> CodingTeamConfig:
    return CodingTeamConfig(
        roles={
            "lead": _role(),
            "planner": _role(),
            "coder": _role(write=True),
            "tester": _role(),
            "reviewer": _role(),
            "architect": _role(),
            "designer": _role(),
            "devops": _role(write=True),
        }
    )


def _factory() -> AgentFactory:
    providers = ProviderRegistry()
    providers.register(
        "fake", lambda instructions: FakeModelProvider([ModelResponse(text=instructions[:8])])
    )
    return AgentFactory(
        agents=AgentDefinitionRegistry(),
        prompts=PromptRegistry(),
        capabilities=CapabilityRegistry(),
        providers=providers,
        tools=ToolRegistry(),
    )


def test_adaptive_team_uses_exact_roles_and_serializes_writes(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"dependencies":{"next":"16"},"scripts":{"test":"vitest"}}',
        encoding="utf-8",
    )
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("name: CI", encoding="utf-8")
    filesystem = WorkspaceFilesystem(workspace_id="w", roots={"repo": tmp_path})
    snapshot = ProjectDiscovery(filesystem).discover("repo")
    registry = TeamDefinitionRegistry()

    configured = CodingTeamFactory(agent_factory=_factory(), teams=registry).create(
        _config(), snapshot=snapshot
    )

    assert configured.active_roles == (
        "planner",
        "coder",
        "tester",
        "reviewer",
        "designer",
        "devops",
    )
    assert configured.write_execution_strategy == "serialized"
    assert set(configured.runtimes) == {"lead", *configured.active_roles}
    reviewer = next(member for member in configured.definition.members if member.role == "reviewer")
    assert reviewer.capabilities == frozenset({"workspace.read"})
    assert registry.get("coding.team", 1) == configured.definition


def test_role_selection_does_not_launch_optional_roles_for_python_cli(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='cli'", encoding="utf-8")
    snapshot = ProjectDiscovery(
        WorkspaceFilesystem(workspace_id="w", roots={"repo": tmp_path})
    ).discover("repo")

    assert select_coding_roles(snapshot) == ("planner", "coder", "tester", "reviewer")


def test_team_role_capabilities_cannot_escalate_permissions() -> None:
    with pytest.raises(ValidationError, match="cannot exceed"):
        CodingRoleConfig(
            provider_ref="fake",
            permissions=frozenset({"workspace.read"}),
            capabilities=frozenset({"workspace.write"}),
        )

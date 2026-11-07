from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kaiwen_agent.artifacts import (
    ArtifactContext,
    ArtifactManager,
    ArtifactNotFoundError,
    InMemoryArtifactStore,
)
from kaiwen_agent.integrations import WorkspaceFilesystem
from kaiwen_agent.kits.coding import (
    DocumentationTier,
    ProjectDiscovery,
    classify_documentation_tier,
    propose_documentation,
)


def _filesystem(root: Path, *, user_id: str = "user-1") -> WorkspaceFilesystem:
    return WorkspaceFilesystem(
        roots={"workspace": root},
        workspace_id="workspace",
        tenant_id="tenant",
        user_id=user_id,
    )


def test_empty_project_is_bounded_and_requires_no_unrelated_standards(tmp_path: Path) -> None:
    snapshot = ProjectDiscovery(_filesystem(tmp_path), max_files=10).discover("workspace")

    assert snapshot.file_count == 0
    assert snapshot.project.project_types == ("unknown",)
    assert snapshot.condition_facts["has_ui"] is False
    assert snapshot.condition_facts["has_database"] is False
    assert snapshot.sources == ()


def test_existing_typescript_project_preserves_docs_and_design_indicators(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts":{"test":"vitest"},"dependencies":{"next":"16.0.0"}}',
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# Existing", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("Existing rules", encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    (tmp_path / "src" / "components" / "ui").mkdir(parents=True)
    (tmp_path / "src" / "components" / "ui" / "button.tsx").write_text(
        "export const Button = () => null;", encoding="utf-8"
    )
    (tmp_path / "src" / "theme.css").write_text(":root {}", encoding="utf-8")

    snapshot = ProjectDiscovery(_filesystem(tmp_path)).discover("workspace")
    proposal = propose_documentation(snapshot, tier=DocumentationTier.L)

    assert "typescript" in snapshot.project.languages
    assert "next" in snapshot.project.frameworks
    assert snapshot.documentation_paths == ("README.md",)
    assert snapshot.instruction_paths == ("AGENTS.md",)
    assert "design_tokens" in snapshot.design_system_indicators
    assert "shared_ui_components" in snapshot.design_system_indicators
    readme = next(action for action in proposal.actions if action.path == "README.md")
    assert readme.action == "update"
    assert readme.template is None
    assert any(action.path == "docs/DESIGN.md" for action in proposal.actions)


def test_documentation_tiers_are_proportionate(tmp_path: Path) -> None:
    snapshot = ProjectDiscovery(_filesystem(tmp_path)).discover("workspace")

    assert classify_documentation_tier(estimated_changed_files=1) is DocumentationTier.S
    assert classify_documentation_tier(estimated_changed_files=3) is DocumentationTier.M
    assert (
        classify_documentation_tier(estimated_changed_files=1, architecture_change=True)
        is DocumentationTier.L
    )
    assert propose_documentation(snapshot, tier=DocumentationTier.S).actions == ()
    medium = propose_documentation(snapshot, tier=DocumentationTier.M)
    assert {action.path for action in medium.actions} == {
        "README.md",
        "docs/PLAN.md",
        "docs/QUALITY.md",
    }
    assert all(action.template for action in medium.actions)


def test_snapshot_fingerprint_changes_with_observed_files(tmp_path: Path) -> None:
    discovery = ProjectDiscovery(_filesystem(tmp_path))
    first = discovery.discover("workspace")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='sample'", encoding="utf-8")
    second = discovery.discover("workspace")

    assert first.fingerprint != second.fingerprint
    assert not discovery.is_current(first)
    assert discovery.is_current(second)


def test_snapshot_persistence_is_owner_scoped(tmp_path: Path) -> None:
    discovery = ProjectDiscovery(_filesystem(tmp_path))
    snapshot = discovery.discover("workspace")
    manager = ArtifactManager(store=InMemoryArtifactStore())
    owner = ArtifactContext(
        tenant_id="tenant",
        user_id="user-1",
        session_id="session-1",
        trace_id="trace-1",
        run_id="run-1",
    )
    other = ArtifactContext(
        tenant_id="tenant",
        user_id="user-2",
        session_id="session-2",
        trace_id="trace-2",
        run_id="run-1",
    )

    async def scenario() -> None:
        artifact_id = await discovery.persist(snapshot, artifacts=manager, context=owner)
        artifact = await manager.read(owner, artifact_id)
        assert artifact.metadata.labels["input_fingerprint"] == snapshot.fingerprint
        with pytest.raises(ArtifactNotFoundError):
            await manager.read(other, artifact_id)

    asyncio.run(scenario())

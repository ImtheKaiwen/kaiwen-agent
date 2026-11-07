from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from kaiwen_agent.context import AgentContext
from kaiwen_agent.exceptions import ToolExecutionError
from kaiwen_agent.integrations import WorkspaceFilesystem, WorkspacePathError
from kaiwen_agent.toolkits import (
    ProjectDetector,
    ReadOnlyGitRepository,
    ToolBundleDefinition,
    ToolBundleRegistry,
    git_read_tools,
    project_detect_tool,
    workspace_read_tools,
    workspace_search_tool,
)
from kaiwen_agent.tools import ToolExecutor
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.types import ToolCall


def _context(*, workspace_id: str = "workspace-1") -> AgentContext:
    return AgentContext(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        permissions=frozenset({"workspace.read", "git.read"}),
        metadata={"workspace_id": workspace_id},
    )


def _tool(tools: tuple[ToolDefinition, ...], name: str) -> ToolDefinition:
    return next(tool for tool in tools if tool.name == name)


def test_workspace_read_toolkit_is_bounded_and_excludes_secrets(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("first\nKaiwen\nlast\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=value", encoding="utf-8")
    (root / "private.pem").write_text("private", encoding="utf-8")
    filesystem = WorkspaceFilesystem(
        workspace_id="workspace-1",
        roots={"main": root},
        tenant_id="tenant-1",
        user_id="user-1",
    )
    read_tools = workspace_read_tools(filesystem)
    search = workspace_search_tool(filesystem)

    async def scenario() -> None:
        listing = await ToolExecutor().execute(
            _tool(read_tools, "workspace.list_files"),
            ToolCall(
                id="list-1",
                name="workspace.list_files",
                arguments={"root": "main", "path": "."},
            ),
            _context(),
            execution_scope="run-1",
        )
        paths = {entry["path"] for entry in listing.output["entries"]}
        assert "src/main.py" in paths
        assert ".env" not in paths
        assert "private.pem" not in paths

        ranged = await ToolExecutor().execute(
            _tool(read_tools, "workspace.read_lines"),
            ToolCall(
                id="read-1",
                name="workspace.read_lines",
                arguments={"root": "main", "path": "src/main.py", "start_line": 2, "end_line": 2},
            ),
            _context(),
            execution_scope="run-1",
        )
        assert ranged.output["content"] == "Kaiwen"

        result = await ToolExecutor().execute(
            search,
            ToolCall(
                id="search-1",
                name=search.name,
                arguments={"root": "main", "query": "kaiwen"},
            ),
            _context(),
            execution_scope="run-1",
        )
        assert result.output["matches"] == [{"path": "src/main.py", "line": 2, "preview": "Kaiwen"}]

    asyncio.run(scenario())


def test_workspace_toolkit_rejects_wrong_owner_and_unsafe_regex(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("aaaa", encoding="utf-8")
    filesystem = WorkspaceFilesystem(
        workspace_id="workspace-1",
        roots={"main": tmp_path},
        tenant_id="tenant-1",
        user_id="user-1",
    )
    listing = workspace_read_tools(filesystem)[0]
    search = workspace_search_tool(filesystem)

    async def scenario() -> None:
        with pytest.raises(ToolExecutionError) as owner_error:
            await ToolExecutor().execute(
                listing,
                ToolCall(
                    id="list-1",
                    name=listing.name,
                    arguments={"root": "main"},
                ),
                AgentContext(
                    session_id="session-1",
                    tenant_id="other-tenant",
                    user_id="user-1",
                    permissions=frozenset({"workspace.read"}),
                    metadata={"workspace_id": "workspace-1"},
                ),
                execution_scope="run-1",
            )
        assert isinstance(owner_error.value.__cause__, WorkspacePathError)

        with pytest.raises(ToolExecutionError) as regex_error:
            await ToolExecutor().execute(
                search,
                ToolCall(
                    id="search-1",
                    name=search.name,
                    arguments={"root": "main", "query": "(a+)+$", "regex": True},
                ),
                _context(),
                execution_scope="run-1",
            )
        assert isinstance(regex_error.value.__cause__, ValueError)
        assert "Nested quantified" in str(regex_error.value.__cause__)

    asyncio.run(scenario())


def test_workspace_listing_does_not_follow_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = root / "external"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are unavailable on this platform")
    filesystem = WorkspaceFilesystem(workspace_id="workspace-1", roots={"main": root})
    paths = {item["path"] for item in filesystem.list_files("main")["entries"]}
    assert "external" not in paths
    with pytest.raises(WorkspacePathError, match="escapes"):
        filesystem.read_text("main", "external/secret.txt")


def test_project_detector_reports_evidence_and_unknowns(tmp_path: Path) -> None:
    package = """{
      "scripts": {"test": "vitest", "build": "next build"},
      "dependencies": {"next": "1", "react": "1"},
      "devDependencies": {"typescript": "1"}
    }"""
    (tmp_path / "package.json").write_text(package, encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    filesystem = WorkspaceFilesystem(workspace_id="workspace-1", roots={"main": tmp_path})
    snapshot = ProjectDetector(filesystem).detect("main")
    assert snapshot.project_types == ("node",)
    assert snapshot.languages == ("typescript",)
    assert snapshot.frameworks == ("next", "react")
    assert snapshot.condition_facts["has_ui"] is True
    assert snapshot.condition_facts["has_database"] is None
    assert {item.path for item in snapshot.manifests} == {
        "package-lock.json",
        "package.json",
        "tsconfig.json",
    }

    async def scenario() -> None:
        tool = project_detect_tool(ProjectDetector(filesystem))
        result = await ToolExecutor().execute(
            tool,
            ToolCall(id="detect-1", name=tool.name, arguments={"root": "main"}),
            _context(),
            execution_scope="run-1",
        )
        assert result.output["workspace_id"] == "workspace-1"

    asyncio.run(scenario())


@pytest.mark.skipif(shutil.which("git") is None, reason="Git executable is unavailable")
def test_git_toolkit_is_read_only_bounded_and_rejects_argument_attacks(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / "tracked.txt").write_text("hello", encoding="utf-8")
    filesystem = WorkspaceFilesystem(workspace_id="workspace-1", roots={"repo": tmp_path})
    repository = ReadOnlyGitRepository(filesystem=filesystem, root="repo")
    tools = git_read_tools(repository)

    async def scenario() -> None:
        status = _tool(tools, "git.status")
        result = await ToolExecutor().execute(
            status,
            ToolCall(id="status-1", name="git.status", arguments={}),
            _context(),
            execution_scope="run-1",
        )
        assert "tracked.txt" in result.output["stdout"]
        assert all(tool.side_effect == "none" for tool in tools)
        assert all(tool.permissions == frozenset({"git.read"}) for tool in tools)

        diff = _tool(tools, "git.diff")
        with pytest.raises(ToolExecutionError) as revision_error:
            await ToolExecutor().execute(
                diff,
                ToolCall(
                    id="diff-1",
                    name="git.diff",
                    arguments={"revision": "--output=/tmp/exfiltrate"},
                ),
                _context(),
                execution_scope="run-1",
            )
        assert isinstance(revision_error.value.__cause__, ValueError)

        with pytest.raises(ToolExecutionError) as path_error:
            await ToolExecutor().execute(
                diff,
                ToolCall(
                    id="diff-2",
                    name="git.diff",
                    arguments={"path": "../outside.txt"},
                ),
                _context(),
                execution_scope="run-1",
            )
        assert isinstance(path_error.value.__cause__, WorkspacePathError)

    asyncio.run(scenario())


def test_tool_bundle_registry_is_idempotent_only_for_identical_definitions() -> None:
    inspect = ToolBundleDefinition(
        id="coding.inspect",
        version=1,
        tool_names=frozenset({"workspace.list_files", "workspace.search_text"}),
    )
    registry = ToolBundleRegistry([inspect])
    registry.register(inspect)
    assert registry.get("coding.inspect", 1) == inspect
    with pytest.raises(ValueError, match="Conflicting"):
        registry.register(
            inspect.model_copy(update={"tool_names": frozenset({"workspace.read_text"})})
        )

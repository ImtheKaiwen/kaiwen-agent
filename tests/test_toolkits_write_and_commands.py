from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path

import pytest

from kaiwen_agent.context import AgentContext
from kaiwen_agent.exceptions import ApprovalRequiredError, ToolExecutionError
from kaiwen_agent.integrations import WorkspaceFilesystem
from kaiwen_agent.toolkits import (
    CommandCatalog,
    CommandParameter,
    CommandPreset,
    CommandRunInput,
    FakeSandboxRunner,
    PatchConflictError,
    SandboxResult,
    SandboxUnavailableError,
    UnsupportedSandboxRunner,
    command_tool,
    workspace_write_tools,
)
from kaiwen_agent.toolkits import (
    testing_tools as build_testing_tools,
)
from kaiwen_agent.tools import ToolExecutor
from kaiwen_agent.tools.approval import StaticApprovalGate
from kaiwen_agent.types import ToolCall


def _context(*permissions: str) -> AgentContext:
    return AgentContext(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        permissions=frozenset(permissions),
        metadata={"workspace_id": "workspace-1"},
    )


def test_workspace_patch_requires_approval_and_detects_stale_preimage(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    original = "alpha\nbeta\n"
    target.write_text(original, encoding="utf-8")
    filesystem = WorkspaceFilesystem(workspace_id="workspace-1", roots={"main": tmp_path})
    patch_tool = workspace_write_tools(filesystem)[0]
    arguments = {
        "root": "main",
        "path": "file.txt",
        "expected_sha256": sha256(original.encode()).hexdigest(),
        "patch": "--- a/file.txt\n+++ b/file.txt\n@@ -1,2 +1,2 @@\n alpha\n-beta\n+gamma\n",
    }

    async def scenario() -> None:
        with pytest.raises(ApprovalRequiredError):
            await ToolExecutor().execute(
                patch_tool,
                ToolCall(id="patch-1", name=patch_tool.name, arguments=arguments),
                _context("workspace.write"),
                execution_scope="run-1",
            )
        assert target.read_text(encoding="utf-8") == original

        result = await ToolExecutor(approval_gate=StaticApprovalGate(True)).execute(
            patch_tool,
            ToolCall(id="patch-2", name=patch_tool.name, arguments=arguments),
            _context("workspace.write"),
            execution_scope="run-1",
        )
        assert target.read_text(encoding="utf-8") == "alpha\ngamma\n"
        assert result.output["previous_sha256"] == arguments["expected_sha256"]

        with pytest.raises(ToolExecutionError) as stale:
            await ToolExecutor(approval_gate=StaticApprovalGate(True)).execute(
                patch_tool,
                ToolCall(id="patch-3", name=patch_tool.name, arguments=arguments),
                _context("workspace.write"),
                execution_scope="run-1",
            )
        assert isinstance(stale.value.__cause__, PatchConflictError)
        assert target.read_text(encoding="utf-8") == "alpha\ngamma\n"

    asyncio.run(scenario())


def test_workspace_patch_rejects_multi_file_and_context_mismatch(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    original = "one\ntwo\n"
    target.write_text(original, encoding="utf-8")
    tool = workspace_write_tools(
        WorkspaceFilesystem(workspace_id="workspace-1", roots={"main": tmp_path})
    )[0]

    async def execute(patch: str, call_id: str) -> None:
        await ToolExecutor(approval_gate=StaticApprovalGate(True)).execute(
            tool,
            ToolCall(
                id=call_id,
                name=tool.name,
                arguments={
                    "root": "main",
                    "path": "file.txt",
                    "expected_sha256": sha256(original.encode()).hexdigest(),
                    "patch": patch,
                },
            ),
            _context("workspace.write"),
            execution_scope="run-1",
        )

    async def scenario() -> None:
        with pytest.raises(ToolExecutionError, match="failed"):
            await execute(
                "--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n-wrong\n+new\n",
                "patch-1",
            )
        with pytest.raises(ToolExecutionError):
            await execute(
                "--- a/file.txt\n+++ b/file.txt\n"
                "--- a/other.txt\n+++ b/other.txt\n@@ -1 +1 @@\n-one\n+new\n",
                "patch-2",
            )
        assert target.read_text(encoding="utf-8") == original

    asyncio.run(scenario())


def _catalog(tmp_path: Path, runner: FakeSandboxRunner) -> CommandCatalog:
    filesystem = WorkspaceFilesystem(workspace_id="workspace-1", roots={"main": tmp_path})
    return CommandCatalog(
        filesystem=filesystem,
        root="main",
        runner=runner,
        presets=(
            CommandPreset(
                id="pytest.target",
                category="test",
                executable="python",
                arguments=("-m", "pytest", "{target}"),
                parameters=(CommandParameter(name="target", pattern=r"^[A-Za-z0-9_./-]{1,100}$"),),
                timeout_seconds=30,
                max_output_bytes=1_000,
                network_enabled=False,
            ),
            CommandPreset(
                id="inspect.version",
                category="command",
                executable="python",
                arguments=("--version",),
                writable_workspace=False,
            ),
        ),
    )


def test_command_catalog_builds_exact_argv_and_redacts_output(tmp_path: Path) -> None:
    runner = FakeSandboxRunner(
        {
            "pytest.target": SandboxResult(
                exit_code=0,
                stdout="ok sk-abcdefghijklmnopqrstuvwxyz",
                duration_seconds=0.1,
            )
        }
    )
    catalog = _catalog(tmp_path, runner)
    tool = build_testing_tools(catalog, requires_approval=False)[0]

    async def scenario() -> None:
        result = await ToolExecutor().execute(
            tool,
            ToolCall(
                id="test-1",
                name=tool.name,
                arguments={"command_id": "pytest.target", "parameters": {"target": "tests/unit"}},
            ),
            _context("tests.run"),
            execution_scope="run-1",
        )
        assert result.output["exit_code"] == 0
        assert result.output["stdout"] == "ok [REDACTED]"
        assert runner.calls[0].argv == ("python", "-m", "pytest", "tests/unit")
        assert runner.calls[0].network_enabled is False

    asyncio.run(scenario())


def test_command_catalog_denies_injection_unknown_commands_and_wrong_category(
    tmp_path: Path,
) -> None:
    runner = FakeSandboxRunner(
        {
            "pytest.target": SandboxResult(exit_code=0),
            "inspect.version": SandboxResult(exit_code=0),
        }
    )
    catalog = _catalog(tmp_path, runner)
    test_tool = build_testing_tools(catalog, requires_approval=False)[0]

    async def scenario() -> None:
        with pytest.raises(ToolExecutionError):
            await ToolExecutor().execute(
                test_tool,
                ToolCall(
                    id="test-1",
                    name=test_tool.name,
                    arguments={
                        "command_id": "pytest.target",
                        "parameters": {"target": "tests;rm -rf workspace"},
                    },
                ),
                _context("tests.run"),
                execution_scope="run-1",
            )
        with pytest.raises(ToolExecutionError):
            await ToolExecutor().execute(
                test_tool,
                ToolCall(
                    id="test-2",
                    name=test_tool.name,
                    arguments={"command_id": "npm.install", "parameters": {}},
                ),
                _context("tests.run"),
                execution_scope="run-1",
            )
        with pytest.raises(ToolExecutionError):
            await ToolExecutor().execute(
                test_tool,
                ToolCall(
                    id="test-3",
                    name=test_tool.name,
                    arguments={"command_id": "inspect.version", "parameters": {}},
                ),
                _context("tests.run"),
                execution_scope="run-1",
            )
        assert runner.calls == []

    asyncio.run(scenario())


def test_general_command_requires_approval_and_unsupported_runner_fails_closed(
    tmp_path: Path,
) -> None:
    fake = FakeSandboxRunner({"inspect.version": SandboxResult(exit_code=0, stdout="Python")})
    catalog = _catalog(tmp_path, fake)
    tool = command_tool(catalog)

    async def scenario() -> None:
        with pytest.raises(ApprovalRequiredError):
            await ToolExecutor().execute(
                tool,
                ToolCall(
                    id="command-1",
                    name=tool.name,
                    arguments={"command_id": "inspect.version", "parameters": {}},
                ),
                _context("commands.execute"),
                execution_scope="run-1",
            )
        assert fake.calls == []

        unsupported = CommandCatalog(
            filesystem=WorkspaceFilesystem(
                workspace_id="workspace-1",
                roots={"main": tmp_path},
            ),
            root="main",
            runner=UnsupportedSandboxRunner(),
            presets=(catalog.get("inspect.version"),),
        )
        with pytest.raises(SandboxUnavailableError):
            await unsupported.execute(
                _context("commands.execute"),
                CommandRunInput(command_id="inspect.version"),
                category="command",
            )

    asyncio.run(scenario())


def test_fake_sandbox_cancellation_is_cooperative(tmp_path: Path) -> None:
    runner = FakeSandboxRunner(
        {"inspect.version": SandboxResult(exit_code=0)},
        delay_seconds=10,
    )
    catalog = _catalog(tmp_path, runner)

    async def scenario() -> None:
        task = asyncio.create_task(
            catalog.execute(
                _context("commands.execute"),
                CommandRunInput(command_id="inspect.version"),
                category="command",
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(runner.calls) == 1

    asyncio.run(scenario())

"""M6.4-A audit R2: directory containment, workspace identity, cancellation during capture.

The platform `DockerSandboxRunner` is the contract. The command workspace must equal the
configured root exactly (an equivalent-looking path is not trusted). The working directory must
resolve inside that root, contain no traversal component and be part of the captured snapshot. A
rejected command starts no process and records no evidence. Cancellation while a snapshot is being
captured or materialized in a worker thread must not leave a private copy behind. The runner is
not a sandbox.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest

from kaiwen_agent.artifacts import ArtifactContext, ArtifactManager
from kaiwen_agent.context import AgentContext
from kaiwen_agent.kits.coding import EvidenceBinding, EvidenceSigner, TrustedEvidencePipeline
from kaiwen_agent.toolkits import (
    CommandCatalog,
    CommandPreset,
    CommandRunInput,
    SandboxUnavailableError,
)
from kaiwen_agent.toolkits import snapshot as snapshot_module
from tests.test_coding_team_runner import MUTATE, SLEEP, Project, Spawned, run


def scratch_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    return scratch


# --------------------------------------------------------------------------- finding 1


@pytest.mark.parametrize(
    "parts", [("..", ".."), ("pkg", "..", ".."), ("..", "project"), ("pkg", "..")]
)
def test_a_traversal_in_the_working_directory_is_rejected_before_any_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parts: tuple[str, ...]
) -> None:
    scratch = scratch_directory(tmp_path, monkeypatch)

    async def scenario() -> None:
        project = Project(tmp_path)
        spawned = Spawned(monkeypatch)
        escaped = project.root.joinpath(*parts)
        command = project.command("print('ran')").model_copy(update={"working_directory": escaped})
        with pytest.raises(SandboxUnavailableError):
            await project.runner.run(command)
        assert spawned.processes == []  # nothing executed after the rejection

    run(scenario())
    assert list(scratch.iterdir()) == []  # and the private copies were removed


def test_the_working_directory_must_be_part_of_the_captured_snapshot(tmp_path: Path) -> None:
    project = Project(tmp_path)
    (project.root / "empty").mkdir()  # exists in the workspace but holds no snapshot file
    for name in ("empty", "missing"):
        command = project.command("print(1)").model_copy(
            update={"working_directory": project.root / name}
        )
        with pytest.raises(SandboxUnavailableError):
            run(project.runner.run(command))


def test_a_valid_nested_working_directory_still_runs_there(tmp_path: Path) -> None:
    project = Project(tmp_path)
    command = project.command("import os; print(os.path.basename(os.getcwd()))").model_copy(
        update={"working_directory": project.root / "pkg"}
    )
    result = run(project.runner.run(command))
    assert result.exit_code == 0 and result.stdout.strip() == "pkg"
    assert result.snapshot_unchanged is True


# --------------------------------------------------------------------------- finding 2


def test_a_command_for_another_workspace_is_rejected_before_any_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        project = Project(tmp_path)
        other = tmp_path / "other"
        other.mkdir()
        spawned = Spawned(monkeypatch)
        command = project.command("print('ran')").model_copy(
            update={"workspace": other, "working_directory": other}
        )
        with pytest.raises(SandboxUnavailableError):
            await project.runner.run(command)
        assert spawned.processes == []

    run(scenario())


def test_only_the_workspace_differs_is_enough_to_reject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        project = Project(tmp_path)
        other = tmp_path / "other"
        other.mkdir()
        spawned = Spawned(monkeypatch)
        # The directory is valid inside the runner's own root; only the stated workspace differs.
        command = project.command("print('ran')").model_copy(update={"workspace": other})
        with pytest.raises(SandboxUnavailableError):
            await project.runner.run(command)
        assert spawned.processes == []

    run(scenario())


def test_an_equivalent_looking_workspace_path_is_not_trusted(tmp_path: Path) -> None:
    project = Project(tmp_path)
    lookalike = Path(str(project.root) + os.sep + ".." + os.sep + project.root.name)
    assert lookalike.resolve() == project.root.resolve()  # the same place, spelled differently
    command = project.command("print(1)").model_copy(
        update={"workspace": lookalike, "working_directory": lookalike}
    )
    with pytest.raises(SandboxUnavailableError):  # the platform compares the configured path
        run(project.runner.run(command))


def test_the_exact_configured_workspace_runs(tmp_path: Path) -> None:
    project = Project(tmp_path)
    result = run(project.runner.run(project.command("print('ok')")))
    assert result.exit_code == 0 and result.stdout.strip() == "ok"


def test_a_rejected_execution_produces_no_evidence(tmp_path: Path) -> None:
    async def scenario() -> None:
        project = Project(tmp_path)
        other = Project(tmp_path / "other")  # the catalog is bound to another workspace
        artifacts = ArtifactManager()
        pipeline = TrustedEvidencePipeline.build(
            artifacts=artifacts,
            signer=EvidenceSigner(key=b"s" * 32),
            bindings=[
                EvidenceBinding(
                    command_id="unit.tests",
                    requirement_id="coding.tests.run",
                    evidence_type="test_run",
                )
            ],
            trusted_runners=[project.runner],
        )
        catalog = CommandCatalog(
            filesystem=other.filesystem,
            root="main",
            runner=project.runner,  # a misconfigured host: runner and catalog disagree
            presets=(
                CommandPreset(
                    id="unit.tests",
                    category="test",
                    executable="python",
                    arguments=("-c", "print('ok')"),
                    timeout_seconds=30,
                ),
            ),
            observers=(pipeline.recorder,),
        )
        context = AgentContext(
            tenant_id="local",
            user_id="local",
            session_id="s",
            trace_id="t",
            metadata={"runtime_run_id": "run-1", "workspace_id": "project"},
        )
        # Both workspaces share an id; only the roots differ, so the runner must refuse.
        with pytest.raises(SandboxUnavailableError):
            await catalog.execute(
                context, CommandRunInput(command_id="unit.tests", parameters={}), category="test"
            )
        scope = ArtifactContext(
            tenant_id="local", user_id="local", session_id="s", trace_id="t", run_id="run-1"
        )
        assert await artifacts.list(scope, limit=10) == []

    run(scenario())


# --------------------------------------------------------------------------- finding 3


class Held:
    """Holds a function inside its worker thread until the test releases it."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def wrap(self, real: Any) -> Any:
        def held(*args: Any, **kwargs: Any) -> Any:
            self.entered.set()
            assert self.release.wait(30)
            try:
                return real(*args, **kwargs)
            finally:
                self.finished.set()

        return held


async def settle(condition: Any, *, rounds: int = 2_000) -> None:
    """Yield to the loop (no timed sleep) until `condition()` holds."""

    for _ in range(rounds):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("the loop never reached the expected state")


@pytest.mark.parametrize("stage", ["capture", "materialize"])
@pytest.mark.parametrize("cancels", [1, 3], ids=["once", "repeated"])
def test_cancellation_while_a_snapshot_is_prepared_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, cancels: int
) -> None:
    scratch = scratch_directory(tmp_path, monkeypatch)

    async def scenario() -> None:
        project = Project(tmp_path)
        held = Held()
        if stage == "capture":
            project.builder.capture = held.wrap(project.builder.capture)  # type: ignore[method-assign]
        else:
            real = snapshot_module.ExecutionSnapshot.materialize
            monkeypatch.setattr(snapshot_module.ExecutionSnapshot, "materialize", held.wrap(real))
        spawned = Spawned(monkeypatch)
        pending = asyncio.ensure_future(project.runner.run(project.command(SLEEP)))
        assert await asyncio.to_thread(held.entered.wait, 30)  # the worker thread is inside
        for _ in range(cancels):
            pending.cancel()
        held.release.set()
        with pytest.raises(asyncio.CancelledError):  # cancellation is never turned into a result
            await pending
        assert await asyncio.to_thread(held.finished.wait, 30)  # the thread has completed
        await settle(lambda: not any(scratch.iterdir()))
        assert spawned.processes == []

    run(scenario())
    assert list(scratch.iterdir()) == []


def test_a_normal_run_after_a_cancelled_one_is_unaffected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch_directory(tmp_path, monkeypatch)

    async def scenario() -> None:
        project = Project(tmp_path)
        held = Held()
        real = project.builder.capture
        project.builder.capture = held.wrap(real)  # type: ignore[method-assign]
        pending = asyncio.ensure_future(project.runner.run(project.command("print(1)")))
        assert await asyncio.to_thread(held.entered.wait, 30)
        pending.cancel()
        held.release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        project.builder.capture = real  # type: ignore[method-assign]
        result = await project.runner.run(project.command("print('fine')"))
        assert result.exit_code == 0 and result.stdout.strip() == "fine"
        assert result.snapshot_unchanged is True and MUTATE  # a valid run keeps its behavior

    run(scenario())

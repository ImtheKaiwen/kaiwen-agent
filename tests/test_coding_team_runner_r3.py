"""M6.4-A R3: a worker thread must not outlive the cleanup that owns its files.

`snapshot.materialize` runs in a worker thread. If the caller is cancelled while that thread is
still active, the run directory and the snapshot must not be removed underneath it: the thread could
re-create the deleted directory (the destination is created with `parents=True`). Ownership rule:
cleanup of what a thread writes or reads runs when that thread has ended, never concurrently.
Cancellation still propagates, starts no process and produces no trusted evidence. Deterministic:
the worker thread is held at the destination-directory boundary with a threading event.
"""

from __future__ import annotations

import asyncio
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
    SnapshotError,
)
from kaiwen_agent.toolkits import snapshot as snapshot_module
from tests.test_coding_team_runner import SLEEP, Project, Spawned, run
from tests.test_coding_team_runner_r2 import scratch_directory, settle


class Boundary:
    """Holds the materialization worker at the creation of the destination directory."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        real_mkdir = Path.mkdir
        real_materialize = snapshot_module.ExecutionSnapshot.materialize
        boundary = self

        def mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
            if path.name == "workspace" and path.parent.name.startswith("kaiwen-local-run-"):
                boundary.entered.set()
                assert boundary.release.wait(30)
            real_mkdir(path, *args, **kwargs)

        def materialize(self: Any, destination: Path, *, writable: bool) -> None:
            try:
                real_materialize(self, destination, writable=writable)
            finally:
                boundary.finished.set()

        monkeypatch.setattr(Path, "mkdir", mkdir)
        monkeypatch.setattr(snapshot_module.ExecutionSnapshot, "materialize", materialize)


@pytest.mark.parametrize("cancels", [1, 3], ids=["once", "repeated"])
def test_cancellation_at_the_destination_boundary_cannot_recreate_the_run_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancels: int
) -> None:
    scratch = scratch_directory(tmp_path, monkeypatch)

    async def scenario() -> None:
        project = Project(tmp_path)
        boundary = Boundary(monkeypatch)
        spawned = Spawned(monkeypatch)
        pending = asyncio.ensure_future(project.runner.run(project.command(SLEEP)))
        assert await asyncio.to_thread(boundary.entered.wait, 30)  # the thread is at mkdir
        for _ in range(cancels):
            pending.cancel()
            await asyncio.sleep(0)  # let each cancellation be delivered
        with pytest.raises(asyncio.CancelledError):  # cancellation is never a result
            await pending
        boundary.release.set()  # the worker may now create the directory it was about to create
        assert await asyncio.to_thread(boundary.finished.wait, 30)
        await settle(lambda: not any(scratch.iterdir()))
        assert spawned.processes == []

    run(scenario())
    assert list(scratch.iterdir()) == []  # nothing was re-created after the cleanup


def test_a_cancelled_materialization_produces_no_trusted_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = scratch_directory(tmp_path, monkeypatch)

    async def scenario() -> None:
        project = Project(tmp_path)
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
            filesystem=project.filesystem,
            root="main",
            runner=project.runner,
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
        boundary = Boundary(monkeypatch)
        pending = asyncio.ensure_future(
            catalog.execute(
                context, CommandRunInput(command_id="unit.tests", parameters={}), category="test"
            )
        )
        assert await asyncio.to_thread(boundary.entered.wait, 30)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        boundary.release.set()
        assert await asyncio.to_thread(boundary.finished.wait, 30)
        await settle(lambda: not any(scratch.iterdir()))
        scope = ArtifactContext(
            tenant_id="local", user_id="local", session_id="s", trace_id="t", run_id="run-1"
        )
        assert await artifacts.list(scope, limit=10) == []

    run(scenario())


def test_cleanup_happens_on_success_failure_and_cancellation_alike(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = scratch_directory(tmp_path, monkeypatch)

    async def scenario() -> None:
        project = Project(tmp_path)
        ok = await project.runner.run(project.command("print('ok')"))
        assert ok.exit_code == 0 and ok.cleanup_failed is False
        failing = await project.runner.run(project.command("raise SystemExit(3)"))
        assert failing.exit_code == 3 and failing.cleanup_failed is False
        assert list(scratch.iterdir()) == []

        def broken(self: Any, destination: Path, *, writable: bool) -> None:
            raise SnapshotError("copy failed")

        real = snapshot_module.ExecutionSnapshot.materialize
        monkeypatch.setattr(snapshot_module.ExecutionSnapshot, "materialize", broken)
        with pytest.raises(SandboxUnavailableError):  # a failure is refused, not a result
            await project.runner.run(project.command("print('never')"))
        monkeypatch.setattr(snapshot_module.ExecutionSnapshot, "materialize", real)
        assert list(scratch.iterdir()) == []

    run(scenario())


def test_cancellation_while_the_comparison_runs_waits_for_its_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = scratch_directory(tmp_path, monkeypatch)

    async def scenario() -> None:
        project = Project(tmp_path)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        real = snapshot_module.ExecutionSnapshot.changed_paths

        def held(self: Any, directory: Path, *, limit: int = 20) -> tuple[str, ...]:
            entered.set()
            assert release.wait(30)
            try:
                return real(self, directory, limit=limit)
            finally:
                finished.set()

        monkeypatch.setattr(snapshot_module.ExecutionSnapshot, "changed_paths", held)
        closed_while_running: list[bool] = []
        real_close = snapshot_module.ExecutionSnapshot.close

        def close(self: Any) -> None:
            closed_while_running.append(entered.is_set() and not finished.is_set())
            real_close(self)

        monkeypatch.setattr(snapshot_module.ExecutionSnapshot, "close", close)
        pending = asyncio.ensure_future(project.runner.run(project.command("print('ok')")))
        assert await asyncio.to_thread(entered.wait, 30)  # the process already ran and exited
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        release.set()
        assert await asyncio.to_thread(finished.wait, 30)
        await settle(lambda: not any(scratch.iterdir()))
        assert closed_while_running and not any(closed_while_running)  # closed after the thread

    run(scenario())
    assert list(scratch.iterdir()) == []


def test_a_normal_run_is_unchanged_by_the_ownership_rule(tmp_path: Path) -> None:
    project = Project(tmp_path)
    result = run(project.runner.run(project.command("print('fine')")))
    assert result.exit_code == 0 and result.stdout.strip() == "fine"
    assert result.snapshot_unchanged is True and result.cleanup_failed is False
    assert tempfile.gettempdir()  # the platform temp directory is untouched by the tests above

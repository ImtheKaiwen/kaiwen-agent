"""M6.4-A: the example's `LocalSnapshotRunner` keeps the platform's execution-evidence contract.

The runner is NOT a sandbox. What it must still do, because the evidence pipeline and the completion
gate rely on it: run in a private, verified copy of the captured snapshot (never in the snapshot
itself); report `snapshot_unchanged` and `modified_paths` from a real comparison and fail closed
when that comparison is unavailable; return a timed-out result, and leave no process behind, on a
timeout; terminate its process and clean up on cancellation (which still propagates); and not
hand the host's environment (keys) to the project code. Deterministic: process creation is
observed, not slept on.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from kaiwen_agent.agents import VersionedReference
from kaiwen_agent.artifacts import ArtifactManager
from kaiwen_agent.context import AgentContext
from kaiwen_agent.integrations import WorkspaceFilesystem
from kaiwen_agent.kits.coding import EvidenceBinding, EvidenceSigner, TrustedEvidencePipeline
from kaiwen_agent.kits.coding.standards import coding_standards_packs
from kaiwen_agent.quality import CompletionGate, CompletionGateStatus, VerifierRegistry
from kaiwen_agent.quality.models import VerificationContext
from kaiwen_agent.standards import StandardsRegistry, StandardsResolver
from kaiwen_agent.toolkits import (
    CommandCatalog,
    CommandPreset,
    CommandRunInput,
    SandboxCommand,
    SnapshotBuilder,
)
from kaiwen_agent.toolkits import snapshot as snapshot_module

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "coding-team-acceptance" / "main.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("coding_team_acceptance_example_runner", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


example = load()

MUTATE = "open('pkg/calc.py', 'a').write('# changed during the run\\n')"
DELETE = "import os; os.remove('pkg/calc.py')"
CREATE = "open('pkg/new_file.py', 'w').write('x = 1\\n')"
SLEEP = "import time; time.sleep(300)"


def run(coroutine: Any) -> Any:
    async def bounded() -> Any:
        return await asyncio.wait_for(coroutine, timeout=60)

    return asyncio.run(bounded())


class Project:
    def __init__(self, base: Path) -> None:
        self.root = base / "project"
        (self.root / "pkg").mkdir(parents=True)
        (self.root / "pkg" / "__init__.py").write_text("")
        (self.root / "pkg" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        self.filesystem = WorkspaceFilesystem(workspace_id="project", roots={"main": self.root})
        self.builder = SnapshotBuilder(self.filesystem, "main")
        self.runner = example.LocalSnapshotRunner(self.builder)

    def command(self, code: str, *, timeout: float = 30) -> SandboxCommand:
        return SandboxCommand(
            command_id="probe",
            argv=("python", "-c", code),
            workspace=self.root,
            working_directory=self.root,
            timeout_seconds=timeout,
            max_output_bytes=10_000,
        )


class Spawned:
    """Observes the processes the runner starts, without changing how it starts them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.processes: list[Any] = []
        self.started = asyncio.Event()
        original = asyncio.create_subprocess_exec

        async def spy(*args: Any, **kwargs: Any) -> Any:
            process = await original(*args, **kwargs)
            self.processes.append(process)
            self.started.set()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)


# --------------------------------------------------------------------------- hypothesis 1


@pytest.mark.parametrize(
    ("code", "paths"),
    [(MUTATE, ["pkg/calc.py"]), (DELETE, ["pkg/calc.py"])],
    ids=["edit", "delete"],
)
def test_a_command_that_changes_a_snapshot_file_is_not_reported_unchanged(
    tmp_path: Path, code: str, paths: list[str]
) -> None:
    project = Project(tmp_path)
    result = run(project.runner.run(project.command(code)))
    assert result.exit_code == 0  # it "succeeded", yet it changed what it was asked to verify
    assert result.snapshot_unchanged is False
    assert list(result.modified_paths) == paths


def test_a_new_file_is_allowed_and_an_untouched_run_is_unchanged(tmp_path: Path) -> None:
    project = Project(tmp_path)
    created = run(project.runner.run(project.command(CREATE)))
    assert created.snapshot_unchanged is True and created.modified_paths == ()
    quiet = run(project.runner.run(project.command("print('ok')")))
    assert (
        quiet.exit_code == 0 and quiet.snapshot_unchanged is True and quiet.cleanup_failed is False
    )
    assert "ok" in quiet.stdout


def test_the_workspace_itself_is_never_modified_by_a_run(tmp_path: Path) -> None:
    project = Project(tmp_path)
    before = (project.root / "pkg" / "calc.py").read_text()
    run(project.runner.run(project.command(MUTATE)))
    assert (project.root / "pkg" / "calc.py").read_text() == before


def test_an_unverifiable_comparison_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project(tmp_path)

    def broken(self: Any, directory: Path, *, limit: int = 20) -> tuple[str, ...]:
        raise OSError("cannot compare")

    monkeypatch.setattr(snapshot_module.ExecutionSnapshot, "changed_paths", broken)
    result = run(project.runner.run(project.command("print('ok')")))
    assert result.exit_code == 0 and result.snapshot_unchanged is False  # not claimed unchanged


def test_the_real_gate_rejects_evidence_of_a_run_that_changed_the_snapshot(tmp_path: Path) -> None:
    async def scenario() -> tuple[CompletionGateStatus, CompletionGateStatus]:
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
        verifiers = VerifierRegistry()
        pipeline.install_verifiers(verifiers)
        standards = StandardsResolver(StandardsRegistry(coding_standards_packs())).resolve(
            [VersionedReference(id="coding.tests", version=1)],
            condition_facts={"has_executable_tests": True},
        )
        statuses = []
        for run_id, code in (("run-clean", "print('ok')"), ("run-dirty", MUTATE)):
            catalog = CommandCatalog(
                filesystem=project.filesystem,
                root="main",
                runner=project.runner,
                presets=(
                    CommandPreset(
                        id="unit.tests",
                        category="test",
                        executable="python",
                        arguments=("-c", code),
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
                metadata={"runtime_run_id": run_id, "workspace_id": "project"},
            )
            await catalog.execute(
                context, CommandRunInput(command_id="unit.tests", parameters={}), category="test"
            )
            verdict = await CompletionGate(verifiers=verifiers, artifacts=artifacts).evaluate(
                standards,
                context=VerificationContext(
                    tenant_id="local",
                    user_id="local",
                    session_id="s",
                    trace_id="t",
                    run_id=run_id,
                    protected_input_fingerprint=project.builder.fingerprint(),
                ),
            )
            statuses.append(verdict.status)
        return statuses[0], statuses[1]

    clean, dirty = run(scenario())
    assert clean is CompletionGateStatus.PASS  # the control: a genuine run passes
    assert dirty is not CompletionGateStatus.PASS  # a run that edited its snapshot never does


# --------------------------------------------------------------------------- hypothesis 2


def test_a_timeout_returns_a_timed_out_result_and_leaves_no_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        project = Project(tmp_path)
        spawned = Spawned(monkeypatch)
        result = await project.runner.run(project.command(SLEEP, timeout=0.5))
        assert result.timed_out is True and result.exit_code == 124
        assert result.cleanup_failed is False
        (process,) = spawned.processes
        assert process.returncode is not None  # terminated and reaped, not left running

    run(scenario())


def test_cancellation_terminates_the_process_and_still_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        project = Project(tmp_path)
        spawned = Spawned(monkeypatch)
        pending = asyncio.ensure_future(project.runner.run(project.command(SLEEP)))
        await spawned.started.wait()  # the process exists; no sleeping or guessing
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        (process,) = spawned.processes
        assert process.returncode is not None

    run(scenario())


def test_every_path_removes_the_private_run_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    async def scenario() -> None:
        project = Project(tmp_path)
        await project.runner.run(project.command("print('ok')"))
        await project.runner.run(project.command(MUTATE))
        await project.runner.run(project.command(SLEEP, timeout=0.5))
        spawned = Spawned(monkeypatch)
        pending = asyncio.ensure_future(project.runner.run(project.command(SLEEP)))
        await spawned.started.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

    run(scenario())
    assert list(scratch.iterdir()) == []


# ------------------------------------------------------------------ same-class hardening


def test_the_project_code_does_not_see_the_host_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KAIWEN_TEST_SECRET", "must-not-reach-the-child")
    project = Project(tmp_path)
    code = "import os; print(os.environ.get('KAIWEN_TEST_SECRET', 'absent'))"
    result = run(project.runner.run(project.command(code)))
    assert result.stdout.strip() == "absent"


def test_only_the_python_executable_runs_and_the_directory_must_stay_inside(
    tmp_path: Path,
) -> None:
    project = Project(tmp_path)
    other = project.command("print(1)").model_copy(update={"argv": ("sh", "-c", "echo hi")})
    with pytest.raises(ValueError):
        run(project.runner.run(other))
    outside = project.command("print(1)").model_copy(update={"working_directory": tmp_path})
    with pytest.raises(ValueError):
        run(project.runner.run(outside))

"""Real Docker integration tests. No fake engine is used here.

They run only when a Linux Docker engine is reachable and the pinned image can be made
available; otherwise they are skipped with an explicit reason. A skipped run proves
nothing about container isolation. Set `KAIWEN_DOCKER_TEST_IMAGE` to a digest-pinned
image reference to use a different image.
"""

from __future__ import annotations

import asyncio
import gc
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kaiwen_agent.agents import VersionedReference
from kaiwen_agent.artifacts import ArtifactContext, ArtifactManager
from kaiwen_agent.context import AgentContext
from kaiwen_agent.integrations import WorkspaceFilesystem
from kaiwen_agent.kits.coding.standards import coding_standards_packs
from kaiwen_agent.kits.coding.verification import (
    CommandEvidenceRecorder,
    EvidenceBinding,
    EvidenceSigner,
    install_coding_verifiers,
)
from kaiwen_agent.quality import (
    CompletionGate,
    CompletionGateStatus,
    VerificationContext,
    VerifierRegistry,
)
from kaiwen_agent.standards import StandardsRegistry, StandardsResolver
from kaiwen_agent.toolkits import (
    CommandCatalog,
    CommandPreset,
    CommandRunInput,
    DockerSandboxConfig,
    DockerSandboxRunner,
    SandboxUnavailableError,
)

# python:3.12-alpine, multi-architecture index digest
DEFAULT_IMAGE = "python@sha256:e064671ca452259bab50e8e2c7bd896e39e9a343bb06ca25234adf6fe18d0fc7"
IMAGE = os.environ.get("KAIWEN_DOCKER_TEST_IMAGE", DEFAULT_IMAGE)


def _docker_ready() -> tuple[bool, str]:
    docker = shutil.which("docker")
    if docker is None:
        return False, "docker executable not found"
    try:
        server = subprocess.run(
            [docker, "version", "--format", "{{.Server.Os}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "docker engine not reachable"
    if server.returncode != 0 or server.stdout.strip() != "linux":
        return False, "a linux docker engine is not running"
    present = subprocess.run([docker, "image", "inspect", IMAGE], capture_output=True)
    if present.returncode != 0:
        try:
            pulled = subprocess.run(
                [docker, "pull", "--quiet", IMAGE], capture_output=True, timeout=600
            )
        except (OSError, subprocess.TimeoutExpired):
            return False, "pinned test image could not be pulled"
        if pulled.returncode != 0:
            return False, "pinned test image could not be pulled"
    return True, ""


_READY, _REASON = _docker_ready()
# CI sets KAIWEN_REQUIRE_DOCKER=1 on Linux runners, where Docker must work: there the
# absence of an engine is a failure, not a skip.
if not _READY and os.environ.get("KAIWEN_REQUIRE_DOCKER") == "1":
    raise RuntimeError(f"KAIWEN_REQUIRE_DOCKER is set but Docker is unavailable: {_REASON}")
pytestmark = pytest.mark.skipif(not _READY, reason=f"Real Docker unavailable: {_REASON}")

TEST_SOURCE = "def add(a, b):\n    return a + b\n"
TEST_CASE = (
    "import unittest\n"
    "from pkg.calc import add\n\n"
    "class AddTests(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(add(2, 3), 5)\n\n"
    "if __name__ == '__main__':\n"
    "    unittest.main()\n"
)


def build_stack(
    tmp_path: Path,
    presets: tuple[CommandPreset, ...],
    files: dict[str, str] | None = None,
    **config: object,
):  # type: ignore[no-untyped-def]
    root = tmp_path / "project"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    # unittest discovery with a top-level directory needs an importable start package
    (root / "tests" / "__init__.py").write_text("")
    for name, content in (files or {}).items():
        (root / name).write_text(content)
    (root / "pkg" / "calc.py").write_text(TEST_SOURCE)
    (root / "tests" / "test_calc.py").write_text(TEST_CASE)
    filesystem = WorkspaceFilesystem(workspace_id="workspace-1", roots={"main": root})
    runner = DockerSandboxRunner(
        DockerSandboxConfig(image=IMAGE, **config),  # type: ignore[arg-type]
        filesystem=filesystem,
        root="main",
    )
    artifacts = ArtifactManager()
    signer = EvidenceSigner(key=b"integration-test-key-0123456789")
    recorder = CommandEvidenceRecorder(
        artifacts=artifacts,
        signer=signer,
        trusted_runners=[runner],
        bindings=[
            EvidenceBinding(
                command_id="unit.tests", requirement_id="coding.tests.run", evidence_type="test_run"
            )
        ],
    )

    def catalog_for(bound_runner: DockerSandboxRunner) -> CommandCatalog:
        return CommandCatalog(
            filesystem=filesystem,
            root="main",
            runner=bound_runner,
            presets=presets,
            observers=(recorder,),
        )

    registry = VerifierRegistry()
    install_coding_verifiers(registry, artifacts, signer=signer)
    return (
        root,
        runner,
        artifacts,
        catalog_for,
        CompletionGate(verifiers=registry, artifacts=artifacts),
    )


UNIT_TESTS = CommandPreset(
    id="unit.tests",
    category="test",
    executable="python",
    arguments=("-m", "unittest", "discover", "-s", "tests", "-t", "."),
    timeout_seconds=120,
)


def script(preset_id: str, filename: str, *, timeout: float = 60) -> CommandPreset:
    """A preset that runs a script file shipped inside the execution snapshot.

    Presets reject braces and other metacharacters in arguments on purpose, so probes
    live in files that are part of the snapshot rather than in `-c` strings.
    """

    return CommandPreset(
        id=preset_id,
        category="test",
        executable="python",
        arguments=(filename,),
        timeout_seconds=timeout,
    )


def context(run_id: str = "run-1") -> AgentContext:
    return AgentContext(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        trace_id="trace-1",
        permissions=frozenset({"tests.run"}),
        metadata={"workspace_id": "workspace-1", "runtime_run_id": run_id},
    )


def gate_context(fingerprint: str, run_id: str = "run-1") -> VerificationContext:
    return VerificationContext(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        trace_id="trace-1",
        run_id=run_id,
        protected_input_fingerprint=fingerprint,
    )


def resolution():  # type: ignore[no-untyped-def]
    return StandardsResolver(StandardsRegistry(coding_standards_packs())).resolve(
        [VersionedReference(id="coding.tests", version=1)],
        condition_facts={"has_executable_tests": True},
    )


def leftover_containers() -> list[str]:
    listing = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "label=kaiwen.sandbox=1"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return listing.stdout.split()


def test_real_container_run_produces_verifiable_evidence_and_detects_changes(
    tmp_path: Path,
) -> None:
    root, runner, artifacts, catalog_for, gate = build_stack(tmp_path, (UNIT_TESTS,))

    async def scenario() -> None:
        identity = await runner.verify()
        assert identity.image_id.startswith("sha256:")
        snapshot = runner.capture()
        catalog = catalog_for(runner.bound_to(snapshot))
        executed = await catalog.execute(
            context(), CommandRunInput(command_id="unit.tests"), category="test"
        )
        assert executed.exit_code == 0, executed.stderr
        assert "OK" in executed.stderr or "OK" in executed.stdout
        assert executed.snapshot_fingerprint == snapshot.fingerprint
        assert executed.image_id == identity.image_id and executed.artifact_refs
        evidence = await artifacts.read(
            ArtifactContext(
                tenant_id="tenant-1", user_id="user-1", session_id="s", trace_id="t", run_id="run-1"
            ),
            executed.artifact_refs[0],
        )
        assert json.loads(evidence.content)["outcome"] == "passed"
        passed = await gate.evaluate(resolution(), context=gate_context(snapshot.fingerprint))
        assert passed.status == CompletionGateStatus.PASS

        # Break the source: a new snapshot has a new fingerprint; old evidence is stale.
        (root / "pkg" / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        changed = runner.capture()
        assert changed.fingerprint != snapshot.fingerprint
        stale = await gate.evaluate(resolution(), context=gate_context(changed.fingerprint))
        assert stale.status == CompletionGateStatus.BLOCKED
        failing = await catalog_for(runner.bound_to(changed)).execute(
            context(), CommandRunInput(command_id="unit.tests"), category="test"
        )
        assert failing.exit_code != 0
        blocked = await gate.evaluate(resolution(), context=gate_context(changed.fingerprint))
        assert blocked.status == CompletionGateStatus.BLOCKED
        assert "command_failed" in blocked.verifications[0].findings
        snapshot.close()
        changed.close()

    asyncio.run(scenario())
    assert leftover_containers() == []
    assert (root / "pkg" / "calc.py").read_text().endswith("a - b\n")


def test_real_container_isolation_properties(tmp_path: Path) -> None:
    probe_source = (
        "import json, os, socket\n"
        "info = {'uid': os.getuid(), 'gid': os.getgid()}\n"
        "status = dict(line.split(':', 1) for line in open('/proc/self/status').read().splitlines()"
        " if ':' in line)\n"
        "info['cap_eff'] = status['CapEff'].strip()\n"
        "info['no_new_privs'] = status['NoNewPrivs'].strip()\n"
        "def attempt(action):\n"
        "    try:\n"
        "        action(); return True\n"
        "    except OSError:\n"
        "        return False\n"
        "info['network'] = attempt(lambda: socket.create_connection(('1.1.1.1', 53), timeout=3))\n"
        "info['write_root'] = attempt(lambda: open('/usr/kaiwen-probe', 'w'))\n"
        "info['write_tmp'] = attempt(lambda: open('/tmp/kaiwen-probe', 'w'))\n"
        "info['write_workspace'] = attempt(lambda: open('/workspace/created.txt', 'w'))\n"
        "info['workspace_files'] = sorted(os.listdir('/workspace'))\n"
        "info['env_keys'] = sorted(os.environ)\n"
        "info['docker_socket'] = os.path.exists('/var/run/docker.sock')\n"
        "print(json.dumps(info))\n"
    )
    root, runner, _artifacts, catalog_for, _gate = build_stack(
        tmp_path, (script("probe", "probe.py"),), files={"probe.py": probe_source}
    )
    (root / ".env").write_text("SECRET=host-secret")
    os.environ["KAIWEN_PROBE_SECRET"] = "must-not-be-forwarded"

    async def scenario() -> None:
        try:
            result = await catalog_for(runner).execute(
                context(), CommandRunInput(command_id="probe"), category="test"
            )
        finally:
            os.environ.pop("KAIWEN_PROBE_SECRET", None)
        assert result.exit_code == 0, result.stderr
        info = json.loads(result.stdout.strip().splitlines()[-1])
        expected_uid, expected_gid = (int(part) for part in runner.config.effective_user.split(":"))
        assert (info["uid"], info["gid"]) == (expected_uid, expected_gid)
        assert info["uid"] != 0
        assert int(info["cap_eff"], 16) == 0
        assert info["no_new_privs"] == "1"
        assert info["network"] is False
        assert info["write_root"] is False
        assert info["write_tmp"] is True
        assert info["write_workspace"] is True  # a private copy; the host is unchanged
        assert ".env" not in info["workspace_files"]
        assert "KAIWEN_PROBE_SECRET" not in info["env_keys"]
        assert info["docker_socket"] is False

    asyncio.run(scenario())
    assert not (root / "created.txt").exists()
    assert (root / ".env").read_text() == "SECRET=host-secret"
    assert leftover_containers() == []


def test_real_container_timeout_memory_and_cleanup(tmp_path: Path) -> None:
    _root, runner, _artifacts, catalog_for, _gate = build_stack(
        tmp_path,
        (script("sleeper", "sleeper.py", timeout=3), script("hog", "hog.py")),
        files={
            "sleeper.py": "import time\ntime.sleep(60)\n",
            "hog.py": "x = bytearray(600 * 1024 * 1024)\nprint(len(x))\n",
        },
        memory_mb=128,
    )

    unobserved: list[dict[str, object]] = []

    async def scenario() -> None:
        asyncio.get_running_loop().set_exception_handler(lambda _l, c: unobserved.append(c))
        catalog = catalog_for(runner)
        timed = await catalog.execute(
            context(), CommandRunInput(command_id="sleeper"), category="test"
        )
        assert timed.timed_out is True and timed.exit_code == 124
        assert timed.duration_seconds < 30
        killed = await catalog.execute(
            context(), CommandRunInput(command_id="hog"), category="test"
        )
        assert killed.exit_code != 0 and "600" not in killed.stdout

    asyncio.run(scenario())
    gc.collect()
    assert unobserved == [], "a reader task exception was never retrieved"
    assert leftover_containers() == []


def test_real_run_is_refused_for_a_missing_image_without_host_fallback(tmp_path: Path) -> None:
    missing = "python@sha256:" + "0" * 64
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.py").write_text("print('would run on the host')\n")
    runner = DockerSandboxRunner(
        DockerSandboxConfig(image=missing),
        filesystem=WorkspaceFilesystem(workspace_id="workspace-1", roots={"main": root}),
        root="main",
    )
    catalog = CommandCatalog(
        filesystem=runner.filesystem,
        root="main",
        runner=runner,
        presets=(script("p", "a.py"),),
    )

    async def scenario() -> None:
        with pytest.raises(SandboxUnavailableError):
            await catalog.execute(context(), CommandRunInput(command_id="p"), category="test")

    asyncio.run(scenario())


def test_real_container_modifying_snapshot_sources_never_passes(tmp_path: Path) -> None:
    """A command that rewrites tracked sources cannot earn evidence for the original state."""

    mutate = CommandPreset(
        id="unit.tests",
        category="test",
        executable="python",
        arguments=("mutate.py",),
        timeout_seconds=60,
    )
    root, runner, _artifacts, catalog_for, gate = build_stack(
        tmp_path,
        (mutate,),
        files={"mutate.py": "open('pkg/calc.py', 'a').write('# tampered during the run\\n')\n"},
    )

    async def scenario() -> None:
        snapshot = runner.capture()
        executed = await catalog_for(runner.bound_to(snapshot)).execute(
            context(), CommandRunInput(command_id="unit.tests"), category="test"
        )
        assert executed.exit_code == 0, executed.stderr
        assert executed.snapshot_unchanged is False
        assert executed.modified_paths == ("pkg/calc.py",)
        verdict = await gate.evaluate(resolution(), context=gate_context(snapshot.fingerprint))
        assert verdict.status == CompletionGateStatus.BLOCKED
        assert "command_snapshot_modified" in verdict.verifications[0].findings
        snapshot.close()

    asyncio.run(scenario())
    assert (root / "pkg" / "calc.py").read_text() == TEST_SOURCE  # the host copy is untouched
    assert leftover_containers() == []


LINUX_NON_ROOT = sys.platform.startswith("linux") and os.getuid() not in (0, 65534)  # type: ignore[attr-defined]


@pytest.mark.skipif(not LINUX_NON_ROOT, reason="needs a non-root Linux host user")
def test_real_container_created_restrictive_directories_are_removed(tmp_path: Path) -> None:
    """The run directory must not leak when the command creates owner-only directories."""

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    mkdirs = (
        "import os\n"
        "os.makedirs('out/deep')\n"
        "open('out/deep/file.txt', 'w').write('x')\n"
        "os.chmod('out/deep', 0o500)\n"
        "os.chmod('out', 0o500)\n"
    )
    root, runner, _artifacts, catalog_for, _gate = build_stack(
        tmp_path, (script("mkdirs", "mkdirs.py"),), files={"mkdirs.py": mkdirs}
    )
    runner._temporary_directory = scratch
    results = []

    async def scenario() -> None:
        catalog = catalog_for(runner)
        results.append(
            await catalog.execute(context(), CommandRunInput(command_id="mkdirs"), category="test")
        )

    asyncio.run(scenario())
    assert results[0].exit_code == 0, results[0].stderr
    assert results[0].cleanup_failed is False
    assert list(scratch.iterdir()) == [], "the temporary run directory was leaked"
    assert leftover_containers() == []


@pytest.mark.skipif(not LINUX_NON_ROOT, reason="needs a non-root Linux host user")
def test_real_container_with_a_foreign_uid_reports_a_failed_cleanup(tmp_path: Path) -> None:
    """Reproduces the ownership problem: a container uid that differs from the host user
    leaves directories the host cannot remove. The runner must say so, not claim success."""

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    mkdirs = "import os\nos.makedirs('out')\nopen('out/file.txt', 'w').write('x')\n"
    root, runner, _artifacts, catalog_for, _gate = build_stack(
        tmp_path,
        (script("mkdirs", "mkdirs.py"),),
        files={"mkdirs.py": mkdirs},
        user="65534:65534",
    )
    runner._temporary_directory = scratch
    results = []

    async def scenario() -> None:
        results.append(
            await catalog_for(runner).execute(
                context(), CommandRunInput(command_id="mkdirs"), category="test"
            )
        )

    try:
        asyncio.run(scenario())
        assert results[0].exit_code == 0, results[0].stderr
        assert results[0].cleanup_failed is True
        assert list(scratch.iterdir()), "the host should not have been able to remove it"
    finally:
        # Test-only cleanup of the files the foreign uid owns, as root inside a container.
        for leftover in scratch.iterdir():
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--pull=never",
                    "--user",
                    "0:0",
                    "--entrypoint",
                    "rm",
                    "--mount",
                    f"type=bind,source={scratch},target=/scratch",
                    IMAGE,
                    "-rf",
                    f"/scratch/{leftover.name}",
                ],
                capture_output=True,
                timeout=120,
            )
    assert leftover_containers() == []


def test_real_container_workflow_invalidates_evidence_when_the_coder_changes_source(
    tmp_path: Path,
) -> None:
    """A -> B -> C through the real controller, real containers and the signed gate."""

    from kaiwen_agent.control import RunControl
    from kaiwen_agent.kits.coding import WorkspaceFingerprintProvider
    from kaiwen_agent.persistence import SQLitePersistence
    from kaiwen_agent.runtime import (
        AutonomyLevel,
        DurableRunManager,
        RuntimeIdentity,
        RuntimeVersionPins,
    )
    from kaiwen_agent.tasks import TaskScheduler
    from kaiwen_agent.toolkits import SnapshotBuilder
    from kaiwen_agent.workers import FunctionWorker, WorkerRegistry
    from kaiwen_agent.workflow import (
        WorkflowCompiler,
        WorkflowController,
        WorkflowOutcomeStatus,
        WorkflowPlan,
        WorkflowTaskSpec,
        trusted_task_context,
    )

    root, runner, artifacts, catalog_for, gate = build_stack(tmp_path, (UNIT_TESTS,))
    catalog = catalog_for(runner)
    builder = SnapshotBuilder(runner.filesystem, "main")
    store = SQLitePersistence(tmp_path / "state.sqlite3")
    runs = DurableRunManager(store)
    owner = RuntimeIdentity(
        session_id="session-1", tenant_id="tenant-1", user_id="user-1", trace_id="trace-1"
    )
    pins = RuntimeVersionPins(
        workspace_id="workspace",
        workspace_version=1,
        agent_id="agent",
        agent_version=1,
        prompt_id="prompt",
        prompt_version=1,
        prompt_checksum="prompt-checksum",
        capability_manifest_id="agent",
        capability_manifest_version=1,
        provider_ref="fake",
        tool_names=(),
        toolset_checksum="toolset-checksum",
    )
    results: list[object] = []

    async def verify(task, task_context):  # type: ignore[no-untyped-def]
        agent_context = await trusted_task_context(
            task, runs=runs, permissions=frozenset({"tests.run"}), workspace_id="workspace-1"
        )
        result = await catalog.execute(
            agent_context, CommandRunInput(command_id="unit.tests"), category="test"
        )
        results.append(result)
        return {"exit_code": result.exit_code}

    async def edit(task, task_context):  # type: ignore[no-untyped-def]
        (root / "pkg" / "calc.py").write_text(str(task.input["content"]))
        return {}

    workers = WorkerRegistry()
    workers.register("tester", FunctionWorker(verify))
    workers.register("editor", FunctionWorker(edit))

    class Revisions:
        def __init__(self, *plans: WorkflowPlan) -> None:
            self.plans = list(plans)

        async def revise(self, request):  # type: ignore[no-untyped-def]
            return self.plans.pop(0) if self.plans else None

    def controller(revisions: Revisions) -> WorkflowController:
        return WorkflowController(
            runs=runs,
            compiler=WorkflowCompiler(
                workers=workers, worker_allowlist=frozenset({"tester", "editor"})
            ),
            scheduler_factory=lambda graph: TaskScheduler(
                graph=graph,
                workers=workers,
                store=store,
                control=RunControl(checkpoints=store),
            ),
            completion_gate=gate,
            revision_planner=revisions,
            fingerprint_provider=WorkspaceFingerprintProvider(builder),
        )

    def tester(task_id: str, *after: str) -> WorkflowTaskSpec:
        return WorkflowTaskSpec(
            id=task_id, name="Run tests", worker_id="tester", dependencies=after
        )

    def editor(task_id: str, content: str, *after: str) -> WorkflowTaskSpec:
        return WorkflowTaskSpec(
            id=task_id,
            name="Edit",
            worker_id="editor",
            dependencies=after,
            input={"content": content},
        )

    state_b = "def add(a, b):\n    return b + a\n"
    state_c = "def add(a, b):\n    return a - b\n"

    async def scenario() -> None:
        fingerprint_a = builder.fingerprint()
        run = await runs.create(
            identity=owner, autonomy=AutonomyLevel.AUTONOMOUS, version_pins=pins
        )
        revisions = Revisions(WorkflowPlan(objective="Ship", tasks=(tester("test-b"),)))
        first = await controller(revisions).execute(
            run=run,
            identity=owner,
            plan=WorkflowPlan(
                objective="Ship", tasks=(tester("test-a"), editor("to-b", state_b, "test-a"))
            ),
            standards=resolution(),
            protected_input_fingerprint="objective-1",
        )
        fingerprint_b = builder.fingerprint()
        assert fingerprint_a != fingerprint_b
        assert first.status == WorkflowOutcomeStatus.COMPLETED and first.cycles == 2
        assert first.verified_input_fingerprint == fingerprint_b
        assert [item.exit_code for item in results] == [0, 0]  # type: ignore[attr-defined]
        assert [item.snapshot_fingerprint for item in results] == [  # type: ignore[attr-defined]
            fingerprint_a,
            fingerprint_b,
        ]

        # State C breaks the tests: B's evidence cannot complete it, and C's failure cannot either.
        second_run = await runs.create(
            identity=owner, autonomy=AutonomyLevel.AUTONOMOUS, version_pins=pins
        )
        second = await controller(Revisions()).execute(
            run=second_run,
            identity=owner,
            plan=WorkflowPlan(
                objective="Break",
                tasks=(tester("test-b"), editor("to-c", state_c, "test-b")),
            ),
            standards=resolution(),
            protected_input_fingerprint="objective-2",
        )
        assert second.status == WorkflowOutcomeStatus.BLOCKED
        fingerprint_c = builder.fingerprint()
        assert fingerprint_c not in {item.snapshot_fingerprint for item in results[2:]}  # type: ignore[attr-defined]
        third_run = await runs.create(
            identity=owner, autonomy=AutonomyLevel.AUTONOMOUS, version_pins=pins
        )
        third = await controller(Revisions()).execute(
            run=third_run,
            identity=owner,
            plan=WorkflowPlan(objective="Test C", tasks=(tester("test-c"),)),
            standards=resolution(),
            protected_input_fingerprint="objective-3",
        )
        assert third.status == WorkflowOutcomeStatus.BLOCKED  # C is tested and fails
        assert results[-1].exit_code != 0  # type: ignore[attr-defined]
        assert results[-1].snapshot_fingerprint == fingerprint_c  # type: ignore[attr-defined]

    asyncio.run(scenario())
    assert leftover_containers() == []

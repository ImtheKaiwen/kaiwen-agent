"""Real Docker integration tests. No fake engine is used here.

They run only when a Linux Docker engine is reachable and the pinned image can be made
available; otherwise they are skipped with an explicit reason. A skipped run proves
nothing about container isolation. Set `KAIWEN_DOCKER_TEST_IMAGE` to a digest-pinned
image reference to use a different image.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
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


def build_stack(tmp_path: Path, presets: tuple[CommandPreset, ...], **config: object):  # type: ignore[no-untyped-def]
    root = tmp_path / "project"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
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


def probe(preset_id: str, code: str, *, timeout: float = 60) -> CommandPreset:
    return CommandPreset(
        id=preset_id,
        category="test",
        executable="python",
        arguments=("-c", code),
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
    code = (
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
    root, runner, _artifacts, catalog_for, _gate = build_stack(tmp_path, (probe("probe", code),))
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
        assert (info["uid"], info["gid"]) == (65534, 65534)
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
    sleeper = probe("sleeper", "import time; time.sleep(60)", timeout=3)
    hog = probe("hog", "x = bytearray(600 * 1024 * 1024); print(len(x))", timeout=60)
    _root, runner, _artifacts, catalog_for, _gate = build_stack(
        tmp_path, (sleeper, hog), memory_mb=128
    )

    async def scenario() -> None:
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
        presets=(probe("p", "print(1)"),),
    )

    async def scenario() -> None:
        with pytest.raises(SandboxUnavailableError):
            await catalog.execute(context(), CommandRunInput(command_id="p"), category="test")

    asyncio.run(scenario())

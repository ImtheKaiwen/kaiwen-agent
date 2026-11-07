"""Run real project tests in a Docker sandbox and turn the result into verified evidence.

Requires a running Linux Docker engine and a locally available, digest-pinned image
(`docker pull <image>`). Without an engine the example reports why and exits cleanly;
it never runs the tests on the host. Set KAIWEN_REQUIRE_DOCKER=1 to make an unavailable
engine a failure, and KAIWEN_DOCKER_IMAGE to use another pinned image. A real run that
does not produce a passing gate and then a non-passing gate exits with an error.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from kaiwen_agent.agents import VersionedReference
from kaiwen_agent.artifacts import ArtifactManager
from kaiwen_agent.context import AgentContext
from kaiwen_agent.integrations import WorkspaceFilesystem
from kaiwen_agent.kits.coding.standards import coding_standards_packs
from kaiwen_agent.kits.coding.verification import (
    CommandEvidenceRecorder,
    EvidenceBinding,
    EvidenceSigner,
    install_coding_verifiers,
)
from kaiwen_agent.quality import CompletionGate, VerificationContext, VerifierRegistry
from kaiwen_agent.standards import StandardsRegistry, StandardsResolver
from kaiwen_agent.toolkits import (
    CommandCatalog,
    CommandPreset,
    CommandRunInput,
    DockerSandboxConfig,
    DockerSandboxRunner,
    SandboxUnavailableError,
)

IMAGE = os.environ.get(
    "KAIWEN_DOCKER_IMAGE",
    "python@sha256:e064671ca452259bab50e8e2c7bd896e39e9a343bb06ca25234adf6fe18d0fc7",
)
RUN_ID = "example-run"


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="kaiwen-docker-example-") as directory:
        root = Path(directory) / "project"
        (root / "pkg").mkdir(parents=True)
        (root / "tests").mkdir()
        # `unittest discover -s tests -t .` needs `tests` to be an importable package.
        (root / "tests" / "__init__.py").write_text("")
        (root / "pkg" / "__init__.py").write_text("")
        (root / "pkg" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (root / "tests" / "test_calc.py").write_text(
            "import unittest\nfrom pkg.calc import add\n\n"
            "class AddTests(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(add(2, 3), 5)\n"
        )

        # Trusted host composition: the model can only choose this preset by id.
        filesystem = WorkspaceFilesystem(workspace_id="example", roots={"main": root})
        runner = DockerSandboxRunner(
            DockerSandboxConfig(image=IMAGE), filesystem=filesystem, root="main"
        )
        artifacts = ArtifactManager()
        signer = EvidenceSigner(key=os.urandom(32))
        registry = VerifierRegistry()
        install_coding_verifiers(registry, artifacts, signer=signer)
        gate = CompletionGate(verifiers=registry, artifacts=artifacts)
        recorder = CommandEvidenceRecorder(
            artifacts=artifacts,
            signer=signer,
            trusted_runners=[runner],
            bindings=[
                EvidenceBinding(
                    command_id="unit.tests",
                    requirement_id="coding.tests.run",
                    evidence_type="test_run",
                )
            ],
        )
        preset = CommandPreset(
            id="unit.tests",
            category="test",
            executable="python",
            arguments=("-m", "unittest", "discover", "-s", "tests", "-t", "."),
            timeout_seconds=120,
        )
        standards = StandardsResolver(StandardsRegistry(coding_standards_packs())).resolve(
            [VersionedReference(id="coding.tests", version=1)],
            condition_facts={"has_executable_tests": True},
        )
        context = AgentContext(
            session_id="example",
            tenant_id="local",
            user_id="local",
            trace_id="example-trace",
            permissions=frozenset({"tests.run"}),
            metadata={"workspace_id": "example", "runtime_run_id": RUN_ID},
        )

        async def verdict(fingerprint: str) -> str:
            result = await gate.evaluate(
                standards,
                context=VerificationContext(
                    tenant_id="local",
                    user_id="local",
                    session_id="example",
                    trace_id="example-trace",
                    run_id=RUN_ID,
                    protected_input_fingerprint=fingerprint,
                ),
            )
            return result.status.value

        try:
            await runner.verify()
        except SandboxUnavailableError as error:
            print(f"Docker sandbox unavailable ({error}); nothing was executed.")
            if os.environ.get("KAIWEN_REQUIRE_DOCKER") == "1":
                raise SystemExit("KAIWEN_REQUIRE_DOCKER is set, so this is a failure") from error
            return

        snapshot = runner.capture()
        catalog = CommandCatalog(
            filesystem=filesystem,
            root="main",
            runner=runner.bound_to(snapshot),
            presets=(preset,),
            observers=(recorder,),
        )
        executed = await catalog.execute(
            context, CommandRunInput(command_id="unit.tests"), category="test"
        )
        print("exit code:", executed.exit_code, "| snapshot:", snapshot.fingerprint[:12])
        executed_verdict = await verdict(snapshot.fingerprint)
        print("gate for the executed snapshot:", executed_verdict)

        # Any source change gives a new fingerprint, so earlier evidence no longer counts.
        (root / "pkg" / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        with runner.capture() as changed:
            changed_verdict = await verdict(changed.fingerprint)
        print("gate after a source change:", changed_verdict)
        snapshot.close()

        # A real run that does not behave as documented must fail loudly.
        problems = []
        if executed.exit_code != 0:
            problems.append(f"tests exited with {executed.exit_code}: {executed.stderr[-500:]}")
        if executed_verdict != "pass":
            problems.append(f"expected a passing gate, got {executed_verdict}")
        if changed_verdict == "pass":
            problems.append("a changed source must not pass on old evidence")
        if executed.cleanup_failed is not False:
            problems.append("the sandbox could not remove its temporary run directory")
        if problems:
            raise SystemExit("Docker evidence example failed: " + "; ".join(problems))


if __name__ == "__main__":
    asyncio.run(main())

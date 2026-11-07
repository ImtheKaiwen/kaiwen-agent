"""Run a configured AgentRuntime as a workflow worker that executes real tests in Docker.

A deterministic scripted model (no external LLM) asks the worker's `testing.run` tool to run
the project's tests. The tool executes in a Docker container over a snapshot, the signed
evidence pipeline records the result, and the workflow's completion gate decides, using a
fresh fingerprint of the workspace and the durable run's workspace binding. In the second run
the source is broken first, so the model's confident "all tests pass" changes nothing: the gate
blocks. Requires a running Linux Docker engine and a local, digest-pinned image; without one
the example says so and never runs anything on the host (KAIWEN_REQUIRE_DOCKER=1 makes that a
failure, KAIWEN_DOCKER_IMAGE changes the image). A run that does not behave as described exits
with an error. This is a single-worker demonstration, not a multi-agent team.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

from kaiwen_agent.agents import VersionedReference
from kaiwen_agent.artifacts import ArtifactManager
from kaiwen_agent.control import RunControl
from kaiwen_agent.integrations import WorkspaceFilesystem
from kaiwen_agent.kits.coding import (
    AuthorizedCodingWorkspace,
    EvidenceBinding,
    EvidenceSigner,
    TrustedEvidencePipeline,
    WorkspaceFingerprintProvider,
)
from kaiwen_agent.kits.coding.standards import coding_standards_packs
from kaiwen_agent.models.fake import FakeModelProvider
from kaiwen_agent.persistence import SQLitePersistence
from kaiwen_agent.quality import CompletionGate, VerifierRegistry
from kaiwen_agent.runtime import (
    AgentRuntime,
    AutonomyLevel,
    DurableRunManager,
    RuntimeIdentity,
    RuntimeVersionPins,
)
from kaiwen_agent.standards import StandardsRegistry, StandardsResolver
from kaiwen_agent.tasks import AgentTask, TaskScheduler
from kaiwen_agent.toolkits import (
    CommandCatalog,
    CommandPreset,
    DockerSandboxConfig,
    DockerSandboxRunner,
    SandboxUnavailableError,
    SnapshotBuilder,
    testing_tools,
)
from kaiwen_agent.tools import ToolExecutor
from kaiwen_agent.types import ModelResponse, ToolCall
from kaiwen_agent.workers import FunctionWorker, TaskExecutionContext, WorkerRegistry
from kaiwen_agent.workflow import (
    AgentRuntimeWorker,
    WorkflowCompiler,
    WorkflowController,
    WorkflowOutcomeStatus,
    WorkflowPlan,
    WorkflowTaskSpec,
)

IMAGE = os.environ.get(
    "KAIWEN_DOCKER_IMAGE",
    "python@sha256:e064671ca452259bab50e8e2c7bd896e39e9a343bb06ca25234adf6fe18d0fc7",
)
BROKEN = "def add(a, b):\n    return a - b\n"


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="kaiwen-agent-worker-") as directory:
        base = Path(directory)
        root = base / "project"
        (root / "pkg").mkdir(parents=True)
        (root / "tests").mkdir()
        (root / "pkg" / "__init__.py").write_text("")
        (root / "tests" / "__init__.py").write_text("")
        (root / "pkg" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (root / "tests" / "test_calc.py").write_text(
            "import unittest\nfrom pkg.calc import add\n\n"
            "class AddTests(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(add(2, 3), 5)\n"
        )
        filesystem = WorkspaceFilesystem(workspace_id="example", roots={"main": root})
        runner = DockerSandboxRunner(
            DockerSandboxConfig(image=IMAGE), filesystem=filesystem, root="main"
        )
        try:
            await runner.verify()
        except SandboxUnavailableError as error:
            print(f"Docker sandbox unavailable ({error}); nothing was executed.")
            if os.environ.get("KAIWEN_REQUIRE_DOCKER") == "1":
                raise SystemExit("KAIWEN_REQUIRE_DOCKER is set, so this is a failure") from error
            return

        # Host composition: one workspace, one evidence identity, one fresh-state provider.
        preset = CommandPreset(
            id="unit.tests",
            category="test",
            executable="python",
            arguments=("-m", "unittest", "discover", "-s", "tests", "-t", "."),
            timeout_seconds=120,
        )
        artifacts = ArtifactManager()
        pipeline = TrustedEvidencePipeline.build(
            artifacts=artifacts,
            signer=EvidenceSigner(key=os.urandom(32)),
            bindings=[
                EvidenceBinding(
                    command_id="unit.tests",
                    requirement_id="coding.tests.run",
                    evidence_type="test_run",
                )
            ],
            trusted_runners=[runner],
        )
        registry = VerifierRegistry()
        pipeline.install_verifiers(registry)
        catalog = CommandCatalog(
            filesystem=filesystem,
            root="main",
            runner=runner,
            presets=(preset,),
            observers=(pipeline.recorder,),
        )
        fingerprints = WorkspaceFingerprintProvider(SnapshotBuilder(filesystem, "main"))
        workspace = AuthorizedCodingWorkspace(
            filesystem=filesystem,
            root="main",
            fingerprints=fingerprints,
            catalog=catalog,
            runner=runner,
            pipeline=pipeline,
        )
        store = SQLitePersistence(base / "state.sqlite3")
        runs = DurableRunManager(store)
        workers = WorkerRegistry()

        async def break_source(task: AgentTask, context: TaskExecutionContext) -> dict[str, Any]:
            (root / "pkg" / "calc.py").write_text(BROKEN)
            return {}

        workers.register("editor", FunctionWorker(break_source))
        controller = WorkflowController(
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
            completion_gate=CompletionGate(verifiers=registry, artifacts=artifacts),
            fingerprint_provider=fingerprints,
            workspace_binding=workspace.binding,
        )

        def register_tester(claim: str) -> None:
            run_tests = ToolCall(
                id="call-1",
                name="testing.run",
                arguments={"command_id": "unit.tests", "parameters": {}},
            )
            runtime = AgentRuntime(
                name="tester",
                model=FakeModelProvider(
                    [ModelResponse(tool_calls=[run_tests]), ModelResponse(text=claim)]
                ),
                tools=testing_tools(catalog, unattended_categories=frozenset({"test"})),
                permissions=frozenset({"tests.run"}),
                tool_executor=ToolExecutor(action_intent_store=store),
                max_steps=4,
            )
            worker = AgentRuntimeWorker(
                runtime=runtime,
                runs=runs,
                task_store=store,
                role="tester",
                permissions=frozenset({"tests.run"}),
                tool_names=("testing.run",),
                evidence_tool_names=frozenset({"testing.run"}),
                workspace=workspace,
                controller=controller,
                artifacts=artifacts,
                fingerprints=fingerprints,
            )
            workers._workers["tester"] = worker

        standards = StandardsResolver(StandardsRegistry(coding_standards_packs())).resolve(
            [VersionedReference(id="coding.tests", version=1)],
            condition_facts={"has_executable_tests": True},
        )
        owner = RuntimeIdentity(
            session_id="example", tenant_id="local", user_id="local", trace_id="example-trace"
        )
        pins = RuntimeVersionPins(
            workspace_id="example",
            workspace_version=1,
            agent_id="agent",
            agent_version=1,
            prompt_id="prompt",
            prompt_version=1,
            prompt_checksum="prompt",
            capability_manifest_id="agent",
            capability_manifest_version=1,
            provider_ref="fake",
            tool_names=(),
            toolset_checksum="toolset",
        )

        async def run_workflow(label: str, tasks: tuple[WorkflowTaskSpec, ...]) -> Any:
            # The run is bound to its workspace in the durable record when it is created.
            run = await runs.create(
                identity=owner,
                autonomy=AutonomyLevel.AUTONOMOUS,
                version_pins=pins,
                workspace_binding=workspace.binding,
            )
            outcome = await controller.execute(
                run=run,
                identity=owner,
                plan=WorkflowPlan(objective=label, tasks=tasks),
                standards=standards,
                protected_input_fingerprint=f"objective:{label}",
            )
            print(f"{label}: {outcome.status.value}")
            return outcome

        verify = WorkflowTaskSpec(id="verify", name="Run the tests", worker_id="tester")
        register_tester("Tests executed.")
        first = await run_workflow("working code", (verify,))
        register_tester("All tests pass!")  # the model claims success regardless of the result
        second = await run_workflow(
            "broken code",
            (
                WorkflowTaskSpec(id="break", name="Break the source", worker_id="editor"),
                WorkflowTaskSpec(
                    id="verify", name="Run the tests", worker_id="tester", dependencies=("break",)
                ),
            ),
        )

        problems = []
        if first.status != WorkflowOutcomeStatus.COMPLETED:
            problems.append("working code should complete")
        if second.status == WorkflowOutcomeStatus.COMPLETED:
            problems.append("broken code must not complete on the model's claim")
        if problems:
            raise SystemExit("Agent worker example failed: " + "; ".join(problems))
        print("The model's claim never replaced the executed, verified result.")


if __name__ == "__main__":
    asyncio.run(main())

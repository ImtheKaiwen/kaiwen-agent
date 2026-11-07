"""Show that a source change invalidates earlier execution evidence in a coding workflow.

State A is tested in a real Docker container, a coder step changes the source to state B,
and the completion gate (fed a fresh fingerprint of the current workspace) rejects A's
evidence. A repair cycle tests state B and completes. A later change to a failing state C
cannot complete. Requires a running Linux Docker engine and a local, digest-pinned image
(`docker pull <image>`); without one the example says so and never runs anything on the
host. Set KAIWEN_REQUIRE_DOCKER=1 to make that a failure, KAIWEN_DOCKER_IMAGE to change
the image. A run that does not behave as described exits with an error.
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
    EvidenceBinding,
    EvidenceSigner,
    TrustedEvidencePipeline,
    WorkspaceFingerprintProvider,
)
from kaiwen_agent.kits.coding.standards import coding_standards_packs
from kaiwen_agent.persistence import SQLitePersistence
from kaiwen_agent.quality import CompletionGate, VerifierRegistry
from kaiwen_agent.runtime import (
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
    CommandRunInput,
    DockerSandboxConfig,
    DockerSandboxRunner,
    SandboxUnavailableError,
    SnapshotBuilder,
)
from kaiwen_agent.workers import FunctionWorker, TaskExecutionContext, WorkerRegistry
from kaiwen_agent.workflow import (
    WorkflowCompiler,
    WorkflowController,
    WorkflowOutcomeStatus,
    WorkflowPlan,
    WorkflowRevisionRequest,
    WorkflowTaskSpec,
    trusted_task_context,
)

IMAGE = os.environ.get(
    "KAIWEN_DOCKER_IMAGE",
    "python@sha256:e064671ca452259bab50e8e2c7bd896e39e9a343bb06ca25234adf6fe18d0fc7",
)
STATE_B = "def add(a, b):\n    return b + a\n"
STATE_C = "def add(a, b):\n    return a - b\n"


class OneRevision:
    def __init__(self, *plans: WorkflowPlan) -> None:
        self.plans = list(plans)

    async def revise(self, request: WorkflowRevisionRequest) -> WorkflowPlan | None:
        return self.plans.pop(0) if self.plans else None


def verify_task(task_id: str, *after: str) -> WorkflowTaskSpec:
    return WorkflowTaskSpec(id=task_id, name="Run tests", worker_id="tester", dependencies=after)


def edit_task(task_id: str, content: str, *after: str) -> WorkflowTaskSpec:
    return WorkflowTaskSpec(
        id=task_id,
        name="Edit source",
        worker_id="editor",
        dependencies=after,
        input={"content": content},
    )


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="kaiwen-freshness-") as directory:
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

        # One evidence identity: the same artifacts and signer for recording and verifying.
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
            presets=(
                CommandPreset(
                    id="unit.tests",
                    category="test",
                    executable="python",
                    arguments=("-m", "unittest", "discover", "-s", "tests", "-t", "."),
                    timeout_seconds=120,
                ),
            ),
            observers=(pipeline.recorder,),
        )
        store = SQLitePersistence(base / "state.sqlite3")
        runs = DurableRunManager(store)
        standards = StandardsResolver(StandardsRegistry(coding_standards_packs())).resolve(
            [VersionedReference(id="coding.tests", version=1)],
            condition_facts={"has_executable_tests": True},
        )
        builder = SnapshotBuilder(filesystem, "main")
        exit_codes: list[int] = []

        async def run_tests(task: AgentTask, context: TaskExecutionContext) -> dict[str, Any]:
            # Identity comes from the durable run, not from the plan or the model.
            agent_context = await trusted_task_context(
                task, runs=runs, permissions=frozenset({"tests.run"}), workspace_id="example"
            )
            result = await catalog.execute(
                agent_context, CommandRunInput(command_id="unit.tests"), category="test"
            )
            exit_codes.append(result.exit_code)
            return {"exit_code": result.exit_code}

        async def edit_source(task: AgentTask, context: TaskExecutionContext) -> dict[str, Any]:
            (root / "pkg" / "calc.py").write_text(str(task.input["content"]))
            return {}

        workers = WorkerRegistry()
        workers.register("tester", FunctionWorker(run_tests))
        workers.register("editor", FunctionWorker(edit_source))

        def controller(revisions: OneRevision) -> WorkflowController:
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
                completion_gate=CompletionGate(verifiers=registry, artifacts=artifacts),
                revision_planner=revisions,
                fingerprint_provider=WorkspaceFingerprintProvider(builder),
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

        async def start(label: str, workflow: WorkflowPlan, revisions: OneRevision) -> Any:
            run = await runs.create(
                identity=owner, autonomy=AutonomyLevel.AUTONOMOUS, version_pins=pins
            )
            outcome = await controller(revisions).execute(
                run=run,
                identity=owner,
                plan=workflow,
                standards=standards,
                protected_input_fingerprint=f"objective:{label}",
            )
            print(f"{label}: {outcome.status.value} after {outcome.cycles} cycle(s)")
            return outcome

        fingerprint_a = builder.fingerprint()
        first = await start(
            "A tested, then changed to B",
            WorkflowPlan(
                objective="Ship",
                tasks=(verify_task("test-a"), edit_task("to-b", STATE_B, "test-a")),
            ),
            OneRevision(WorkflowPlan(objective="Ship", tasks=(verify_task("test-b"),))),
        )
        fingerprint_b = builder.fingerprint()
        second = await start(
            "B tested, then changed to failing C",
            WorkflowPlan(
                objective="Ship",
                tasks=(verify_task("test-b"), edit_task("to-c", STATE_C, "test-b")),
            ),
            OneRevision(),
        )

        problems = []
        if fingerprint_a == fingerprint_b:
            problems.append("source states A and B must have different fingerprints")
        if first.status != WorkflowOutcomeStatus.COMPLETED or first.cycles != 2:
            problems.append("state B should complete only in the repair cycle")
        if first.verified_input_fingerprint != fingerprint_b:
            problems.append("the gate must have verified state B")
        if second.status == WorkflowOutcomeStatus.COMPLETED:
            problems.append("state C must not complete on evidence for state B")
        if exit_codes[:2] != [0, 0]:
            problems.append(f"unexpected test exit codes: {exit_codes}")
        if problems:
            raise SystemExit("Evidence freshness example failed: " + "; ".join(problems))
        print("Evidence recorded for one source state never satisfied another.")


if __name__ == "__main__":
    asyncio.run(main())

from __future__ import annotations

import asyncio
import math

from kaiwen_agent.agents import VersionedReference
from kaiwen_agent.artifacts import ArtifactCreate, ArtifactManager, ArtifactType
from kaiwen_agent.control import RunControl
from kaiwen_agent.persistence import SQLitePersistence
from kaiwen_agent.quality import (
    CompletionGate,
    RepairLimits,
    RepairTracker,
    VerificationResult,
    VerificationStatus,
    VerifierRegistry,
)
from kaiwen_agent.runtime import (
    AutonomyLevel,
    DurableRunManager,
    RuntimeIdentity,
    RuntimeVersionPins,
)
from kaiwen_agent.standards import (
    RequirementDefinition,
    RequirementObligation,
    RequirementSeverity,
    ResolvedRequirement,
    StandardResolution,
)
from kaiwen_agent.tasks import TaskScheduler
from kaiwen_agent.workers import FunctionWorker, WorkerRegistry
from kaiwen_agent.workflow import (
    WorkflowCompiler,
    WorkflowController,
    WorkflowLimits,
    WorkflowOutcomeStatus,
    WorkflowPlan,
    WorkflowPlanError,
    WorkflowRevisionReason,
    WorkflowTaskSpec,
)


def _identity() -> RuntimeIdentity:
    return RuntimeIdentity(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        conversation_id="conversation-1",
        trace_id="trace-1",
    )


def _pins() -> RuntimeVersionPins:
    return RuntimeVersionPins(
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


def _empty_standards() -> StandardResolution:
    return StandardResolution(pack_refs=(), requirements=(), prompt_refs=())


async def _runtime_run(store: SQLitePersistence):
    manager = DurableRunManager(store)
    run = await manager.create(
        identity=_identity(),
        autonomy=AutonomyLevel.AUTONOMOUS,
        version_pins=_pins(),
    )
    return manager, run


def _controller(
    *,
    store: SQLitePersistence,
    workers: WorkerRegistry,
    artifacts: ArtifactManager | None = None,
    verifiers: VerifierRegistry | None = None,
    revision_planner=None,
    repair_tracker: RepairTracker | None = None,
    limits: WorkflowLimits | None = None,
) -> WorkflowController:
    compiler = WorkflowCompiler(
        workers=workers,
        worker_allowlist=frozenset({"worker"}),
        effective_capabilities=frozenset({"repo.read", "repo.write"}),
        resource_allowlist=frozenset({"repo:main"}),
        limits=limits,
    )
    return WorkflowController(
        runs=DurableRunManager(store),
        compiler=compiler,
        scheduler_factory=lambda graph: TaskScheduler(
            graph=graph,
            workers=workers,
            store=store,
            control=RunControl(checkpoints=store),
        ),
        completion_gate=CompletionGate(
            verifiers=verifiers or VerifierRegistry(),
            artifacts=artifacts or ArtifactManager(),
        ),
        revision_planner=revision_planner,
        repair_tracker=repair_tracker,
        limits=limits,
    )


def test_workflow_compiles_into_task_scheduler_and_completes(tmp_path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "workflow.sqlite3")
        observed: list[str] = []

        async def execute(task, context):
            await context.checkpoint(state={"logical": task.metadata["logical_task_id"]})
            observed.append(str(task.metadata["logical_task_id"]))
            return {"ok": True}

        workers = WorkerRegistry()
        workers.register("worker", FunctionWorker(execute))
        manager, run = await _runtime_run(store)
        controller = _controller(store=store, workers=workers)
        plan = WorkflowPlan(
            objective="Build and verify",
            tasks=(
                WorkflowTaskSpec(
                    id="build",
                    name="Build",
                    worker_id="worker",
                    capabilities=frozenset({"repo.write"}),
                    resource_keys=("repo:main",),
                ),
                WorkflowTaskSpec(
                    id="verify",
                    name="Verify",
                    worker_id="worker",
                    dependencies=("build",),
                    capabilities=frozenset({"repo.read"}),
                ),
            ),
        )
        outcome = await controller.execute(
            run=run,
            identity=_identity(),
            plan=plan,
            standards=_empty_standards(),
            protected_input_fingerprint="input-1",
        )
        assert outcome.status == WorkflowOutcomeStatus.COMPLETED
        assert observed == ["build", "verify"]
        assert all(task.metadata["runtime_run_id"] == run.id for task in outcome.tasks)
        persisted = await manager.get(run_id=run.id, identity=_identity())
        assert persisted is not None and persisted.status.value == "completed"

    asyncio.run(scenario())


def test_compiler_rejects_untrusted_workers_capabilities_resources_and_cycles(tmp_path) -> None:
    async def execute(task, context):
        del task, context
        return {}

    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "validation.sqlite3")
        workers = WorkerRegistry()
        workers.register("worker", FunctionWorker(execute))
        _, run = await _runtime_run(store)
        compiler = WorkflowCompiler(
            workers=workers,
            worker_allowlist=frozenset({"worker"}),
            effective_capabilities=frozenset({"repo.read"}),
            resource_allowlist=frozenset({"repo:main"}),
        )
        cases = (
            WorkflowPlan(
                objective="Bad worker",
                tasks=(WorkflowTaskSpec(id="one", name="One", worker_id="unknown"),),
            ),
            WorkflowPlan(
                objective="Bad capability",
                tasks=(
                    WorkflowTaskSpec(
                        id="one",
                        name="One",
                        worker_id="worker",
                        capabilities=frozenset({"repo.write"}),
                    ),
                ),
            ),
            WorkflowPlan(
                objective="Bad resource",
                tasks=(
                    WorkflowTaskSpec(
                        id="one",
                        name="One",
                        worker_id="worker",
                        resource_keys=("repo:other",),
                    ),
                ),
            ),
            WorkflowPlan(
                objective="Cycle",
                tasks=(
                    WorkflowTaskSpec(
                        id="one", name="One", worker_id="worker", dependencies=("two",)
                    ),
                    WorkflowTaskSpec(
                        id="two", name="Two", worker_id="worker", dependencies=("one",)
                    ),
                ),
            ),
        )
        for plan in cases:
            try:
                compiler.compile(plan, run=run, identity=_identity(), cycle=1)
            except WorkflowPlanError:
                continue
            raise AssertionError("Unsafe workflow plan was accepted")

    asyncio.run(scenario())


def test_compiler_rejects_cross_owner_identity_and_non_json_input(tmp_path) -> None:
    async def execute(task, context):
        del task, context
        return {}

    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "identity.sqlite3")
        workers = WorkerRegistry()
        workers.register("worker", FunctionWorker(execute))
        _, run = await _runtime_run(store)
        compiler = WorkflowCompiler(
            workers=workers,
            worker_allowlist=frozenset({"worker"}),
        )
        plan = WorkflowPlan(
            objective="Owned",
            tasks=(WorkflowTaskSpec(id="one", name="One", worker_id="worker"),),
        )
        other = _identity().model_copy(update={"user_id": "user-2"})
        try:
            compiler.compile(plan, run=run, identity=other, cycle=1)
        except WorkflowPlanError:
            pass
        else:
            raise AssertionError("Cross-owner workflow plan was accepted")

        invalid = WorkflowPlan(
            objective="Strict JSON",
            tasks=(
                WorkflowTaskSpec(
                    id="one",
                    name="One",
                    worker_id="worker",
                    input={"value": math.nan},
                ),
            ),
        )
        try:
            compiler.compile(invalid, run=run, identity=_identity(), cycle=1)
        except WorkflowPlanError:
            pass
        else:
            raise AssertionError("Non-JSON workflow input was accepted")

    asyncio.run(scenario())


def test_scheduler_failure_marks_runtime_run_failed(tmp_path) -> None:
    class BrokenScheduler:
        async def run(self):
            raise RuntimeError("scheduler unavailable")

    async def execute(task, context):
        del task, context
        return {}

    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "scheduler-failure.sqlite3")
        workers = WorkerRegistry()
        workers.register("worker", FunctionWorker(execute))
        manager, run = await _runtime_run(store)
        compiler = WorkflowCompiler(
            workers=workers,
            worker_allowlist=frozenset({"worker"}),
        )
        controller = WorkflowController(
            runs=manager,
            compiler=compiler,
            scheduler_factory=lambda graph: BrokenScheduler(),  # type: ignore[arg-type,return-value]
            completion_gate=CompletionGate(
                verifiers=VerifierRegistry(),
                artifacts=ArtifactManager(),
            ),
        )
        outcome = await controller.execute(
            run=run,
            identity=_identity(),
            plan=WorkflowPlan(
                objective="Fail safely",
                tasks=(WorkflowTaskSpec(id="one", name="One", worker_id="worker"),),
            ),
            standards=_empty_standards(),
            protected_input_fingerprint="input-1",
        )
        assert outcome.status == WorkflowOutcomeStatus.FAILED
        assert outcome.reason == "scheduler_failed:RuntimeError"
        persisted = await manager.get(run_id=run.id, identity=_identity())
        assert persisted is not None and persisted.status.value == "failed"

    asyncio.run(scenario())


class OneRevisionPlanner:
    def __init__(self, plan: WorkflowPlan) -> None:
        self.plan = plan
        self.requests = []

    async def revise(self, request):
        self.requests.append(request)
        return self.plan


class BrokenRevisionPlanner:
    async def revise(self, request):
        del request
        raise RuntimeError("planner unavailable")


def test_execution_failure_uses_bounded_replan_then_completes(tmp_path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "replan.sqlite3")

        async def execute(task, context):
            del context
            if task.input.get("fail"):
                raise RuntimeError("failed")
            return {"ok": True}

        workers = WorkerRegistry()
        workers.register("worker", FunctionWorker(execute))
        _, run = await _runtime_run(store)
        repaired = WorkflowPlan(
            objective="Retry safely",
            tasks=(
                WorkflowTaskSpec(
                    id="fixed", name="Fixed", worker_id="worker", input={"fail": False}
                ),
            ),
        )
        planner = OneRevisionPlanner(repaired)
        controller = _controller(store=store, workers=workers, revision_planner=planner)
        outcome = await controller.execute(
            run=run,
            identity=_identity(),
            plan=WorkflowPlan(
                objective="Initial",
                tasks=(
                    WorkflowTaskSpec(
                        id="broken", name="Broken", worker_id="worker", input={"fail": True}
                    ),
                ),
            ),
            standards=_empty_standards(),
            protected_input_fingerprint="input-1",
        )
        assert outcome.status == WorkflowOutcomeStatus.COMPLETED
        assert outcome.cycles == 2
        assert planner.requests[0].reason == WorkflowRevisionReason.EXECUTION_FAILED

    asyncio.run(scenario())


def test_revision_planner_failure_marks_runtime_run_failed(tmp_path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "planner-failure.sqlite3")

        async def execute(task, context):
            del task, context
            raise RuntimeError("work failed")

        workers = WorkerRegistry()
        workers.register("worker", FunctionWorker(execute))
        manager, run = await _runtime_run(store)
        outcome = await _controller(
            store=store,
            workers=workers,
            revision_planner=BrokenRevisionPlanner(),
        ).execute(
            run=run,
            identity=_identity(),
            plan=WorkflowPlan(
                objective="Fail planner safely",
                tasks=(WorkflowTaskSpec(id="one", name="One", worker_id="worker"),),
            ),
            standards=_empty_standards(),
            protected_input_fingerprint="input-1",
        )
        assert outcome.status == WorkflowOutcomeStatus.FAILED
        assert outcome.reason == "revision_planner_failed:RuntimeError"
        persisted = await manager.get(run_id=run.id, identity=_identity())
        assert persisted is not None and persisted.status.value == "failed"

    asyncio.run(scenario())


def test_repeated_quality_failure_stagnates_and_blocks(tmp_path) -> None:
    requirement = RequirementDefinition(
        id="quality.tests",
        description="Tests pass",
        obligation=RequirementObligation.MUST,
        severity=RequirementSeverity.ERROR,
        acceptance_criteria=("Tests pass",),
        verifier_ref="tests.verify",
        evidence_types=frozenset({"test_report"}),
    )
    standards = StandardResolution(
        pack_refs=(VersionedReference(id="software", version=1),),
        requirements=(
            ResolvedRequirement(
                definition=requirement,
                source_pack_refs=(VersionedReference(id="software", version=1),),
                applies=True,
            ),
        ),
        prompt_refs=(),
    )

    class FailingVerifier:
        verifier_id = "tests.verify"

        async def verify(self, requirement, *, context):
            del context
            return VerificationResult(
                requirement_id=requirement.definition.id,
                status=VerificationStatus.FAIL,
                findings=("same failure",),
                verifier_id=self.verifier_id,
            )

    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "quality.sqlite3")

        async def execute(task, context):
            del task, context
            return {"ok": True}

        workers = WorkerRegistry()
        workers.register("worker", FunctionWorker(execute))
        _, run = await _runtime_run(store)
        plan = WorkflowPlan(
            objective="Verify",
            tasks=(WorkflowTaskSpec(id="work", name="Work", worker_id="worker"),),
        )
        planner = OneRevisionPlanner(plan)
        verifiers = VerifierRegistry()
        verifiers.register(FailingVerifier())
        controller = _controller(
            store=store,
            workers=workers,
            verifiers=verifiers,
            revision_planner=planner,
            repair_tracker=RepairTracker(
                RepairLimits(
                    max_repairs_per_requirement=5,
                    max_replans=1,
                    stagnation_repetitions=2,
                )
            ),
        )
        outcome = await controller.execute(
            run=run,
            identity=_identity(),
            plan=plan,
            standards=standards,
            protected_input_fingerprint="input-1",
        )
        assert outcome.status == WorkflowOutcomeStatus.BLOCKED
        assert outcome.cycles == 2
        assert len(planner.requests) == 1

    asyncio.run(scenario())


def test_quality_repair_requires_fresh_evidence_before_completion(tmp_path) -> None:
    requirement = RequirementDefinition(
        id="quality.tests",
        description="Tests pass",
        obligation=RequirementObligation.MUST,
        acceptance_criteria=("Tests pass",),
        verifier_ref="tests.verify",
        evidence_types=frozenset({"test_report"}),
    )
    standards = StandardResolution(
        pack_refs=(VersionedReference(id="software", version=1),),
        requirements=(
            ResolvedRequirement(
                definition=requirement,
                source_pack_refs=(VersionedReference(id="software", version=1),),
                applies=True,
            ),
        ),
        prompt_refs=(),
    )

    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "quality-pass.sqlite3")
        artifacts = ArtifactManager()
        calls = 0

        class RepairingVerifier:
            verifier_id = "tests.verify"

            async def verify(self, requirement, *, context):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return VerificationResult(
                        requirement_id=requirement.definition.id,
                        status=VerificationStatus.FAIL,
                        findings=("tests failed",),
                        verifier_id=self.verifier_id,
                    )
                evidence = await artifacts.create(
                    context.artifact_context(),
                    ArtifactCreate(
                        type=ArtifactType.REPORT,
                        mime_type="application/json",
                        content=b'{"passed":true}',
                        labels={
                            "evidence_type": "test_report",
                            "input_fingerprint": context.protected_input_fingerprint,
                        },
                    ),
                )
                return VerificationResult(
                    requirement_id=requirement.definition.id,
                    status=VerificationStatus.PASS,
                    evidence_refs=(evidence.id,),
                    verifier_id=self.verifier_id,
                )

        async def execute(task, context):
            del task, context
            return {"ok": True}

        workers = WorkerRegistry()
        workers.register("worker", FunctionWorker(execute))
        _, run = await _runtime_run(store)
        plan = WorkflowPlan(
            objective="Repair",
            tasks=(WorkflowTaskSpec(id="work", name="Work", worker_id="worker"),),
        )
        planner = OneRevisionPlanner(plan)
        verifiers = VerifierRegistry()
        verifiers.register(RepairingVerifier())
        outcome = await _controller(
            store=store,
            workers=workers,
            artifacts=artifacts,
            verifiers=verifiers,
            revision_planner=planner,
        ).execute(
            run=run,
            identity=_identity(),
            plan=plan,
            standards=standards,
            protected_input_fingerprint="input-1",
        )
        assert outcome.status == WorkflowOutcomeStatus.COMPLETED
        assert outcome.cycles == 2
        assert outcome.quality_result is not None and outcome.quality_result.can_complete

    asyncio.run(scenario())

"""A credential-free coding team: Lead, Coder, Tester and Reviewer, accepted only by the host.

One local, deterministic composition of existing platform parts (no external model, API key or
Docker engine is needed):

* `Lead` proposes the other tasks through the C1 to C3 delegation path; the compiler proves each
  task's identity and dependencies.
* `Coder` changes the project; `Tester` runs the project's tests through the real command catalog
  and the trusted evidence pipeline (signed, run-scoped artifacts); `Reviewer` submits one typed
  assessment through `review.submit`.
* A real `CompletionGate` verifies the signed test evidence for the protected workspace
  fingerprint; a real `TeamAcceptance` then decides, after reading storage again, whether the
  fenced run may complete. Everything is stored in one SQLite database under a fenced owner lease.

What is real: SQLite persistence and leases, the fenced task store, the durable run manager, the
workflow compiler, controller and scheduler, `AgentRuntime` and `AgentRuntimeWorker`, the command
catalog, the evidence signer, artifact manager and verifiers, the completion gate, the review
contract and team acceptance. What is scripted: the model responses (`ScriptedModel`), the human
approval answers (`ExampleApprover`), the Coder's edit (a host function writing through the real
`WorkspaceFilesystem`) and the local test runner (`LocalSnapshotRunner`: it runs the project's tests
in a temporary copy of the workspace with the current Python and is NOT a sandbox; production hosts
configure `DockerSandboxRunner`, see `examples/coding-agent-worker`).

Run it with `python examples/coding-team-acceptance/main.py [scenario ...]`. A scenario that does
not end as documented exits with an error. A blocked run is never reported as a delivery.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from kaiwen_agent.agents import VersionedReference
from kaiwen_agent.artifacts import ArtifactContext, ArtifactManager
from kaiwen_agent.context import AgentContext
from kaiwen_agent.control import RunControl
from kaiwen_agent.coordination import SQLiteLeaseStore
from kaiwen_agent.integrations import WorkspaceFilesystem
from kaiwen_agent.kits.coding import (
    AuthorizedCodingWorkspace,
    EvidenceBinding,
    EvidenceSigner,
    TrustedEvidencePipeline,
    WorkspaceFingerprintProvider,
)
from kaiwen_agent.kits.coding.standards import coding_standards_packs
from kaiwen_agent.persistence import SQLitePersistence
from kaiwen_agent.policy import (
    DelegationAdmission,
    DelegationPolicy,
    PolicyEngine,
    PolicyRegistry,
)
from kaiwen_agent.quality import CompletionGate, VerifierRegistry
from kaiwen_agent.realtime import InMemoryConversationStore
from kaiwen_agent.runtime import (
    AgentRuntime,
    AutonomyLevel,
    DurableRunManager,
    RuntimeIdentity,
    RuntimeVersionPins,
)
from kaiwen_agent.standards import StandardsRegistry, StandardsResolver
from kaiwen_agent.tasks import AgentTask, TaskScheduler, TaskStatus
from kaiwen_agent.toolkits import (
    CommandCatalog,
    CommandPreset,
    CommandRunInput,
    ExecutionSnapshot,
    SandboxCommand,
    SandboxResult,
    SandboxUnavailableError,
    SnapshotBuilder,
    SnapshotError,
)
from kaiwen_agent.tools import ToolExecutor
from kaiwen_agent.types import ModelResponse, ToolCall
from kaiwen_agent.workers import FunctionWorker, TaskExecutionContext, WorkerRegistry
from kaiwen_agent.workflow import (
    AgentRuntimeWorker,
    CrossCycleHandoffs,
    DelegationAuthority,
    DurableLeadProposalSource,
    FencedExecution,
    HumanApproval,
    HumanApprovalStatus,
    LeadRoleBinding,
    LeadToolConfig,
    LeadTools,
    LeadViewAssembler,
    ReviewTools,
    TaskContextAssembler,
    TeamAcceptance,
    TeamAcceptancePolicy,
    WorkflowCompiler,
    WorkflowController,
    WorkflowDelegation,
    WorkflowDelegationAdmission,
    WorkflowOutcome,
    WorkflowOutcomeStatus,
    WorkflowPlan,
    WorkflowTaskSpec,
)
from kaiwen_agent.workflow import review as review_contract

CAPABILITIES = frozenset({"workspace.read"})
ROLES = ("lead", "lead2", "coder", "tester", "reviewer")
FIXED = "def add(a, b):\n    return a + b\n"
BROKEN = "def add(a, b):\n    return a - b\n"
PROJECT_TESTS = (
    "import unittest\nfrom pkg.calc import add\n\n"
    "class AddTests(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(add(2, 3), 5)\n"
)


# --------------------------------------------------------------------------- scripted pieces


class ScriptedModel:
    """A deterministic model: each step is a `ModelResponse` or a function of (prompt, context)."""

    def __init__(self, *steps: Any) -> None:
        self.steps = list(steps)

    async def respond(  # type: ignore[no-untyped-def]
        self, agent_input, *, context, tools, tool_results, provider_state
    ) -> ModelResponse:
        del tools, tool_results, provider_state
        if not self.steps:
            return ModelResponse(text="done")
        step = self.steps.pop(0)
        if not callable(step):
            return step  # type: ignore[no-any-return]
        produced = step(agent_input.text, context)
        return await produced if inspect.isawaitable(produced) else produced  # type: ignore[no-any-return]


class LocalSnapshotRunner:
    """Runs a host-registered `python` preset against a private copy of a snapshot. NOT a sandbox.

    It keeps the platform's execution-evidence contract, which the evidence pipeline and the gate
    rely on: the command runs in a fresh, verified copy of the captured snapshot (never in the
    snapshot or the workspace); `snapshot_unchanged` and `modified_paths` come from
    `ExecutionSnapshot.changed_paths` and are reported as modified when the comparison cannot be
    made; a timeout returns a timed-out result after the process was killed and reaped; cancellation
    kills the process, cleans up and still propagates; and the project code receives only a small
    allowlist of environment variables, never the host's keys. The host names this very object as
    the trusted runner.

    What it does not provide: isolation (the project code runs as the host user, with network and
    filesystem access), a process-tree kill (only the started process), or a bounded read of a
    flooding command (the output is read fully, then truncated). Only fixed host presets should use
    it; production hosts configure `DockerSandboxRunner`.
    """

    _ENVIRONMENT = ("SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL")
    _TIMEOUT_EXIT_CODE = 124  # the platform sandbox runner's convention

    def __init__(self, builder: SnapshotBuilder) -> None:
        self.builder = builder
        # A trusted runner names the workspace it executes in; coding workers check it.
        self.filesystem = builder.filesystem
        self.root = builder.root

    async def run(self, command: SandboxCommand) -> SandboxResult:
        relative = self._validate(command)  # nothing runs, and nothing is copied, if it refuses
        started = time.monotonic()
        snapshot = await self._capture()
        run_directory: Path | None = None
        threads: list[asyncio.Future[Any]] = []  # worker threads that use the paths below
        try:
            posix = PurePosixPath(relative.as_posix())
            if not snapshot.has_directory(posix.as_posix()):
                raise SandboxUnavailableError("Working directory is not part of the snapshot")
            run_directory = Path(tempfile.mkdtemp(prefix="kaiwen-local-run-"))
            copy = run_directory / "workspace"
            try:
                await self._thread(
                    threads, snapshot.materialize, copy, writable=command.writable_workspace
                )
            except SnapshotError as error:
                raise SandboxUnavailableError("The execution snapshot is not intact") from error
            output, returncode, timed_out = await self._execute(command, copy / relative)
            # The process is gone: compare the run copy with the captured digests. An
            # unverifiable comparison counts as modified, never as unchanged.
            try:
                modified = await self._thread(threads, snapshot.changed_paths, copy)
            except Exception:  # noqa: BLE001 - fail closed, whatever the reason
                unchanged, modified = False, ()
            else:
                unchanged = not modified
            text = output[: command.max_output_bytes].decode(errors="ignore")
            result = SandboxResult(
                exit_code=returncode,
                stdout=text,
                duration_seconds=time.monotonic() - started,
                timed_out=timed_out,
                snapshot_fingerprint=snapshot.fingerprint,
                image_ref="local-python",
                image_id="local",
                output_truncated=len(output) > command.max_output_bytes,
                snapshot_unchanged=unchanged,
                modified_paths=modified,
            )
        finally:
            leaked = self._release(snapshot, run_directory, threads)
        return result.model_copy(update={"cleanup_failed": leaked})

    @staticmethod
    async def _thread(
        threads: list[asyncio.Future[Any]], function: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """Run `function` in a worker thread that stays registered after a cancellation.

        `asyncio.to_thread` cannot stop its thread when the caller is cancelled. The future is
        shielded and recorded so the cleanup that owns the files the thread writes or reads can wait
        for it (see `_release`) instead of removing them underneath it.
        """

        future = asyncio.ensure_future(asyncio.to_thread(function, *args, **kwargs))
        future.add_done_callback(lambda done: done.cancelled() or done.exception())
        threads.append(future)
        return await asyncio.shield(future)

    @staticmethod
    def _release(
        snapshot: ExecutionSnapshot, run_directory: Path | None, threads: list[asyncio.Future[Any]]
    ) -> bool:
        """Close the snapshot and remove the run directory, never underneath a running thread.

        On the normal path every thread has ended and this removes at once (the result reports a
        leak). After a cancellation a thread may still be running (it could re-create a deleted
        directory, as `materialize` does with `parents=True`): removal is then deferred to the
        moment the last such thread ends. If the event loop is gone by then, the remainder is left
        to the operating system's temporary-directory cleanup.
        """

        def remove() -> bool:
            snapshot.close()
            if run_directory is None:
                return False
            shutil.rmtree(run_directory, ignore_errors=True)
            return run_directory.exists()

        running = [thread for thread in threads if not thread.done()]
        if not running:
            return remove()
        remaining = len(running)

        def one_ended(_: object) -> None:
            nonlocal remaining
            remaining -= 1
            if remaining == 0:
                remove()

        for thread in running:
            thread.add_done_callback(one_ended)
        return False

    def _validate(self, command: SandboxCommand) -> Path:
        """The platform runner's contract: this runner's workspace, a contained directory."""

        if command.argv[0] != "python":
            raise SandboxUnavailableError("The local runner only executes the python preset")
        root = self.filesystem.roots[self.root]
        if command.workspace != root:  # the configured path itself, never an equivalent spelling
            raise SandboxUnavailableError("Command workspace is not the configured root")
        if ".." in command.working_directory.parts:
            raise SandboxUnavailableError("Working directory must not contain a traversal")
        try:
            return command.working_directory.resolve(strict=False).relative_to(root)
        except ValueError:
            raise SandboxUnavailableError("Working directory is outside the workspace") from None

    async def _capture(self) -> ExecutionSnapshot:
        """Capture in a worker thread without leaking the copy if the caller is cancelled.

        A cancelled `to_thread` call still lets the thread finish; the finished snapshot would
        otherwise be dropped without `close()`. The orphan is closed when the thread ends.
        """

        future = asyncio.ensure_future(asyncio.to_thread(self.builder.capture))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            future.add_done_callback(self._close_orphan)
            raise

    @staticmethod
    def _close_orphan(future: asyncio.Future[ExecutionSnapshot]) -> None:
        if not future.cancelled() and future.exception() is None:
            future.result().close()

    async def _execute(self, command: SandboxCommand, cwd: Path) -> tuple[bytes, int, bool]:
        environment = {k: os.environ[k] for k in self._ENVIRONMENT if k in os.environ}
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            *command.argv[1:],
            cwd=cwd,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(
                process.communicate(), timeout=command.timeout_seconds
            )
        except TimeoutError:
            await asyncio.shield(self._terminate(process))
            return b"", self._TIMEOUT_EXIT_CODE, True
        except asyncio.CancelledError:
            await asyncio.shield(self._terminate(process))
            raise
        return output, process.returncode or 0, False

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=10)


class ExampleApprover:
    """The host's human-approval source (a read-only port). Answers are scripted per scenario."""

    def __init__(self, mode: str, on_ask: Callable[[], None] | None = None) -> None:
        self.mode = mode
        self.on_ask = on_ask
        self.subjects: list[Any] = []

    async def decision(self, subject: Any) -> HumanApproval:
        self.subjects.append(subject)
        if self.on_ask is not None:
            self.on_ask()  # e.g. the workspace changes while the approval is awaited
        digest = subject.digest if self.mode != "mismatch" else "0" * 64
        status = HumanApprovalStatus.DENIED if self.mode == "deny" else HumanApprovalStatus.APPROVED
        return HumanApproval(status=status, subject_digest=digest)


# ------------------------------------------------------------------------------- scenarios


@dataclass(frozen=True)
class Scenario:
    """What differs between runs. Nothing here weakens a requirement."""

    name: str
    expect_status: WorkflowOutcomeStatus
    expect_reason: str | None
    layout: str = "cross_cycle"  # or "same_cycle", "no_reviewer"
    fix_code: bool = True  # False: the Coder leaves the bug in place
    recommendation: str = "approve"  # the Reviewer's typed recommendation
    cite: str = "tests"  # "tests": the gate-verified artifact; "compile": an unrelated one
    approval: str | None = None  # None, "approve", "deny", "mismatch", "approve_after_edit"
    team_acceptance: bool = True  # False: the opt-in acceptance is not configured (legacy flow)


SCENARIOS = (
    Scenario("accepted", WorkflowOutcomeStatus.COMPLETED, None),
    Scenario("same_cycle", WorkflowOutcomeStatus.COMPLETED, None, layout="same_cycle"),
    Scenario("human_approved", WorkflowOutcomeStatus.COMPLETED, None, approval="approve"),
    Scenario(
        "changes_requested",
        WorkflowOutcomeStatus.BLOCKED,
        "team_changes_required:reviewer_requested_changes",
        recommendation="changes_requested",
    ),
    Scenario("failing_tests", WorkflowOutcomeStatus.BLOCKED, "quality_blocked", fix_code=False),
    Scenario(
        "unrelated_evidence",
        WorkflowOutcomeStatus.BLOCKED,
        "team_changes_required:evidence_not_gate_verified",
        cite="compile",
    ),
    Scenario(
        "no_reviewer",
        WorkflowOutcomeStatus.BLOCKED,
        "team_blocked:review_missing",
        layout="no_reviewer",
    ),
    Scenario(
        "approval_denied",
        WorkflowOutcomeStatus.BLOCKED,
        "team_blocked:human_denied",
        approval="deny",
    ),
    Scenario(
        "approval_for_other_delivery",
        WorkflowOutcomeStatus.BLOCKED,
        "team_blocked:approval_mismatch",
        approval="mismatch",
    ),
    Scenario(
        "workspace_changed_during_approval",
        WorkflowOutcomeStatus.BLOCKED,
        "workspace_changed_after_acceptance",
        approval="approve_after_edit",
    ),
)


@dataclass
class Result:
    scenario: Scenario
    outcome: WorkflowOutcome
    run_status: str
    run_id: str = ""
    approver: ExampleApprover | None = None
    # Runs the same durable run again (a replay); the scripted models are not rewound.
    replay: Callable[[], Awaitable[WorkflowOutcome]] | None = None

    @property
    def acceptance(self) -> str:
        decision = self.outcome.team_acceptance
        return f"{decision.status.value}:{decision.code}" if decision else "not_evaluated"

    @property
    def as_expected(self) -> bool:
        return (
            self.outcome.status is self.scenario.expect_status
            and self.outcome.reason == self.scenario.expect_reason
            and (self.run_status == "completed") == (self.outcome.status.value == "completed")
        )


def _owner() -> RuntimeIdentity:
    return RuntimeIdentity(
        session_id="session-1", tenant_id="local", user_id="local", trace_id="trace-1"
    )


def _pins() -> RuntimeVersionPins:
    return RuntimeVersionPins(
        workspace_id="project",
        workspace_version=1,
        agent_id="team",
        agent_version=1,
        prompt_id="prompt",
        prompt_version=1,
        prompt_checksum="prompt",
        capability_manifest_id="team",
        capability_manifest_version=1,
        provider_ref="scripted",
        tool_names=(),
        toolset_checksum="toolset",
    )


def _propose(call_id: str, **fields: Any) -> ToolCall:
    arguments = {"description": f"Do {fields['id']}", **fields}
    return ToolCall(id=call_id, name="lead.propose_task", arguments=arguments)


def _lead_script(layout: str) -> dict[str, list[Any]]:
    view = ModelResponse(tool_calls=[ToolCall(id="view", name="lead.view", arguments={})])
    code = _propose("p1", id="code", name="Fix add()", role="coder")
    test = _propose("p2", id="test", name="Run the tests", role="tester", dependencies=["code"])
    if layout == "same_cycle":
        review = _propose(
            "p3",
            id="review",
            name="Review the delivery",
            role="reviewer",
            dependencies=["code", "test"],
        )
        return {"lead": [view, ModelResponse(tool_calls=[code, test, review]), "planned"]}
    if layout == "no_reviewer":
        return {"lead": [view, ModelResponse(tool_calls=[code, test]), "planned"]}
    plan2 = _propose("p3", id="plan2", name="Plan the review", role="lead2")
    review = _propose("p4", id="review", name="Review the delivery", role="reviewer")
    return {
        "lead": [view, ModelResponse(tool_calls=[code, test, plan2]), "planned"],
        "plan2": [ModelResponse(tool_calls=[review]), "planned the review"],
    }


async def run_scenario(
    scenario: Scenario,
    directory: Path,
    *,
    approver: ExampleApprover | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Result:
    """Compose the team from public platform interfaces and run one delivery to its outcome.

    `approver` and `clock` are injection points for deterministic tests (a barrier inside the
    approval source, an injected lease clock); nothing about the requirements changes.
    """

    root = directory / "project"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    (root / "tests" / "__init__.py").write_text("")
    (root / "pkg" / "calc.py").write_text(BROKEN)
    (root / "tests" / "test_calc.py").write_text(PROJECT_TESTS)

    # ---- host composition: workspace, evidence identity, durable and fenced state ------------
    filesystem = WorkspaceFilesystem(workspace_id="project", roots={"main": root})
    builder = SnapshotBuilder(filesystem, "main")
    runner = LocalSnapshotRunner(builder)
    artifacts = ArtifactManager()
    signer = EvidenceSigner(key=hashlib.sha256(b"example-signing-key").digest())
    pipeline = TrustedEvidencePipeline.build(
        artifacts=artifacts,
        signer=signer,
        bindings=[
            EvidenceBinding(
                command_id="unit.tests", requirement_id="coding.tests.run", evidence_type="test_run"
            ),
            EvidenceBinding(
                command_id="compile.check",
                requirement_id="coding.build.check",
                evidence_type="build_check",
            ),
        ],
        trusted_runners=[runner],
    )
    verifiers = VerifierRegistry()
    pipeline.install_verifiers(verifiers)
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
                timeout_seconds=60,
            ),
            CommandPreset(
                id="compile.check",
                category="test",
                executable="python",
                arguments=("-m", "compileall", "-q", "pkg"),
                timeout_seconds=60,
            ),
        ),
        observers=(pipeline.recorder,),
    )
    fingerprints = WorkspaceFingerprintProvider(builder)
    workspace = AuthorizedCodingWorkspace(
        filesystem=filesystem,
        root="main",
        fingerprints=fingerprints,
        catalog=catalog,
        runner=runner,
        pipeline=pipeline,
        identity="project-1",
    )
    store = SQLitePersistence(directory / "state.sqlite3")
    if clock is not None:
        store.lease_clock = clock
    leases = SQLiteLeaseStore(store)
    fenced = FencedExecution(persistence=store, leases=leases)
    runs = DurableRunManager(store)
    workers = WorkerRegistry()
    gate = CompletionGate(verifiers=verifiers, artifacts=artifacts)

    def runtime(model: ScriptedModel, tools: tuple[Any, ...], permissions: frozenset[str]) -> Any:
        return AgentRuntime(
            name="member",
            model=model,
            tools=tools,
            permissions=permissions,
            tool_executor=ToolExecutor(action_intent_store=store),
            max_steps=4,
        )

    # ---- roles: Lead (delegation), Coder (host edit), Tester, Reviewer -----------------------
    lead_config = LeadToolConfig(
        roles={
            "coder": LeadRoleBinding("coder", "coder", CAPABILITIES),
            "tester": LeadRoleBinding("tester", "tester", frozenset()),
            "lead2": LeadRoleBinding("lead2", "lead2", frozenset()),
            "reviewer": LeadRoleBinding("reviewer", "reviewer", frozenset()),
        },
        resource_allowlist=frozenset({"repo:main"}),
    )
    lead_tools = LeadTools(
        config=lead_config, assembler=LeadViewAssembler(runs=runs, tasks=fenced.task_store)
    )
    lead_names = tuple(definition.name for definition in lead_tools.definitions())
    lead_permissions = frozenset({"lead.view", "lead.propose"})
    scripts = _lead_script(scenario.layout)
    for role, capabilities in (("lead", CAPABILITIES), ("lead2", frozenset())):
        steps = [
            ModelResponse(text=step) if isinstance(step, str) else step
            for step in scripts.get("plan2" if role == "lead2" else "lead", [])
        ]
        workers.register(
            role,
            AgentRuntimeWorker(
                runtime=runtime(ScriptedModel(*steps), lead_tools.definitions(), lead_permissions),
                runs=runs,
                task_store=store,
                role=role,
                permissions=lead_permissions,
                tool_names=lead_names,
                allowed_capabilities=capabilities,
                lead=lead_tools,
                workspace=workspace,
            ),
        )

    async def apply_change(task: AgentTask, context: TaskExecutionContext) -> dict[str, Any]:
        if scenario.fix_code:
            filesystem.write_text("main", "pkg/calc.py", FIXED)
        return {
            "role": "coder",
            "summary": "Fixed add() to return a + b." if scenario.fix_code else "No change.",
        }

    workers.register("coder", FunctionWorker(apply_change))

    async def run_tests(task: AgentTask, context: TaskExecutionContext) -> dict[str, Any]:
        """A host-run test step: fixed presets run through the catalog and the evidence pipeline.

        The model never chooses a command here. (A model-driven `testing.run` tool may only run
        unattended on a runner that provides real isolation, such as `DockerSandboxRunner`; see
        `examples/coding-agent-worker`.) The trusted run and owner come from the stored task.
        """

        trusted = AgentContext(
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            session_id=task.session_id,
            trace_id=task.trace_id,
            metadata={
                "runtime_run_id": task.metadata["runtime_run_id"],
                "workspace_id": filesystem.workspace_id,
            },
        )
        references: list[str] = []
        for command_id in ("unit.tests", "compile.check"):
            executed = await catalog.execute(
                trusted, CommandRunInput(command_id=command_id, parameters={}), category="test"
            )
            references.extend(executed.artifact_refs)
        return {
            "role": "tester",
            "summary": "Executed the test presets.",
            "evidence_refs": references,
        }

    workers.register("tester", FunctionWorker(run_tests))

    review_tools = ReviewTools()

    async def reviewer_step(prompt: str, context: AgentContext) -> ModelResponse:
        # The scripted "model" cites the artifact of the command it wants to rely on.
        run_id = str(context.metadata["runtime_run_id"])
        wanted = "unit.tests" if scenario.cite == "tests" else "compile.check"
        cited = await _artifact_of_command(artifacts, context, run_id, wanted)
        findings = [{"severity": "info", "text": f"Relied on {wanted}", "evidence_refs": [cited]}]
        arguments: dict[str, Any] = {
            "recommendation": scenario.recommendation,
            "reviewed_tasks": ["code", "test"],
            "criteria": [{"criterion": "add() returns the sum", "verdict": "met"}],
            "findings": findings,
        }
        if scenario.recommendation == "changes_requested":
            arguments["requested_changes"] = ["Add a negative-number test."]
        return ModelResponse(
            tool_calls=[ToolCall(id="rv", name=review_contract.REVIEW_TOOL, arguments=arguments)]
        )

    reviewer_permissions = frozenset({"review.submit"})
    workers.register(
        "reviewer",
        AgentRuntimeWorker(
            runtime=runtime(
                ScriptedModel(reviewer_step, ModelResponse(text="reviewed")),
                review_tools.definitions(),
                reviewer_permissions,
            ),
            runs=runs,
            task_store=store,
            role="reviewer",
            permissions=reviewer_permissions,
            tool_names=(review_contract.REVIEW_TOOL,),
            artifacts=artifacts,
            evidence_signer=signer,
            fingerprints=fingerprints,
            conversations=InMemoryConversationStore(),
            cross_cycle=CrossCycleHandoffs(
                tasks=fenced.task_store, roles={role: role for role in ROLES}
            ),
            review=review_tools,
            workspace=workspace,
        ),
    )

    # ---- the controller: delegation, gate, acceptance, fenced completion ---------------------
    compiler = WorkflowCompiler(
        workers=workers,
        worker_allowlist=frozenset(ROLES),
        effective_capabilities=CAPABILITIES,
        resource_allowlist=frozenset({"repo:main"}),
    )
    delegation = WorkflowDelegation(
        source=DurableLeadProposalSource(
            tasks=fenced.task_store,
            runs=runs,
            config=lead_config,
            lead_workers=frozenset({"lead", "lead2"}),
            expected_workspace=workspace.binding,
        ),
        admission=WorkflowDelegationAdmission(
            admission=DelegationAdmission(
                policy_engine=PolicyEngine(
                    registry=PolicyRegistry(
                        [
                            DelegationPolicy(
                                max_depth=3,
                                max_parallel_jobs=3,
                                max_jobs_per_run=6,
                                allowed_delegates={"coder", "tester", "lead2", "reviewer"},
                            )
                        ]
                    )
                )
            ),
            runs=runs,
            tasks=fenced.task_store,
            compiler=compiler,
            delegators={
                "lead": DelegationAuthority("lead", "lead", CAPABILITIES),
                "lead2": DelegationAuthority("lead2", "lead2", frozenset()),
            },
            parent_statuses=frozenset({TaskStatus.COMPLETED}),
            expected_workspace=workspace.binding,
        ),
        max_rounds=2,
    )
    approver = approver or _approver(scenario, root)
    acceptance = TeamAcceptance(
        policy=TeamAcceptancePolicy(
            reviewer_workers={"reviewer": "reviewer"},
            required_tasks=frozenset({"code", "test"}),
            human_approval=approver,
        ),
        tasks=fenced.task_store,
        runs=runs,
        assembler=TaskContextAssembler(store=store, artifacts=artifacts, evidence_signer=signer),
    )
    controller = WorkflowController(
        runs=runs,
        compiler=compiler,
        scheduler_factory=lambda graph: TaskScheduler(
            graph=graph,
            workers=workers,
            store=fenced.task_store,
            control=RunControl(checkpoints=store),
        ),
        completion_gate=gate,
        leases=leases,
        fenced=fenced,
        lease_owner_id="host-1",
        action_intents=store,
        fingerprint_provider=fingerprints,
        workspace_binding=workspace.binding,
        delegation=delegation,
        team_acceptance=acceptance if scenario.team_acceptance else None,
    )
    standards = StandardsResolver(StandardsRegistry(coding_standards_packs())).resolve(
        [VersionedReference(id="coding.tests", version=1)],
        condition_facts={"has_executable_tests": True},
    )
    run = await runs.create(
        identity=_owner(),
        autonomy=AutonomyLevel.AUTONOMOUS,
        version_pins=_pins(),
        workspace_binding=workspace.binding,
    )
    plan = WorkflowPlan(
        objective="Fix add()",
        tasks=(
            WorkflowTaskSpec(
                id="lead",
                name="Plan the delivery",
                worker_id="lead",
                capabilities=CAPABILITIES,
            ),
        ),
    )

    async def execute() -> WorkflowOutcome:
        current = await runs.get(run_id=run.id, identity=_owner())
        return await controller.execute(
            run=current or run,
            identity=_owner(),
            plan=plan,
            standards=standards,
            protected_input_fingerprint="objective:fix-add",
        )

    outcome = await execute()
    final = await runs.get(run_id=run.id, identity=_owner())
    return Result(
        scenario,
        outcome,
        final.status.value if final else "unknown",
        run_id=run.id,
        approver=approver,
        replay=execute,
    )


def _approver(scenario: Scenario, root: Path) -> ExampleApprover | None:
    if scenario.approval is None:
        return None
    if scenario.approval == "approve_after_edit":

        def edit_workspace() -> None:
            (root / "pkg" / "calc.py").write_text(FIXED + "# edited during the approval\n")

        return ExampleApprover("approve", on_ask=edit_workspace)
    return ExampleApprover(scenario.approval)


async def _artifact_of_command(
    artifacts: ArtifactManager, context: AgentContext, run_id: str, command_id: str
) -> str:
    """The reference of the run's evidence artifact recorded for one command (host lookup)."""

    scope = ArtifactContext(
        tenant_id=context.tenant_id or "",
        user_id=context.user_id or "",
        session_id=context.session_id,
        trace_id="scripted-model",
        run_id=run_id,
    )
    for metadata in await artifacts.list(scope, limit=100):
        if metadata.run_id == run_id and metadata.labels.get("command_id") == command_id:
            return metadata.id
    raise LookupError(f"no evidence artifact for {command_id}")


async def main_async(names: list[str]) -> int:
    chosen = [s for s in SCENARIOS if not names or s.name in names]
    if not chosen:
        print("Unknown scenario. Available: " + ", ".join(s.name for s in SCENARIOS))
        return 2
    failures = 0
    for scenario in chosen:
        with tempfile.TemporaryDirectory(prefix="kaiwen-team-") as directory:
            result = await run_scenario(scenario, Path(directory))
        marker = "ok " if result.as_expected else "BAD"
        print(
            f"[{marker}] {scenario.name:36s} -> {result.outcome.status.value:9s} "
            f"run={result.run_status:9s} reason={result.outcome.reason} "
            f"acceptance={result.acceptance}"
        )
        failures += 0 if result.as_expected else 1
    if failures:
        print(f"{failures} scenario(s) did not end as documented.")
        return 1
    print("Every scenario ended as documented; only the accepted deliveries completed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async(sys.argv[1:])))

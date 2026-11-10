"""M6.3.3-C1: cycle-boundary delegation in `WorkflowController` (ADR 0015).

Real SQLite, a real `TaskScheduler` over the fenced task store, a real `SQLiteLeaseStore` and the
real controller (delegation requires fenced execution, ADR 0016). The delegation
source and the gate are scripted so every outcome is deterministic. What is pinned:

- delegation is opt-in and changes nothing when absent;
- a candidate becomes executable only after a fresh, explicit allow at the boundary, and the next
  cycle's plan is exactly the admitted task tuple (no planner is in the path);
- anything short of that blocks the run with an explicit reason: it never runs, never completes
  the run and never masquerades as a task or gate failure;
- the completion gate alone still judges completion, after the delegation rounds;
- replays re-authorize from scratch. Admission is not an atomic reservation: the guarantee comes
  from the owner epoch enforced inside every protected write (ADR 0016), and durable proposal
  idempotency is PR-C2.

Every wait is bounded or event-driven; nothing here depends on timing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from kaiwen_agent.control import RunControl
from kaiwen_agent.coordination import InMemoryLeaseStore, SQLiteLeaseStore
from kaiwen_agent.exceptions import FencedConfigurationError, RunOwnershipError
from kaiwen_agent.persistence import SQLitePersistence
from kaiwen_agent.policy import (
    DelegationAdmission,
    DelegationPolicy,
    PolicyDecisionType,
    PolicyEngine,
    PolicyRegistry,
)
from kaiwen_agent.quality import CompletionGateResult, CompletionGateStatus
from kaiwen_agent.runtime import DurableRunManager, RuntimeRunStatus, WorkspaceBinding
from kaiwen_agent.standards import StandardResolution
from kaiwen_agent.tasks import AgentTask, TaskScheduler, TaskStatus
from kaiwen_agent.tools.durability import ActionIntentStatus
from kaiwen_agent.workers import FunctionWorker
from kaiwen_agent.workflow import (
    DelegationAuthority,
    DelegationBoundary,
    DelegationCandidate,
    FencedExecution,
    WorkflowCompiler,
    WorkflowController,
    WorkflowDelegation,
    WorkflowDelegationAdmission,
    WorkflowLimits,
    WorkflowOutcome,
    WorkflowOutcomeStatus,
    WorkflowPlan,
    WorkflowRevisionReason,
    WorkflowRevisionRequest,
    WorkflowTaskSpec,
)
from tests.test_agent_runtime_worker import owner
from tests.test_agent_runtime_worker import plan as make_plan
from tests.test_agent_runtime_worker import spec as lead_spec
from tests.test_delegation_job_path import SECRET, Rule
from tests.test_durable_action_scope import legacy_intent
from tests.test_fenced_writes import FencedFlow
from tests.test_workflow_delegation_admission import proposal
from tests.test_workflow_delegation_admission import spec as child_spec

TIMEOUT = 40
BINDING = WorkspaceBinding(workspace_id="ws-1", root="main", policy_checksum="policy-1")
LEAD_PLAN = make_plan(lead_spec("lead", "lead", capabilities=frozenset({"logs.read"})))


def run_async(factory: Any) -> Any:
    async def bounded() -> Any:
        return await asyncio.wait_for(factory(), timeout=TIMEOUT)

    return asyncio.run(bounded())


class RoleWorker(FunctionWorker):
    """A worker that declares a role, as `AgentRuntimeWorker` does."""

    role = "reviewer"


class RestrictedWorker(FunctionWorker):
    """A worker that declares `allowed_capabilities`, as `AgentRuntimeWorker` does."""

    allowed_capabilities: frozenset[str] = frozenset()


class SpyGate:
    """A completion gate whose verdict is scripted and whose use is recorded."""

    def __init__(self, harness: Harness) -> None:
        self.harness = harness
        self.status = CompletionGateStatus.PASS
        self.calls = 0

    async def evaluate(self, resolution: Any, *, context: Any) -> CompletionGateResult:
        self.calls += 1
        self.harness.events.append("gate")
        return CompletionGateResult(status=self.status, verifications=())


class Source:
    """A scripted host delegation source: answers in order, then nothing."""

    def __init__(self, *answers: Any, default: Any = None) -> None:
        self.answers = list(answers)
        self.default = default
        self.requests: list[WorkflowRevisionRequest] = []

    async def propose(self, request: WorkflowRevisionRequest) -> DelegationCandidate | None:
        self.requests.append(request)
        answer = self.answers.pop(0) if self.answers else self.default
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            answer = answer(request)
            if isinstance(answer, Awaitable):
                answer = await answer
        return answer  # type: ignore[no-any-return]


def propose_for(logical: str, *specs: WorkflowTaskSpec) -> Callable[..., Any]:
    """An answer naming the eligible parent with this logical id and an untrusted proposal."""

    def answer(request: WorkflowRevisionRequest) -> DelegationCandidate:
        assert request.delegation is not None
        parent = next(
            item for item in request.delegation.eligible_parents if item.logical_task_id == logical
        )
        return DelegationCandidate(parent.task_id, proposal(*(specs or (child_spec("a"),))))

    return answer


def named(harness: Any, logical: str, *specs: WorkflowTaskSpec) -> Callable[..., Any]:
    """An answer naming a task that ran earlier, by its real id, whether or not it is eligible."""

    def answer(request: WorkflowRevisionRequest) -> DelegationCandidate:
        task = next(t for t in harness.executed if t.metadata["logical_task_id"] == logical)
        return DelegationCandidate(task.id, proposal(*(specs or (child_spec("a"),))))

    return answer


class Harness:
    """A real store and run, a compiler, a gate spy and a controller with delegation enabled."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        source: Source | None = None,
        rules: list[Any] | None = None,
        delegators: dict[str, DelegationAuthority] | None = None,
        workers: dict[str, FunctionWorker] | None = None,
        limits: WorkflowLimits | None = None,
        max_rounds: int = 1,
        binding: WorkspaceBinding | None = None,
        planner: Any = None,
        lease_class: type[SQLiteLeaseStore] = SQLiteLeaseStore,
    ) -> None:
        self.flow = FencedFlow(tmp_path)
        if lease_class is not SQLiteLeaseStore:
            self.flow.leases = lease_class(self.flow.store)
            self.flow.fenced = FencedExecution(persistence=self.flow.store, leases=self.flow.leases)
        self.store = self.flow.store
        self.source = source or Source()
        self.events: list[str] = []
        self.executed: list[AgentTask] = []
        self.graphs: list[Any] = []
        self.gate = SpyGate(self)
        self.leases = self.flow.leases
        self.runtime_reads: dict[int, Any] = {}
        # logical task id -> awaited inside that worker, to lose ownership at an exact point
        self.on_run: dict[str, Callable[[], Awaitable[None]]] = {}
        self.max_rounds = max_rounds
        self.binding = binding
        self.planner = planner
        self.rules = (
            rules
            if rules is not None
            else [
                DelegationPolicy(
                    max_depth=3,
                    max_parallel_jobs=3,
                    max_jobs_per_run=6,
                    allowed_delegates={"coder", "lead"},
                )
            ]
        )
        self.delegators = (
            delegators
            if delegators is not None
            else {"lead": DelegationAuthority("lead", "lead", frozenset({"logs.read"}))}
        )

        async def work(task: AgentTask, context: Any) -> dict[str, Any]:
            logical = str(task.metadata.get("logical_task_id"))
            self.events.append(f"run:{logical}")
            self.executed.append(task)
            if logical in self.on_run:
                await self.on_run[logical]()
            if logical == "boom":
                raise RuntimeError("worker failed")
            return {"summary": f"{logical} done"}

        for name in ("lead", "coder"):
            cls = (workers or {}).get(name)
            self.flow.workers.register(name, cls if cls is not None else FunctionWorker(work))
        for name, worker in (workers or {}).items():
            if name not in {"lead", "coder"}:
                self.flow.workers.register(name, worker)
        # Restricted/Role workers need the same execution body as the default ones.
        for name in ("lead", "coder"):
            worker = (workers or {}).get(name)
            if worker is not None:
                worker.function = work
        self.compiler = WorkflowCompiler(
            workers=self.flow.workers,
            worker_allowlist=frozenset({"lead", "coder", "boom"}),
            effective_capabilities=frozenset({"logs.read", "deploy"}),
            resource_allowlist=frozenset({"repo:main"}),
            limits=limits,
        )

    def factory(self, graph: Any) -> TaskScheduler:
        self.graphs.append(graph)
        return TaskScheduler(
            graph=graph,
            workers=self.flow.workers,
            store=self.flow.fenced.task_store,
            control=RunControl(checkpoints=self.store),
            max_concurrency=2,
        )

    def admission(
        self,
        *,
        delegators: dict[str, DelegationAuthority] | None = None,
        tasks: Any = None,
        swap_after_build: bool = True,
        **overrides: Any,
    ) -> WorkflowDelegationAdmission:
        settings: dict[str, Any] = {
            "admission": DelegationAdmission(
                policy_engine=PolicyEngine(registry=PolicyRegistry(self.rules))
            ),
            "runs": self.flow.runs,
            "tasks": self.flow.fenced.task_store,
            "compiler": self.compiler,
            "delegators": self.delegators if delegators is None else delegators,
            "parent_statuses": frozenset({TaskStatus.COMPLETED}),
            "expected_workspace": self.binding,
        }
        if tasks is not None and not swap_after_build:
            settings["tasks"] = tasks
        settings.update(overrides)
        built = WorkflowDelegationAdmission(**settings)
        if tasks is not None and swap_after_build:
            # A read path that misbehaves at run time (a tampered or failing listing). The
            # controller only accepts the fenced store at configuration, so `controller()` swaps
            # this in after construction: it changes what admission reads, never where anything
            # is written.
            self.runtime_reads[id(built)] = tasks
        return built

    def controller(
        self, *, admission: WorkflowDelegationAdmission | None = None, **options: Any
    ) -> WorkflowController:
        settings: dict[str, Any] = {
            "runs": self.flow.runs,
            "compiler": self.compiler,
            "scheduler_factory": self.factory,
            "completion_gate": self.gate,
            "revision_planner": self.planner,
            "leases": self.leases,
            "fenced": self.flow.fenced,
            "lease_owner_id": "controller-1",
            "action_intents": self.store,
            "workspace_binding": self.binding,
            "delegation": WorkflowDelegation(
                source=self.source,
                admission=admission or self.admission(),
                max_rounds=self.max_rounds,
            ),
        }
        settings.update(options)
        built = WorkflowController(**settings)
        delegation = settings["delegation"]
        if delegation is not None and id(delegation.admission) in self.runtime_reads:
            delegation.admission.tasks = self.runtime_reads[id(delegation.admission)]
        return built

    async def new_run(self) -> Any:
        return await self.flow.new_run(self.binding)

    async def execute(
        self,
        run: Any,
        workflow: WorkflowPlan = LEAD_PLAN,
        controller: WorkflowController | None = None,
    ) -> WorkflowOutcome:
        fresh = await self.flow.runs.get(run_id=run.id, identity=owner())
        assert fresh is not None
        return await (controller or self.controller()).execute(
            run=fresh,
            identity=owner(),
            plan=workflow,
            standards=StandardResolution(pack_refs=(), requirements=(), prompt_refs=()),
            protected_input_fingerprint="objective-1",
        )

    async def status(self, run: Any) -> RuntimeRunStatus:
        record = await self.flow.runs.get(run_id=run.id, identity=owner())
        assert record is not None
        return record.status

    async def logical_ids(self, run: Any) -> list[str]:
        page = await self.store.list_run_tasks(
            tenant_id="tenant-1", user_id="user-1", runtime_run_id=run.id, limit=200
        )
        return sorted(str(task.metadata.get("logical_task_id")) for task in page.tasks)

    @property
    def ran(self) -> list[str]:
        return [str(task.metadata.get("logical_task_id")) for task in self.executed]


def blocked(outcome: WorkflowOutcome, reason: str) -> None:
    assert outcome.status is WorkflowOutcomeStatus.BLOCKED and outcome.reason == reason


# ------------------------------------------------------------------ opt-in and compatibility


def test_a_controller_without_delegation_behaves_exactly_as_before(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(tmp_path)
        run = await harness.new_run()
        outcome = await harness.execute(run, controller=harness.controller(delegation=None))
        assert outcome.status is WorkflowOutcomeStatus.COMPLETED and outcome.cycles == 1
        assert harness.source.requests == []  # never consulted when delegation is not configured
        assert harness.events == ["run:lead", "gate"] and harness.gate.calls == 1

    run_async(scenario)


def test_a_source_with_no_candidate_leaves_completion_to_the_gate_unchanged(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = Harness(tmp_path, source=Source())
        run = await harness.new_run()
        outcome = await harness.execute(run)
        assert outcome.status is WorkflowOutcomeStatus.COMPLETED and outcome.cycles == 1
        assert harness.events == ["run:lead", "gate"]
        assert len(harness.source.requests) == 1  # consulted once at the boundary, no more

    run_async(scenario)


def test_the_revision_reason_is_explicit_and_never_carries_a_failure() -> None:
    base = {"run_id": "r", "cycle": 1, "prior_plan": LEAD_PLAN}
    boundary = DelegationBoundary()
    ok = WorkflowRevisionRequest(
        **base, reason=WorkflowRevisionReason.DELEGATION_REQUESTED, delegation=boundary
    )
    assert ok.delegation == boundary and ok.reason.value == "delegation_requested"
    assert WorkflowRevisionReason.DELEGATION_REQUESTED not in {
        WorkflowRevisionReason.EXECUTION_FAILED,
        WorkflowRevisionReason.QUALITY_BLOCKED,
    }
    for reason in (WorkflowRevisionReason.EXECUTION_FAILED, WorkflowRevisionReason.QUALITY_BLOCKED):
        assert WorkflowRevisionRequest(**base, reason=reason).delegation is None  # as before
        with pytest.raises(ValidationError):
            WorkflowRevisionRequest(**base, reason=reason, delegation=boundary)
    with pytest.raises(ValidationError):  # a delegation request needs its boundary data
        WorkflowRevisionRequest(**base, reason=WorkflowRevisionReason.DELEGATION_REQUESTED)
    with pytest.raises(ValidationError):  # and may not smuggle a failure or a gate result
        WorkflowRevisionRequest(
            **base,
            reason=WorkflowRevisionReason.DELEGATION_REQUESTED,
            delegation=boundary,
            quality_result=CompletionGateResult(
                status=CompletionGateStatus.BLOCKED, verifications=()
            ),
        )


def test_the_source_is_called_with_a_typed_delegation_request_owned_by_the_controller(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = Harness(tmp_path, source=Source(), max_rounds=2)
        run = await harness.new_run()
        await harness.execute(run)
        (request,) = harness.source.requests
        assert request.reason is WorkflowRevisionReason.DELEGATION_REQUESTED
        assert (request.run_id, request.cycle) == (run.id, 1)
        assert request.task_failures == () and request.quality_result is None
        assert request.prior_plan == LEAD_PLAN
        assert request.delegation is not None
        assert (request.delegation.rounds_used, request.delegation.rounds_allowed) == (0, 2)
        (parent,) = request.delegation.eligible_parents
        assert (parent.logical_task_id, parent.worker_id, parent.depth) == ("lead", "lead", 0)
        assert parent.task_id == harness.executed[0].id  # the controller's own task result

    run_async(scenario)


# ------------------------------------------------------------------ an admitted delegation


def test_an_admitted_delegation_runs_next_cycle_and_the_gate_judges_at_the_end(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        wanted = child_spec(
            "a", capabilities=frozenset({"logs.read"}), resource_keys=("repo:main",)
        )
        harness = Harness(tmp_path, source=Source(propose_for("lead", wanted)))
        run = await harness.new_run()
        outcome = await harness.execute(run)
        assert outcome.status is WorkflowOutcomeStatus.COMPLETED and outcome.cycles == 2
        # The Lead ran once, the delegated child once, and the gate judged only at the end.
        assert harness.events == ["run:lead", "run:a", "gate"] and harness.gate.calls == 1
        lead, child = harness.executed
        stored = await harness.store.get_task(child.id)
        assert stored is not None and stored.status is TaskStatus.COMPLETED
        # Lineage is written by the compiler from the controller's admission, not by the plan.
        assert stored.parent_id == lead.id and stored.delegation_depth == 1
        assert stored.agent_id == "coder" and stored.metadata["workflow_cycle"] == 2
        assert (stored.capabilities, stored.resource_keys) == (
            wanted.capabilities,
            list(wanted.resource_keys),
        )
        assert sorted(await harness.logical_ids(run)) == ["a", "lead"]
        assert await harness.status(run) is RuntimeRunStatus.COMPLETED

    run_async(scenario)


def test_the_delegated_plan_is_exactly_the_admitted_tuple_and_no_planner_is_consulted(
    tmp_path: Path,
) -> None:
    class Planner:
        def __init__(self) -> None:
            self.requests: list[WorkflowRevisionRequest] = []

        async def revise(self, request: WorkflowRevisionRequest) -> WorkflowPlan | None:
            self.requests.append(request)
            # A planner that tried to substitute unauthorized work if it were ever asked.
            return make_plan(lead_spec("evil", "coder", capabilities=frozenset({"deploy"})))

    async def scenario() -> None:
        planner = Planner()
        admitted = (child_spec("a"), child_spec("b", dependencies=("a",)))
        harness = Harness(tmp_path, source=Source(propose_for("lead", *admitted)), planner=planner)
        run = await harness.new_run()
        outcome = await harness.execute(run)
        assert outcome.status is WorkflowOutcomeStatus.COMPLETED
        assert planner.requests == []  # delegation never goes through a revision planner
        assert harness.ran == ["lead", "a", "b"] and "evil" not in await harness.logical_ids(run)
        graph = harness.graphs[1]
        assert sorted(task.metadata["logical_task_id"] for task in graph.tasks()) == ["a", "b"]

    run_async(scenario)


def test_no_task_graph_is_mutated_while_it_executes(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(tmp_path, source=Source(propose_for("lead")))
        run = await harness.new_run()
        await harness.execute(run)
        first, second = harness.graphs  # one compiled graph per cycle, never extended
        assert [t.metadata["logical_task_id"] for t in first.tasks()] == ["lead"]
        assert [t.metadata["logical_task_id"] for t in second.tasks()] == ["a"]
        assert first is not second

    run_async(scenario)


def test_nested_delegation_is_explicitly_authorized_depth_derived_and_bounded(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        both = {
            "lead": DelegationAuthority("lead", "lead", frozenset({"logs.read"})),
            "coder": DelegationAuthority("coder", "builder", frozenset({"logs.read"})),
        }
        harness = Harness(
            tmp_path,
            source=Source(
                propose_for("lead", child_spec("c1")), propose_for("c1", child_spec("g1"))
            ),
            delegators=both,
            max_rounds=2,
        )
        run = await harness.new_run()
        outcome = await harness.execute(run)
        assert outcome.status is WorkflowOutcomeStatus.COMPLETED and outcome.cycles == 3
        assert harness.ran == ["lead", "c1", "g1"]
        c1, g1 = harness.executed[1:]
        stored = await harness.store.get_task(g1.id)
        assert stored is not None and stored.delegation_depth == 2 and stored.parent_id == c1.id
        # The existing depth limit still bounds it: a maximum of one blocks the second level.
        capped = Harness(
            tmp_path / "capped",
            source=Source(
                propose_for("lead", child_spec("c1")), propose_for("c1", child_spec("g1"))
            ),
            delegators=both,
            max_rounds=2,
            rules=[
                DelegationPolicy(
                    max_depth=1,
                    max_parallel_jobs=3,
                    max_jobs_per_run=6,
                    allowed_delegates={"coder"},
                )
            ],
        )
        run2 = await capped.new_run()
        blocked(await capped.execute(run2), "delegation_denied:policy_denied")
        assert capped.ran == ["lead", "c1"]

    run_async(scenario)


# ------------------------------------------------------------------ eligibility of the parent


def test_only_a_task_completed_in_the_cycle_that_just_ended_may_delegate(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(tmp_path, max_rounds=2)
        run = await harness.new_run()
        # The second answer names the Lead, which completed in cycle 1, not in cycle 2.
        harness.source = Source(
            propose_for("lead", child_spec("c1")), named(harness, "lead", child_spec("c2"))
        )
        outcome = await harness.execute(run)
        blocked(outcome, "delegation_parent_not_eligible")
        assert harness.ran == ["lead", "c1"] and harness.gate.calls == 0
        assert await harness.status(run) is RuntimeRunStatus.BLOCKED

    run_async(scenario)


def test_a_made_up_or_foreign_parent_is_not_eligible(tmp_path: Path) -> None:
    async def scenario() -> None:
        for name, parent in (
            ("made_up", "task_does_not_exist"),
            ("other_run", "task_of_another_run_or_tenant"),
        ):
            harness = Harness(
                tmp_path / name,
                source=Source(
                    lambda request, parent=parent: DelegationCandidate(parent, proposal())
                ),
            )
            run = await harness.new_run()
            blocked(await harness.execute(run), "delegation_parent_not_eligible")
            assert harness.ran == ["lead"] and harness.gate.calls == 0

    run_async(scenario)


# ------------------------------------------------------------------ authorization failures


def worker_kwargs(**named: Any) -> dict[str, FunctionWorker]:
    return {name: factory() for name, factory in named.items()}


DENIALS: list[tuple[str, dict[str, Any], tuple[WorkflowTaskSpec, ...], str]] = [
    (
        "unauthorized_delegator",
        {"delegators": {}},
        (child_spec("a"),),
        "delegation_denied:delegator_not_authorized",
    ),
    (
        "capability_escalation",
        {},
        (child_spec("a", capabilities=frozenset({"deploy"})),),
        "delegation_denied:capability_escalation",
    ),
    (
        "child_worker_capability_mismatch",
        {"workers": worker_kwargs(coder=lambda: RestrictedWorker(None))},  # type: ignore[arg-type]
        (child_spec("a", capabilities=frozenset({"logs.read"})),),
        "delegation_denied:worker_capability_mismatch",
    ),
    (
        "delegator_role_mismatch",
        {"workers": worker_kwargs(lead=lambda: RoleWorker(None))},  # type: ignore[arg-type]
        (child_spec("a"),),
        "delegation_denied:delegator_role_mismatch",
    ),
    ("unknown_worker", {}, (child_spec("a", "ghost"),), "delegation_denied:worker_not_allowed"),
    (
        "resource_key",
        {},
        (child_spec("a", resource_keys=("repo:other",)),),
        "delegation_denied:resource_not_allowed",
    ),
    (
        "invalid_plan",
        {},
        (child_spec("a", dependencies=("missing",)),),
        "delegation_denied:plan_invalid",
    ),
    (
        "policy_deny",
        {"rules": [Rule(PolicyDecisionType.DENY, "no")]},
        (child_spec("a"),),
        "delegation_denied:policy_denied",
    ),
    (
        "policy_error",
        {"rules": [Rule(RuntimeError(SECRET))]},
        (child_spec("a"),),
        "delegation_denied:policy_unavailable",
    ),
    (
        "approval",
        {"rules": [Rule(PolicyDecisionType.REQUIRE_APPROVAL)]},
        (child_spec("a"),),
        "delegation_approval_required:approval_required",
    ),
    (
        "review",
        {"rules": [Rule(PolicyDecisionType.REVIEW)]},
        (child_spec("a"),),
        "delegation_approval_required:review_required",
    ),
    (
        "modify",
        {"rules": [Rule(PolicyDecisionType.MODIFY)]},
        (child_spec("a"),),
        "delegation_approval_required:modification_required",
    ),
]


@pytest.mark.parametrize(
    ("kwargs", "specs", "reason"),
    [pytest.param(*case[1:], id=case[0]) for case in DENIALS],
)
def test_anything_short_of_a_fresh_allow_blocks_the_run_and_runs_nothing(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    kwargs: dict[str, Any],
    specs: tuple[WorkflowTaskSpec, ...],
    reason: str,
) -> None:
    async def scenario() -> None:
        caplog.set_level(logging.DEBUG)
        harness = Harness(tmp_path, source=Source(propose_for("lead", *specs)), **kwargs)
        run = await harness.new_run()
        outcome = await harness.execute(run)
        blocked(outcome, reason)
        # Neither a pending nor a rejected delegation is a completion, a failure or a gate result.
        assert await harness.status(run) is RuntimeRunStatus.BLOCKED
        assert harness.gate.calls == 0 and outcome.quality_result is None
        assert harness.ran == ["lead"]  # the delegated work never ran
        assert await harness.logical_ids(run) == ["lead"]  # and was never even stored
        assert SECRET not in (outcome.reason or "")

    run_async(scenario)
    assert "SECRET" not in caplog.text and "hunter2" not in caplog.text


def test_storage_failure_at_the_boundary_fails_closed(tmp_path: Path) -> None:
    class FailingTasks:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        async def list_run_tasks(self, **_: Any) -> Any:
            raise RuntimeError(SECRET)

    async def scenario() -> None:
        harness = Harness(tmp_path, source=Source(propose_for("lead")))
        admission = harness.admission(tasks=FailingTasks(harness.store))
        run = await harness.new_run()
        outcome = await harness.execute(run, controller=harness.controller(admission=admission))
        blocked(outcome, "delegation_denied:state_unavailable")
        assert harness.ran == ["lead"] and harness.gate.calls == 0
        assert SECRET not in (outcome.reason or "")

    run_async(scenario)


def test_a_stale_preliminary_allow_is_not_final_authorization(tmp_path: Path) -> None:
    """The source earns a preliminary allow, then live state changes before the boundary check."""

    async def scenario() -> None:
        rules = [
            DelegationPolicy(
                max_depth=3, max_parallel_jobs=1, max_jobs_per_run=6, allowed_delegates={"coder"}
            )
        ]
        harness = Harness(tmp_path, rules=rules)
        run = await harness.new_run()
        preliminary: list[Any] = []

        async def answer(request: WorkflowRevisionRequest) -> DelegationCandidate:
            candidate = propose_for("lead", child_spec("a"))(request)
            decision = await harness.admission().evaluate(
                run_id=run.id,
                identity=owner(),
                parent_task_id=candidate.parent_task_id,
                proposal=candidate.proposal,
            )
            preliminary.append(decision)
            # Live state changes after that answer: another delegated task of the Lead is active.
            parent = next(t for t in harness.executed if t.agent_id == "lead")
            busy = AgentTask(
                id="task_busy",
                name="busy",
                agent_id="coder",
                session_id="session-1",
                tenant_id="tenant-1",
                user_id="user-1",
                trace_id="trace-1",
                status=TaskStatus.RUNNING,
                delegation_depth=1,
                parent_id=parent.id,
                metadata={"runtime_run_id": run.id, "logical_task_id": "busy", "workflow_cycle": 1},
            )
            await harness.store.save_task(busy)
            return candidate

        harness.source = Source(answer)
        outcome = await harness.execute(run)
        assert preliminary[0].admitted  # the preliminary answer really was an allow
        blocked(outcome, "delegation_denied:policy_denied")  # the final evaluation says no
        assert harness.ran == ["lead"]

    run_async(scenario)


# ------------------------------------------------------------------ bounds, duplicates, failures


def test_a_replayed_candidate_cannot_delegate_twice_because_a_parent_has_one_boundary(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        both = {
            "lead": DelegationAuthority("lead", "lead", frozenset({"logs.read"})),
            "coder": DelegationAuthority("coder", "builder", frozenset({"logs.read"})),
        }
        harness = Harness(tmp_path, delegators=both, max_rounds=3)
        run = await harness.new_run()
        # The very same candidate is delivered again at the next boundary by naming the Lead.
        harness.source = Source(
            propose_for("lead", child_spec("a")), named(harness, "lead", child_spec("a"))
        )
        outcome = await harness.execute(run)
        # Task ids carry their cycle, so the Lead is eligible at exactly one boundary.
        blocked(outcome, "delegation_parent_not_eligible")
        assert harness.ran == ["lead", "a"]  # the child ran exactly once
        assert await harness.logical_ids(run) == ["a", "lead"]  # and exists once

    run_async(scenario)


def test_rounds_and_cycles_are_bounded_and_an_excess_candidate_blocks_explicitly(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        both = {
            "lead": DelegationAuthority("lead", "lead", frozenset({"logs.read"})),
            "coder": DelegationAuthority("coder", "builder", frozenset({"logs.read"})),
        }

        def endless(request: WorkflowRevisionRequest) -> DelegationCandidate:
            assert request.delegation is not None
            parent = request.delegation.eligible_parents[0]
            return DelegationCandidate(parent.task_id, proposal(child_spec(f"c{request.cycle}")))

        harness = Harness(tmp_path, source=Source(default=endless), delegators=both, max_rounds=2)
        run = await harness.new_run()
        outcome = await harness.execute(run)
        blocked(outcome, "delegation_round_limit")
        assert harness.ran == ["lead", "c1", "c2"] and harness.gate.calls == 0
        assert len(harness.source.requests) == 3  # consulted at each boundary, never more

        # A candidate with no cycle left blocks explicitly instead of being ignored.
        tight = Harness(
            tmp_path / "tight",
            source=Source(propose_for("lead")),
            limits=WorkflowLimits(max_cycles=1),
        )
        run2 = await tight.new_run()
        blocked(await tight.execute(run2), "delegation_cycle_limit")
        assert tight.ran == ["lead"] and tight.gate.calls == 0

    run_async(scenario)


def test_a_failing_or_malformed_source_blocks_without_leaking(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        caplog.set_level(logging.DEBUG)
        failing = Harness(tmp_path / "fail", source=Source(RuntimeError(SECRET)))
        run = await failing.new_run()
        outcome = await failing.execute(run)
        blocked(outcome, "delegation_source_failed")
        assert SECRET not in (outcome.reason or "") and failing.gate.calls == 0
        malformed = Harness(tmp_path / "bad", source=Source(lambda request: {"parent": "x"}))
        run2 = await malformed.new_run()
        blocked(await malformed.execute(run2), "delegation_invalid_candidate")
        assert malformed.ran == ["lead"] and malformed.gate.calls == 0

    run_async(scenario)
    assert "SECRET" not in caplog.text and "hunter2" not in caplog.text


def test_a_task_failure_is_never_replaced_by_delegation(tmp_path: Path) -> None:
    class Planner:
        def __init__(self) -> None:
            self.reasons: list[WorkflowRevisionReason] = []

        async def revise(self, request: WorkflowRevisionRequest) -> WorkflowPlan | None:
            self.reasons.append(request.reason)
            return None

    async def scenario() -> None:
        planner = Planner()
        harness = Harness(
            tmp_path,
            source=Source(propose_for("lead")),
            planner=planner,
            delegators={"boom": DelegationAuthority("boom", "boom", frozenset())},
        )
        harness.flow.workers.register("boom", FunctionWorker(_raising))
        run = await harness.new_run()
        outcome = await harness.execute(run, make_plan(lead_spec("boom", "boom")))
        assert outcome.status is WorkflowOutcomeStatus.FAILED
        assert outcome.reason == "task_execution_failed"
        assert harness.source.requests == []  # a failed cycle never reaches the delegation boundary
        assert planner.reasons == [WorkflowRevisionReason.EXECUTION_FAILED]

    run_async(scenario)


async def _raising(task: AgentTask, context: Any) -> dict[str, Any]:
    raise RuntimeError("worker failed")


def test_the_completion_gate_alone_still_decides_after_a_delegation(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(tmp_path, source=Source(propose_for("lead")))
        harness.gate.status = CompletionGateStatus.BLOCKED
        run = await harness.new_run()
        outcome = await harness.execute(run)
        # The delegated child completed, yet the run is not complete: the gate said no.
        blocked(outcome, "quality_blocked")
        assert outcome.cycles == 2 and harness.ran == ["lead", "a"] and harness.gate.calls == 1
        assert outcome.status is not WorkflowOutcomeStatus.COMPLETED

    run_async(scenario)


# ------------------------------------------------------------------ configuration fails closed


def test_delegation_is_refused_at_configuration_when_it_cannot_be_made_safe(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)

    def build(**options: Any) -> WorkflowController:
        return harness.controller(**options)

    # Delegation needs the authoritative SQLite fencing; every other composition is refused.
    with pytest.raises(FencedConfigurationError, match="requires fenced execution"):
        build(fenced=None)
    with pytest.raises(FencedConfigurationError, match="fenced lease store"):
        build(leases=None)
    with pytest.raises(FencedConfigurationError, match="fenced lease store"):
        build(leases=InMemoryLeaseStore())
    with pytest.raises(FencedConfigurationError):
        FencedExecution(persistence=harness.store, leases=InMemoryLeaseStore())  # type: ignore[arg-type]
    with pytest.raises(FencedConfigurationError, match="same durable runs"):
        build(admission=harness.admission(runs=DurableRunManager(harness.store)))
    elsewhere = SQLitePersistence(tmp_path / "elsewhere.sqlite3")
    with pytest.raises(FencedConfigurationError, match="fenced database"):
        build(admission=harness.admission(tasks=elsewhere, swap_after_build=False))
    other = WorkflowCompiler(
        workers=harness.flow.workers,
        worker_allowlist=frozenset({"lead", "coder"}),
        effective_capabilities=frozenset({"logs.read"}),
    )
    with pytest.raises(ValueError, match="compiler"):
        build(compiler=other)
    running_parents = harness.admission(parent_statuses=frozenset({TaskStatus.RUNNING}))
    with pytest.raises(ValueError, match="completed parents"):
        build(admission=running_parents)
    broad = harness.admission(
        parent_statuses=frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.PENDING})
    )
    with pytest.raises(ValueError, match="completed parents"):
        build(admission=broad)  # arbitrary historical states may not delegate
    with pytest.raises(ValueError, match="workspace"):
        build(admission=harness.admission(expected_workspace=BINDING))
    for bad in (0, 6):
        with pytest.raises(ValueError, match="max_rounds"):
            WorkflowDelegation(source=harness.source, admission=harness.admission(), max_rounds=bad)
    assert build().delegation is not None  # a coherent configuration is accepted


# ------------------------------------------------------------------ ownership and recovery


def test_a_controller_that_lost_the_run_stops_before_any_delegated_task_exists(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = Harness(tmp_path)
        run = await harness.new_run()

        async def steal(request: WorkflowRevisionRequest) -> DelegationCandidate:
            # Another executor takes the lease over (the first one's has expired): a newer
            # epoch is now the owner's. A direct run write is refused for a fenced run, so the
            # takeover can only happen through the lease authority.
            harness.flow.clock.advance(3600)
            taken = await harness.leases.acquire(
                key=f"runtime-run:{run.id}", owner_id="controller-2", ttl_seconds=30
            )
            assert taken is not None and taken.fencing_token == 2
            return propose_for("lead")(request)

        harness.source = Source(steal)
        with pytest.raises(RunOwnershipError):
            await harness.execute(run)
        assert harness.ran == ["lead"] and await harness.logical_ids(run) == ["lead"]
        assert harness.gate.calls == 0

    run_async(scenario)


def test_a_second_controller_cannot_enter_a_run_that_is_delegating(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(tmp_path)
        run = await harness.new_run()
        inside = asyncio.Event()
        release = asyncio.Event()

        async def hold(request: WorkflowRevisionRequest) -> DelegationCandidate:
            inside.set()
            await asyncio.wait_for(release.wait(), timeout=10)
            return propose_for("lead")(request)

        harness.source = Source(hold)
        first = asyncio.ensure_future(harness.execute(run))
        await asyncio.wait_for(inside.wait(), timeout=10)
        # The first controller is at the boundary holding the run lease; a second cannot start.
        with pytest.raises(RunOwnershipError):
            await harness.execute(run, controller=harness.controller(lease_owner_id="controller-2"))
        release.set()
        outcome = await asyncio.wait_for(first, timeout=20)
        assert outcome.status is WorkflowOutcomeStatus.COMPLETED
        assert harness.ran == ["lead", "a"]  # the child ran once, only under the lease holder

    run_async(scenario)


def test_cancellation_during_authorization_runs_nothing_and_is_recorded_safely(
    tmp_path: Path,
) -> None:
    class Gated:
        policy_id = "gated"

        def __init__(self) -> None:
            self.entered = asyncio.Event()

        async def evaluate(self, context: Any) -> Any:
            if context.action != "agent.delegate":
                return None
            self.entered.set()
            await asyncio.Event().wait()  # never released: only cancellation ends it
            return None

    async def scenario() -> None:
        gated = Gated()
        harness = Harness(tmp_path, source=Source(propose_for("lead")), rules=[gated])
        run = await harness.new_run()
        pending = asyncio.ensure_future(harness.execute(run))
        await asyncio.wait_for(gated.entered.wait(), timeout=10)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, timeout=10)
        assert harness.ran == ["lead"] and await harness.logical_ids(run) == ["lead"]
        assert await harness.status(run) is RuntimeRunStatus.CANCELLED  # no action was in flight
        assert harness.gate.calls == 0

    run_async(scenario)


def test_a_restart_after_a_blocked_authorization_authorizes_afresh(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(
            tmp_path,
            source=Source(propose_for("lead"), propose_for("lead")),
            rules=[Rule(PolicyDecisionType.REVIEW, "look first")],
        )
        run = await harness.new_run()
        blocked(await harness.execute(run), "delegation_approval_required:review_required")
        assert await harness.logical_ids(run) == ["lead"]  # nothing delegated was created
        # The host resolves the review and restarts. The old answer is not remembered.
        harness.rules = [
            DelegationPolicy(
                max_depth=3, max_parallel_jobs=3, max_jobs_per_run=6, allowed_delegates={"coder"}
            )
        ]
        outcome = await harness.execute(run)
        assert outcome.status is WorkflowOutcomeStatus.COMPLETED
        assert harness.ran.count("a") == 1 and harness.ran.count("lead") == 2  # replay re-runs
        assert await harness.logical_ids(run) == ["a", "lead"]  # one row per task, ids stable

    run_async(scenario)


def test_a_restart_after_a_delegation_replays_with_stable_identity_and_no_duplicates(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = Harness(
            tmp_path,
            source=Source(propose_for("lead"), None, propose_for("lead"), None),
            max_rounds=1,
        )
        harness.gate.status = CompletionGateStatus.BLOCKED
        run = await harness.new_run()
        blocked(await harness.execute(run), "quality_blocked")
        first_ids = sorted(task.id for task in harness.executed)
        child_id = harness.executed[1].id
        harness.gate.status = CompletionGateStatus.PASS  # the host fixes the cause and restarts
        outcome = await harness.execute(run)
        assert outcome.status is WorkflowOutcomeStatus.COMPLETED
        # At-least-once replay: the same deterministic ids, so the store holds one row each.
        assert sorted({task.id for task in harness.executed}) == first_ids
        assert child_id in {task.id for task in harness.executed}
        assert await harness.logical_ids(run) == ["a", "lead"]

    run_async(scenario)


def test_replay_never_inherits_authority_and_a_prior_child_counts_against_the_quota(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        tight = [
            DelegationPolicy(
                max_depth=3, max_parallel_jobs=3, max_jobs_per_run=1, allowed_delegates={"coder"}
            )
        ]
        harness = Harness(
            tmp_path,
            source=Source(propose_for("lead"), None, propose_for("lead"), None),
            rules=tight,
        )
        harness.gate.status = CompletionGateStatus.BLOCKED
        run = await harness.new_run()
        blocked(await harness.execute(run), "quality_blocked")
        # On restart the earlier delegated child counts toward the per-run limit of one, so the
        # same proposal is denied: fail closed, not silent. Re-admitting an identical proposal
        # idempotently needs the durable proposal identity of PR-C2 (documented limitation).
        outcome = await harness.execute(run)
        blocked(outcome, "delegation_denied:policy_denied")

        # And authority is re-read, not remembered: with the host authority removed, no delegation.
        revoked = Harness(
            tmp_path / "revoked",
            source=Source(propose_for("lead"), None, propose_for("lead"), None),
        )
        revoked.gate.status = CompletionGateStatus.BLOCKED
        run2 = await revoked.new_run()
        blocked(await revoked.execute(run2), "quality_blocked")
        out2 = await revoked.execute(
            run2, controller=revoked.controller(admission=revoked.admission(delegators={}))
        )
        blocked(out2, "delegation_denied:delegator_not_authorized")

    run_async(scenario)


def test_an_unresolved_action_of_the_run_still_blocks_before_delegation_is_consulted(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = Harness(tmp_path, source=Source(propose_for("lead")))
        run = await harness.new_run()
        await legacy_intent(
            harness.store,
            status=ActionIntentStatus.EXECUTING,
            intent_id="intent_orphan",
            run_id=run.id,
        )
        outcome = await harness.execute(run)
        blocked(outcome, "uncertain_action:intent_orphan")
        assert harness.source.requests == [] and harness.ran == []  # PR #11 guard unchanged

    run_async(scenario)

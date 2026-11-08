"""M6.3.3-B: workflow delegation admission (ADR 0015, sections 4.1 to 4.4).

`WorkflowDelegationAdmission` validates a Lead's proposal against trusted run state and the central
policy without acting on it. These tests use a real SQLite store, a real durable run and attested
tasks. They pin forged-input resistance, host-derived depth, limits, fail-closed behavior and the
absence of side effects, and they separate what is proven (a preliminary, point-in-time answer)
from what is not claimed (a reservation or a final authorization). Every wait is bounded.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from kaiwen_agent.policy import (
    AdmissionOutcome,
    DelegationAdmission,
    DelegationPolicy,
    PolicyContext,
    PolicyDecision,
    PolicyDecisionType,
    PolicyEngine,
    PolicyRegistry,
)
from kaiwen_agent.runtime import RuntimeIdentity, WorkspaceBinding
from kaiwen_agent.tasks import AgentTask, TaskRunPage, TaskStatus
from kaiwen_agent.workers import FunctionWorker
from kaiwen_agent.workflow import (
    DelegationAuthority,
    WorkflowAdmissionDecision,
    WorkflowCompiler,
    WorkflowDelegationAdmission,
    WorkflowDelegationProposal,
    WorkflowLimits,
    WorkflowTaskSpec,
)
from tests.test_agent_runtime_worker import Flow, owner
from tests.test_delegation_job_path import SECRET, Rule
from tests.test_lead_view import dump

TIMEOUT = 60
BINDING = WorkspaceBinding(workspace_id="ws-1", root="main", policy_checksum="policy-1")


def run_async(factory: Any) -> Any:
    async def bounded() -> Any:
        return await asyncio.wait_for(factory(), timeout=TIMEOUT)

    return asyncio.run(bounded())


def spec(task_id: str, worker: str = "coder", **fields: Any) -> WorkflowTaskSpec:
    return WorkflowTaskSpec(id=task_id, name=f"Task {task_id}", worker_id=worker, **fields)


def proposal(*specs: WorkflowTaskSpec) -> WorkflowDelegationProposal:
    return WorkflowDelegationProposal(tasks=specs or (spec("a"),))


def task(
    run_id: str,
    name: str,
    *,
    agent: str = "lead",
    status: TaskStatus = TaskStatus.RUNNING,
    depth: int = 0,
    capabilities: frozenset[str] = frozenset(),
    parent_id: str | None = None,
    **fields: Any,
) -> AgentTask:
    return AgentTask(
        id=f"task_{name}",
        name=name,
        agent_id=agent,
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        trace_id="trace-1",
        status=status,
        delegation_depth=depth,
        capabilities=capabilities,
        parent_id=parent_id,
        metadata={"runtime_run_id": run_id, "logical_task_id": name, "workflow_cycle": 1},
        **fields,
    )


class ByWorker:
    """A central-policy rule whose decision depends on the delegate (the policy `resource`)."""

    policy_id = "by-worker"

    def __init__(self, decisions: dict[str, PolicyDecisionType]) -> None:
        self.decisions = decisions

    async def evaluate(self, context: PolicyContext) -> PolicyDecision | None:
        if context.action != "agent.delegate" or context.resource not in self.decisions:
            return None
        return PolicyDecision(
            decision=self.decisions[context.resource],
            policy_id=self.policy_id,
            rule_id="worker",
            reason=f"decision for {context.resource}",
        )


class Env:
    """A real run, store, compiler, central policy and the admission under test."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        rules: list[Any] | None = None,
        limits: WorkflowLimits | None = None,
        allowlist: frozenset[str] = frozenset({"lead", "coder", "phantom"}),
        delegators: dict[str, DelegationAuthority] | None = None,
        **options: Any,
    ) -> None:
        self.flow = Flow(tmp_path)
        self.store = self.flow.store
        self.executed: list[str] = []

        async def work(item: AgentTask, context: Any) -> dict[str, Any]:
            self.executed.append(item.id)
            return {}

        for name in ("lead", "coder", "extra"):
            self.flow.workers.register(name, FunctionWorker(work))
        self.compiler = WorkflowCompiler(
            workers=self.flow.workers,
            worker_allowlist=allowlist,
            effective_capabilities=frozenset({"deploy", "logs.read"}),
            resource_allowlist=frozenset({"repo:main"}),
            limits=limits,
        )
        self.rules = (
            rules
            if rules is not None
            else [
                DelegationPolicy(
                    max_depth=3,
                    max_parallel_jobs=3,
                    max_jobs_per_run=6,
                    allowed_delegates={"lead", "coder"},
                )
            ]
        )
        # The host-owned delegation authority: only the Lead may delegate, up to logs.read.
        self.delegators = (
            delegators
            if delegators is not None
            else {"lead": DelegationAuthority("lead", "lead", frozenset({"logs.read"}))}
        )
        self.options = options

    def admission(self, **overrides: Any) -> WorkflowDelegationAdmission:
        settings: dict[str, Any] = {
            "admission": DelegationAdmission(
                policy_engine=PolicyEngine(registry=PolicyRegistry(self.rules))
            ),
            "runs": self.flow.runs,
            "tasks": self.store,
            "compiler": self.compiler,
            "delegators": self.delegators,
            **self.options,
        }
        settings.update(overrides)
        return WorkflowDelegationAdmission(**settings)

    async def running_run(self, binding: WorkspaceBinding | None = None) -> Any:
        run = await self.flow.new_run(binding)
        await self.flow.runs.start(run_id=run.id, identity=owner())
        return run

    async def lead(self, run: Any, **fields: Any) -> AgentTask:
        fields.setdefault("capabilities", frozenset({"logs.read"}))
        lead = task(run.id, "lead", **fields)
        await self.store.save_task(lead)
        return lead

    async def ask(
        self,
        run: Any,
        parent: AgentTask,
        plan: WorkflowDelegationProposal | None = None,
        *,
        identity: RuntimeIdentity | None = None,
        **overrides: Any,
    ) -> WorkflowAdmissionDecision:
        return await self.admission(**overrides).evaluate(
            run_id=run.id,
            identity=identity or owner(),
            parent_task_id=parent.id,
            proposal=plan or proposal(),
        )


def denied(decision: WorkflowAdmissionDecision, code: str, subject: str | None = None) -> None:
    assert decision.outcome is AdmissionOutcome.DENIED and not decision.admitted
    assert decision.code == code and decision.child_depth is None
    if subject is not None:
        assert decision.subject == subject


# ------------------------------------------------------------------ admission and its limits


def test_a_valid_proposal_is_admitted_preliminarily_with_host_derived_depth(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        env = Env(tmp_path)
        run = await env.running_run()
        lead = await env.lead(run, depth=1)
        plan = proposal(
            spec("a", capabilities=frozenset({"logs.read"})),
            spec("b", dependencies=("a",), resource_keys=("repo:main",)),
        )
        decision = await env.ask(run, lead, plan)
        assert decision.admitted and decision.code == "admitted" and decision.subject is None
        assert decision.child_depth == 2  # parent depth 1 plus one, computed by the host
        assert decision.preliminary is True and decision.reserves_nothing is True

    run_async(scenario)


def test_depth_is_read_from_the_trusted_parent_never_from_the_proposal(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = Env(tmp_path)
        run = await env.running_run()
        lead = task(
            run.id,
            "lead",
            depth=2,
            capabilities=frozenset({"logs.read"}),
            input={"delegation_depth": 0, "tenant_id": "other"},
        )
        lead.metadata["delegation_depth"] = 0  # a forged hint on the record: ignored as well
        await env.store.save_task(lead)
        forged = proposal(
            spec("a", input={"delegation_depth": 0, "capabilities": ["deploy"], "tenant_id": "x"})
        )
        decision = await env.ask(run, lead, forged)
        assert decision.admitted and decision.child_depth == 3  # parent 2 + 1
        shallow = Env(
            tmp_path / "strict",
            rules=[DelegationPolicy(max_depth=2, max_parallel_jobs=3, allowed_delegates={"coder"})],
        )
        run2 = await shallow.running_run()
        lead2 = await shallow.lead(run2, depth=2)
        too_deep = await shallow.ask(run2, lead2)
        denied(too_deep, "policy_denied", "a")
        assert "depth" in too_deep.reason  # parent depth 2 reaches the maximum of 2

    run_async(scenario)


def test_a_proposal_has_no_field_that_carries_authority() -> None:
    task_dict = {"id": "a", "name": "A", "worker_id": "coder"}
    for forged in (
        {"tasks": [task_dict], "delegation_depth": 0},
        {"tasks": [task_dict], "tenant_id": "other"},
        {"tasks": [task_dict], "permissions": ["admin"]},
        {"tasks": [task_dict], "parent_task_id": "task_x"},
        {"tasks": [task_dict], "authorized": True},
        {"tasks": [{**task_dict, "delegation_depth": 0}]},
        {"tasks": [{**task_dict, "tenant_id": "other"}]},
        {"tasks": [{**task_dict, "approved": True}]},
    ):
        with pytest.raises(ValidationError):
            WorkflowDelegationProposal.model_validate(forged)
    with pytest.raises(ValidationError):
        WorkflowDelegationProposal(tasks=())


def test_capability_escalation_and_unavailable_capabilities_are_denied(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = Env(tmp_path)
        run = await env.running_run()
        lead = await env.lead(run, capabilities=frozenset({"logs.read"}))
        beyond_parent = await env.ask(
            run, lead, proposal(spec("a"), spec("b", capabilities=frozenset({"deploy"})))
        )
        denied(beyond_parent, "capability_escalation", "b")  # deploy exists, the Lead lacks it
        unknown = await env.ask(run, lead, proposal(spec("a", capabilities=frozenset({"root"}))))
        denied(unknown, "capability_unavailable", "a")  # not available to the host at all

    run_async(scenario)


def test_unknown_unregistered_and_unauthorized_workers_are_denied(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = Env(tmp_path)
        run = await env.running_run()
        lead = await env.lead(run)
        for worker in ("ghost", "phantom", "extra"):
            # ghost: unknown; phantom: allowlisted but not registered; extra: registered only.
            result = await env.ask(run, lead, proposal(spec("a"), spec("w", worker)))
            denied(result, "worker_not_allowed", "w")
        # Allowlisted and registered, but the central policy does not allow this delegate.
        narrow = Env(
            tmp_path / "narrow",
            rules=[DelegationPolicy(max_depth=3, max_parallel_jobs=3, allowed_delegates={"lead"})],
        )
        run2 = await narrow.running_run()
        lead2 = await narrow.lead(run2)
        policy_no = await narrow.ask(run2, lead2)
        denied(policy_no, "policy_denied", "a")

    run_async(scenario)


def test_resource_keys_outside_the_compiler_allowlist_are_denied(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = Env(tmp_path)
        run = await env.running_run()
        lead = await env.lead(run)
        result = await env.ask(
            run, lead, proposal(spec("a", resource_keys=("repo:main", "repo:other")))
        )
        denied(result, "resource_not_allowed", "a")
        assert (
            await env.ask(run, lead, proposal(spec("a", resource_keys=("repo:main",))))
        ).admitted

    run_async(scenario)


def test_plan_size_and_structure_are_validated_by_the_compiler(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = Env(
            tmp_path, limits=WorkflowLimits(max_tasks_per_plan=2, max_input_bytes_per_task=200)
        )
        run = await env.running_run()
        lead = await env.lead(run)
        denied(
            await env.ask(run, lead, proposal(spec("a"), spec("b"), spec("c"))), "plan_too_large"
        )
        denied(
            await env.ask(run, lead, proposal(spec("a", dependencies=("missing",)))),
            "plan_invalid",
        )
        cyclic = proposal(spec("a", dependencies=("b",)), spec("b", dependencies=("a",)))
        denied(await env.ask(run, lead, cyclic), "plan_invalid")
        denied(
            await env.ask(run, lead, proposal(spec("a", input={"blob": "x" * 500}))),
            "plan_invalid",
        )
        assert (
            await env.ask(run, lead, proposal(spec("a"), spec("b", dependencies=("a",))))
        ).admitted

    run_async(scenario)


# ------------------------------------------------------------------ run, workspace and parent trust


def test_run_ownership_session_and_state_are_trusted_facts(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = Env(tmp_path)
        run = await env.running_run()
        lead = await env.lead(run)
        for stranger in (owner(user="user-2"), owner(tenant="tenant-2")):
            denied(await env.ask(run, lead, identity=stranger), "run_not_found")
        wrong_session = RuntimeIdentity(
            session_id="session-2", tenant_id="tenant-1", user_id="user-1", trace_id="t"
        )
        denied(await env.ask(run, lead, identity=wrong_session), "run_not_found")
        denied(
            await env.admission().evaluate(
                run_id="runtime_unknown",
                identity=owner(),
                parent_task_id=lead.id,
                proposal=proposal(),
            ),
            "run_not_found",
        )
        no_owner = RuntimeIdentity(session_id="session-1", trace_id="t")
        denied(await env.ask(run, lead, identity=no_owner), "invalid_trusted_state")
        idle = await env.flow.new_run()  # created, never started
        idle_lead = task(idle.id, "lead", status=TaskStatus.RUNNING)
        await env.store.save_task(idle_lead)
        denied(await env.ask(idle, idle_lead), "run_not_active")

    run_async(scenario)


def test_the_workspace_binding_must_match_when_one_is_expected(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = Env(tmp_path)
        bound = await env.running_run(BINDING)
        unbound = await env.running_run()
        lead = await env.lead(bound)
        assert (await env.ask(bound, lead, expected_workspace=BINDING)).admitted
        other = WorkspaceBinding(workspace_id="ws-1", root="main", policy_checksum="other")
        denied(await env.ask(bound, lead, expected_workspace=other), "workspace_mismatch")
        lead2 = await env.lead(unbound)
        denied(await env.ask(unbound, lead2, expected_workspace=BINDING), "workspace_mismatch")
        assert (await env.ask(unbound, lead2)).admitted  # nothing expected, nothing enforced

    run_async(scenario)


def test_a_forged_missing_or_unattested_parent_is_refused(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = Env(tmp_path)
        run = await env.running_run()
        sibling = await env.running_run()
        real = await env.lead(run)
        elsewhere = task(sibling.id, "elsewhere", capabilities=frozenset({"logs.read"}))
        await env.store.save_task(elsewhere)
        # A task of another run of the same owner is not a parent in this run.
        denied(await env.ask(run, elsewhere), "parent_not_found")
        denied(
            await env.admission().evaluate(
                run_id=run.id,
                identity=owner(),
                parent_task_id="task_nonexistent",
                proposal=proposal(),
            ),
            "parent_not_found",
        )
        # A task that claims this run but whose owner fields no run confirms is not attested.
        forged = task(run.id, "forged")
        forged.user_id = "user-2"
        await env.store.save_task(forged)
        denied(await env.ask(run, forged), "parent_not_found")
        assert (await env.ask(run, real)).admitted

    run_async(scenario)


def test_the_parent_must_be_in_an_allowed_state(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = Env(tmp_path)
        run = await env.running_run()
        for status in (
            TaskStatus.PAUSED,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.PENDING,
        ):
            lead = task(run.id, f"lead_{status.value}", status=status)
            await env.store.save_task(lead)
            denied(await env.ask(run, lead), "parent_not_active")
        done = task(run.id, "lead_done", status=TaskStatus.COMPLETED)
        await env.store.save_task(done)
        configured = await env.ask(run, done, parent_statuses=frozenset({TaskStatus.COMPLETED}))
        assert configured.admitted  # the allowed states are an explicit host choice

    run_async(scenario)


# ------------------------------------------------------------------ counters and limits


def delegated(run: Any, name: str, parent: AgentTask | None, **fields: Any) -> AgentTask:
    fields.setdefault("agent", "coder")
    fields.setdefault("depth", 1)
    return task(
        run.id,
        name,
        parent_id=parent.id if parent else "task_unresolvable",
        **fields,
    )


def test_per_run_and_parallel_limits_count_the_callers_delegated_tasks(tmp_path: Path) -> None:
    async def scenario() -> None:
        rules = [
            DelegationPolicy(
                max_depth=3, max_parallel_jobs=1, max_jobs_per_run=2, allowed_delegates={"coder"}
            )
        ]
        env = Env(tmp_path, rules=rules)
        run = await env.running_run()
        lead = await env.lead(run)
        assert (await env.ask(run, lead)).admitted
        await env.store.save_task(delegated(run, "d1", lead, status=TaskStatus.RUNNING))
        busy = await env.ask(run, lead)
        denied(busy, "policy_denied", "a")
        assert "parallel" in busy.reason
        # Completed delegations free the parallel slot but still count for the run.
        done = await env.store.get_task("task_d1")
        assert done is not None
        done.status = TaskStatus.COMPLETED
        await env.store.save_task(done)
        assert (await env.ask(run, lead)).admitted
        await env.store.save_task(delegated(run, "d2", lead, status=TaskStatus.COMPLETED))
        spent = await env.ask(run, lead)
        denied(spent, "policy_denied", "a")
        assert "per run" in spent.reason

    run_async(scenario)


def test_each_task_of_a_batch_counts_the_ones_before_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        rules = [
            DelegationPolicy(
                max_depth=3, max_parallel_jobs=2, max_jobs_per_run=9, allowed_delegates={"coder"}
            )
        ]
        env = Env(tmp_path, rules=rules)
        run = await env.running_run()
        lead = await env.lead(run)
        assert (await env.ask(run, lead, proposal(spec("a"), spec("b")))).admitted
        three = await env.ask(run, lead, proposal(spec("a"), spec("b"), spec("c")))
        denied(three, "policy_denied", "c")  # the third would be the third parallel task

    run_async(scenario)


def test_other_agents_delegations_do_not_count_and_unresolvable_ones_do(tmp_path: Path) -> None:
    async def scenario() -> None:
        rules = [
            DelegationPolicy(
                max_depth=3, max_parallel_jobs=1, max_jobs_per_run=9, allowed_delegates={"coder"}
            )
        ]
        env = Env(tmp_path, rules=rules)
        run = await env.running_run()
        lead = await env.lead(run)
        other_lead = task(run.id, "other_lead", agent="other")
        await env.store.save_task(other_lead)
        await env.store.save_task(delegated(run, "theirs", other_lead))
        assert (await env.ask(run, lead)).admitted  # another agent's active delegation
        await env.store.save_task(delegated(run, "orphan", None))  # parent cannot be resolved
        denied(await env.ask(run, lead), "policy_denied", "a")  # counted: fail closed

    run_async(scenario)


# ------------------------------------------------------------------ policy outcomes and failures


@pytest.mark.parametrize(
    ("decision", "code"),
    [
        (PolicyDecisionType.REQUIRE_APPROVAL, "approval_required"),
        (PolicyDecisionType.REVIEW, "review_required"),
        (PolicyDecisionType.MODIFY, "modification_required"),
    ],
)
def test_approval_like_policy_outcomes_never_admit(
    tmp_path: Path, decision: PolicyDecisionType, code: str
) -> None:
    async def scenario() -> None:
        env = Env(tmp_path, rules=[Rule(decision, reason="needs a human")])
        run = await env.running_run()
        lead = await env.lead(run)
        result = await env.ask(run, lead)
        assert result.outcome is AdmissionOutcome.APPROVAL_REQUIRED and not result.admitted
        assert result.code == code and result.subject == "a" and result.child_depth is None
        assert "REWRITTEN" not in result.model_dump_json()

    run_async(scenario)


def test_a_later_denial_outranks_an_earlier_approval_requirement(tmp_path: Path) -> None:
    async def scenario() -> None:
        rules = [ByWorker({"coder": PolicyDecisionType.REVIEW, "lead": PolicyDecisionType.DENY})]
        env = Env(tmp_path, rules=rules)
        run = await env.running_run()
        lead = await env.lead(run)
        review_only = await env.ask(run, lead, proposal(spec("a"), spec("b")))
        assert review_only.outcome is AdmissionOutcome.APPROVAL_REQUIRED
        assert review_only.subject == "a"
        mixed = await env.ask(run, lead, proposal(spec("a"), spec("b", "lead")))
        denied(mixed, "policy_denied", "b")

    run_async(scenario)


def test_policy_deny_default_and_failure_fail_closed_and_sanitized(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        caplog.set_level(logging.DEBUG)
        run_env = Env(tmp_path / "deny", rules=[Rule(PolicyDecisionType.DENY, reason="no way")])
        run = await run_env.running_run()
        lead = await run_env.lead(run)
        result = await run_env.ask(run, lead)
        denied(result, "policy_denied", "a")
        assert result.reason == "no way"
        default_env = Env(tmp_path / "default", rules=[])
        run2 = await default_env.running_run()
        denied(await default_env.ask(run2, await default_env.lead(run2)), "policy_denied", "a")
        broken = Env(tmp_path / "broken", rules=[Rule(RuntimeError(SECRET))])
        run3 = await broken.running_run()
        outcome = await broken.ask(run3, await broken.lead(run3))
        denied(outcome, "policy_unavailable", "a")
        assert SECRET not in outcome.model_dump_json()

    run_async(scenario)
    assert "SECRET" not in caplog.text and "hunter2" not in caplog.text


def test_storage_failures_and_contract_violations_fail_closed_without_leaking(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class BrokenRuns:
        async def get(self, **_: Any) -> Any:
            raise RuntimeError(SECRET)

    class BrokenTasks:
        async def list_run_tasks(self, **_: Any) -> Any:
            raise RuntimeError(SECRET)

    class Endless:
        calls = 0

        async def list_run_tasks(self, **_: Any) -> Any:
            Endless.calls += 1
            if Endless.calls > 20:
                raise AssertionError("runaway scan")
            return TaskRunPage(tasks=(), next_after_id="task_x")  # no progress, always more

    async def scenario() -> None:
        caplog.set_level(logging.DEBUG)
        env = Env(tmp_path)
        run = await env.running_run()
        lead = await env.lead(run)
        for override in ({"runs": BrokenRuns()}, {"tasks": BrokenTasks()}, {"tasks": Endless()}):
            result = await env.ask(run, lead, **override)
            denied(result, "state_unavailable")
            assert SECRET not in result.model_dump_json()
        assert Endless.calls <= 2  # the shared scan rejects a store that makes no progress
        # More run tasks than the scan bound: counters cannot be established, so fail closed.
        for index in range(5):  # six tasks in all: more than the bound of four
            await env.store.save_task(
                delegated(run, f"d{index}", lead, status=TaskStatus.COMPLETED)
            )
        denied(await env.ask(run, lead, max_scan_tasks=4), "state_incomplete")
        assert (await env.ask(run, lead, max_scan_tasks=20)).admitted

    run_async(scenario)
    assert "SECRET" not in caplog.text and "hunter2" not in caplog.text


def test_construction_requires_the_run_query_capability_and_sane_bounds(tmp_path: Path) -> None:
    env = Env(tmp_path)

    class ListOnly:
        async def list_tasks(self, **_: Any) -> list[AgentTask]:
            return []

    with pytest.raises(TypeError, match="TaskRunQuery"):
        env.admission(tasks=ListOnly())
    for bad in (0, 1_001):
        with pytest.raises(ValueError, match="max_scan_tasks"):
            env.admission(max_scan_tasks=bad)
    with pytest.raises(ValueError, match="parent_statuses"):
        env.admission(parent_statuses=frozenset())


# ------------------------------------------------------------------ no side effects, concurrency


def test_admission_creates_no_tasks_jobs_approvals_or_executions(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = Env(tmp_path)
        run = await env.running_run(BINDING)
        lead = await env.lead(run, depth=1)
        await env.store.save_task(delegated(run, "d1", lead, status=TaskStatus.COMPLETED))
        before = dump(env.store.path)
        page_before = await env.store.list_run_tasks(
            tenant_id="tenant-1", user_id="user-1", runtime_run_id=run.id
        )
        plan = proposal(spec("a"), spec("b", dependencies=("a",)))
        for _ in range(3):
            assert (await env.ask(run, lead, plan, expected_workspace=BINDING)).admitted
        denied(await env.ask(run, lead, proposal(spec("a", "ghost"))), "worker_not_allowed")
        after = dump(env.store.path)
        assert after == before  # no task, run, intent, approval or checkpoint row changed
        page_after = await env.store.list_run_tasks(
            tenant_id="tenant-1", user_id="user-1", runtime_run_id=run.id
        )
        assert page_after == page_before
        assert env.executed == []  # no worker, so no scheduler, ever ran

    run_async(scenario)


def test_concurrent_admissions_are_point_in_time_and_reserve_nothing(tmp_path: Path) -> None:
    """Pins the documented limit (ADR 0015, 4.2): preliminary answers are not reservations.

    With one parallel slot free, every concurrent evaluation is admitted. A later evaluation sees
    the new state and is denied, so nothing is cached. An authoritative final admission would have
    to re-check where work becomes executable (PR-C1); this test does not claim that it does.
    """

    async def scenario() -> None:
        rules = [
            DelegationPolicy(
                max_depth=3, max_parallel_jobs=1, max_jobs_per_run=9, allowed_delegates={"coder"}
            )
        ]
        env = Env(tmp_path, rules=rules)
        run = await env.running_run()
        lead = await env.lead(run)
        results = await asyncio.gather(*(env.ask(run, lead) for _ in range(6)))
        assert all(item.admitted and item.reserves_nothing for item in results)
        await env.store.save_task(delegated(run, "d1", lead, status=TaskStatus.RUNNING))
        denied(await env.ask(run, lead), "policy_denied", "a")  # fresh state, fresh answer

    run_async(scenario)


def test_cancellation_propagates_and_leaves_no_trace(tmp_path: Path) -> None:
    async def scenario() -> None:
        gate = asyncio.Event()

        class Slow:
            policy_id = "slow"

            async def evaluate(self, context: PolicyContext) -> PolicyDecision | None:
                if context.action != "agent.delegate":
                    return None
                await gate.wait()
                return None

        env = Env(tmp_path, rules=[Slow()])
        run = await env.running_run()
        lead = await env.lead(run)
        before = dump(env.store.path)
        pending = asyncio.ensure_future(env.ask(run, lead))
        await asyncio.sleep(0.2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, timeout=5)
        assert dump(env.store.path) == before

    run_async(scenario)

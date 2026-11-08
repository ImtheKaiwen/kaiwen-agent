"""PR #13 audit R1: who may delegate, and up to which capabilities (ADR 0015).

A parent task's recorded capabilities are what a plan asked for. The compiler checks them only
against the host-wide set, and an `AgentRuntimeWorker` checks them against its own
`allowed_capabilities` only when it executes the task. They are therefore not proof that the
parent's worker may delegate, or may hand those capabilities down. The delegation ceiling and the
permission to delegate come from the host-owned `DelegationAuthority` mapping. These tests use the
real `WorkflowCompiler` and real `AgentRuntimeWorker` objects for the reproduced defect, and pin
that the delegate allowlist (who may be delegated to) is distinct from the caller's permission to
delegate. Every wait is bounded.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from kaiwen_agent.policy import (
    AdmissionOutcome,
    DelegationAdmission,
    DelegationPolicy,
    PolicyDecisionType,
    PolicyEngine,
    PolicyRegistry,
)
from kaiwen_agent.tasks import TaskStatus
from kaiwen_agent.workers import FunctionWorker
from kaiwen_agent.workflow import (
    DelegationAuthority,
    WorkflowAdmissionDecision,
    WorkflowCompiler,
    WorkflowDelegationAdmission,
    WorkflowPlan,
    WorkflowTaskSpec,
)
from tests.test_agent_runtime_worker import Flow, Provider, owner
from tests.test_delegation_job_path import Rule
from tests.test_workflow_delegation_admission import (
    Env,
    delegated,
    denied,
    proposal,
    run_async,
    spec,
    task,
)

HOST_WIDE = frozenset({"logs.read", "deploy"})


def authority(worker: str = "lead", role: str | None = None, *caps: str) -> DelegationAuthority:
    return DelegationAuthority(worker, role or worker, frozenset(caps))


def limits(**fields: Any) -> DelegationPolicy:
    options: dict[str, Any] = {
        "max_depth": 3,
        "max_parallel_jobs": 3,
        "max_jobs_per_run": 6,
        "allowed_delegates": {"coder", "lead"},
    }
    options.update(fields)
    return DelegationPolicy(**options)


class Real:
    """Real `AgentRuntimeWorker`s behind the real compiler, so the worker boundary is exercised."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        coder_caps: frozenset[str] = HOST_WIDE,
        **delegators: DelegationAuthority,
    ) -> None:
        self.flow = Flow(tmp_path)
        self.lead_worker = self.flow.worker(
            "lead",
            self.flow.runtime(Provider(text="done")),
            allowed_capabilities=frozenset({"logs.read"}),
        )
        self.flow.worker(
            "coder", self.flow.runtime(Provider(text="done")), allowed_capabilities=coder_caps
        )
        self.compiler = WorkflowCompiler(
            workers=self.flow.workers,
            worker_allowlist=frozenset({"lead", "coder"}),
            effective_capabilities=HOST_WIDE,
            resource_allowlist=frozenset({"repo:main"}),
        )
        self.delegators = delegators
        self.rules: list[Any] = [limits(allowed_delegates={"coder"})]

    def admission(self) -> WorkflowDelegationAdmission:
        return WorkflowDelegationAdmission(
            admission=DelegationAdmission(
                policy_engine=PolicyEngine(registry=PolicyRegistry(self.rules))
            ),
            runs=self.flow.runs,
            tasks=self.flow.store,
            compiler=self.compiler,
            delegators=self.delegators,
        )

    async def running_run(self) -> Any:
        run = await self.flow.new_run()
        await self.flow.runs.start(run_id=run.id, identity=owner())
        return run


# ------------------------------------------------------------------ the reproduced defect


def test_a_plan_authored_label_on_the_parent_does_not_confer_delegation_authority(
    tmp_path: Path,
) -> None:
    """Reproduces the audit finding with the real compiler and the real worker boundary.

    Host-wide capabilities include `deploy`. The Lead is host-configured to carry only
    `logs.read`. The real compiler still accepts a Lead task that claims `deploy`; the real worker
    rejects that claim only when it executes the task. The central policy allows the Coder. The
    adapter used to admit a Coder child asking for `deploy` solely because the parent's recorded
    label claimed it.
    """

    async def scenario() -> None:
        env = Real(tmp_path, lead=authority("lead", "lead", "logs.read"))
        run = await env.running_run()
        lead_spec = WorkflowTaskSpec(
            id="lead", name="Lead", worker_id="lead", capabilities=frozenset({"deploy"})
        )

        # 1. The real compiler boundary accepts the claim (it only checks the host-wide set).
        graph = env.compiler.compile(
            WorkflowPlan(objective="plan", tasks=(lead_spec,)), run=run, identity=owner(), cycle=1
        )
        (lead_task,) = graph.tasks()
        assert lead_task.capabilities == frozenset({"deploy"})
        assert lead_task.agent_id == "lead" and lead_task.metadata["runtime_run_id"] == run.id

        # 2. The real worker boundary refuses the same claim, but only at execution time.
        with pytest.raises(PermissionError, match="not allowed"):
            await env.lead_worker.execute(lead_task, None)  # type: ignore[arg-type]

        # 3. The Lead is running with that recorded claim; the Coder child asks for deploy.
        lead_task.status = TaskStatus.RUNNING
        await env.flow.store.save_task(lead_task)
        child = proposal(spec("child", "coder", capabilities=frozenset({"deploy"})))
        decision = await env.admission().evaluate(
            run_id=run.id, identity=owner(), parent_task_id=lead_task.id, proposal=child
        )
        denied(decision, "capability_escalation", "child")  # the host ceiling excludes deploy

    run_async(scenario)


def test_a_properly_authorized_ceiling_admits_and_the_policy_sees_that_ceiling(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        recorder = Rule(PolicyDecisionType.ALLOW, policy_id="recorder")
        env = Env(
            tmp_path,
            rules=[limits(allowed_delegates={"coder"}), recorder],
            delegators={"lead": authority("lead", "lead", "logs.read", "deploy")},
        )
        run = await env.running_run()
        lead = await env.lead(run, capabilities=frozenset({"logs.read"}))  # recorded, not authority
        plan = proposal(spec("a", capabilities=frozenset({"deploy"})))
        decision = await env.ask(run, lead, plan)
        assert decision.admitted and decision.child_depth == 1
        (seen,) = recorder.contexts
        # The policy sees the validated host ceiling and the host-assigned role, not plan data.
        assert seen.capabilities == frozenset({"logs.read", "deploy"})
        assert seen.metadata["delegator_role"] == "lead"
        assert seen.metadata["requested_capabilities"] == ["deploy"]
        assert (seen.agent_id, seen.resource) == ("lead", "coder")

    run_async(scenario)


def test_a_parent_record_that_exceeds_the_ceiling_neither_widens_nor_poisons(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        recorder = Rule(PolicyDecisionType.ALLOW, policy_id="recorder")
        env = Env(
            tmp_path,
            rules=[limits(allowed_delegates={"coder"}), recorder],
            delegators={"lead": authority("lead", "lead", "logs.read")},
        )
        run = await env.running_run()
        lead = await env.lead(run, capabilities=frozenset({"deploy", "logs.read"}))
        within = await env.ask(
            run, lead, proposal(spec("a", capabilities=frozenset({"logs.read"})))
        )
        assert within.admitted  # a claim beyond the ceiling on the record does not block
        assert recorder.contexts[-1].capabilities == frozenset({"logs.read"})  # not deploy
        beyond = await env.ask(run, lead, proposal(spec("a", capabilities=frozenset({"deploy"}))))
        denied(beyond, "capability_escalation", "a")

    run_async(scenario)


# ------------------------------------------------------------------ who may delegate


def test_the_delegate_allowlist_is_not_the_callers_permission_to_delegate(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = Env(tmp_path)  # the policy allows lead and coder as delegates; only lead may call
        run = await env.running_run()
        lead = await env.lead(run)
        coder = task(run.id, "coder_task", agent="coder", depth=1)
        await env.store.save_task(coder)
        assert (await env.ask(run, lead)).admitted
        result = await env.ask(run, coder)  # the target worker is allowed, the caller is not
        denied(result, "delegator_not_authorized")
        assert result.subject is None

    run_async(scenario)


def test_without_any_host_authority_nobody_delegates(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = Env(tmp_path, delegators={})
        run = await env.running_run()
        lead = await env.lead(run)
        denied(await env.ask(run, lead), "delegator_not_authorized")

    run_async(scenario)


def test_authorization_is_by_configured_worker_not_by_the_literal_name_lead(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        env = Env(tmp_path, delegators={"coder": authority("coder", "builder", "logs.read")})
        run = await env.running_run()
        lead = await env.lead(run)
        coder = task(
            run.id, "coder_task", agent="coder", depth=1, capabilities=frozenset({"logs.read"})
        )
        await env.store.save_task(coder)
        denied(await env.ask(run, lead), "delegator_not_authorized")  # not authorized here
        decision = await env.ask(run, coder, proposal(spec("a", "lead")))
        assert decision.admitted and decision.child_depth == 2

    run_async(scenario)


def test_explicitly_authorized_nested_delegation_is_supported_and_still_limited(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        both = {
            "lead": authority("lead", "lead", "logs.read"),
            "coder": authority("coder", "builder", "logs.read"),
        }
        env = Env(tmp_path, delegators=both, rules=[limits(max_depth=2)])
        run = await env.running_run()
        nested = task(
            run.id, "nested", agent="coder", depth=1, capabilities=frozenset({"logs.read"})
        )
        await env.store.save_task(nested)
        ok = await env.ask(run, nested, proposal(spec("a", "coder")))
        assert ok.admitted and ok.child_depth == 2
        deepest = task(
            run.id, "deepest", agent="coder", depth=2, capabilities=frozenset({"logs.read"})
        )
        await env.store.save_task(deepest)
        too_deep = await env.ask(run, deepest, proposal(spec("a", "coder")))
        denied(too_deep, "policy_denied", "a")
        assert "depth" in too_deep.reason  # the existing depth limit still applies

    run_async(scenario)


def test_a_delegated_parents_labels_narrow_its_children_but_a_root_parents_do_not(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        ceiling = ("logs.read", "deploy")
        env = Env(
            tmp_path,
            delegators={
                "lead": authority("lead", "lead", *ceiling),
                "coder": authority("coder", "builder", *ceiling),
            },
        )
        run = await env.running_run()
        want_deploy = proposal(spec("a", capabilities=frozenset({"deploy"})))
        root = await env.lead(run, capabilities=frozenset({"logs.read"}))
        # A root parent's recorded labels are plan-authored: the host ceiling governs.
        assert (await env.ask(run, root, want_deploy)).admitted
        child = task(run.id, "child", agent="coder", depth=1, capabilities=frozenset({"logs.read"}))
        await env.store.save_task(child)
        # A delegated parent's labels were set by an earlier admission: they narrow, as on the
        # job path, so a task granted only logs.read cannot hand deploy down.
        narrowed = await env.ask(run, child, want_deploy)
        denied(narrowed, "capability_exceeds_parent", "a")

    run_async(scenario)


# ------------------------------------------------------------------ role and identity binding


def test_the_authority_is_bound_to_the_registered_workers_role(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = Real(tmp_path, lead=authority("lead", "reviewer", "logs.read"))  # worker role: lead
        run = await env.running_run()
        parent = task(run.id, "lead", agent="lead", capabilities=frozenset({"logs.read"}))
        await env.flow.store.save_task(parent)
        plan = proposal(spec("a", "coder"))
        mismatch = await env.admission().evaluate(
            run_id=run.id, identity=owner(), parent_task_id=parent.id, proposal=plan
        )
        denied(mismatch, "delegator_role_mismatch")
        env.delegators = {"lead": authority("lead", "lead", "logs.read")}
        matching = await env.admission().evaluate(
            run_id=run.id, identity=owner(), parent_task_id=parent.id, proposal=plan
        )
        assert matching.admitted

    run_async(scenario)


def test_a_worker_without_a_role_is_bound_by_its_registered_id_only(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = Env(tmp_path, delegators={"lead": authority("lead", "any-label", "logs.read")})
        run = await env.running_run()
        lead = await env.lead(run)
        assert (await env.ask(run, lead)).admitted  # a FunctionWorker declares no role to check

    run_async(scenario)


def test_forged_run_owner_and_session_bindings_never_reach_the_authority(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = Env(tmp_path)
        run = await env.running_run()
        sibling = await env.running_run()
        await env.lead(run)
        # A record named like the authorized worker but in another run of the same owner.
        elsewhere = task(sibling.id, "lead_elsewhere", agent="lead")
        await env.store.save_task(elsewhere)
        denied(await env.ask(run, elsewhere), "parent_not_found")
        # A record that claims this run for another owner is not attested and is not found.
        forged = task(run.id, "lead_forged", agent="lead")
        forged.user_id = "user-2"
        await env.store.save_task(forged)
        denied(await env.ask(run, forged), "parent_not_found")
        # The same task under another owner's identity: the run itself is not found.
        parent = task(run.id, "lead_real", agent="lead")
        await env.store.save_task(parent)
        denied(await env.ask(run, parent, identity=owner(user="user-2")), "run_not_found")
        denied(await env.ask(run, parent, identity=owner(tenant="tenant-2")), "run_not_found")

    run_async(scenario)


# ------------------------------------------------------------------ construction is fail-closed


def test_the_authority_source_is_required_validated_and_copied(tmp_path: Path) -> None:
    env = Env(tmp_path)

    async def build(**overrides: Any) -> WorkflowDelegationAdmission:
        return env.admission(**overrides)

    with pytest.raises(TypeError, match="delegators"):
        WorkflowDelegationAdmission(  # type: ignore[call-arg]
            admission=DelegationAdmission(policy_engine=PolicyEngine(registry=PolicyRegistry([]))),
            runs=env.flow.runs,
            tasks=env.store,
            compiler=env.compiler,
        )
    with pytest.raises(ValueError, match="keyed by its own worker id"):
        env.admission(delegators={"lead": authority("coder", "coder")})
    with pytest.raises(ValueError, match="allowlisted, registered"):
        env.admission(delegators={"ghost": authority("ghost")})  # unknown
    with pytest.raises(ValueError, match="allowlisted, registered"):
        env.admission(delegators={"phantom": authority("phantom")})  # allowlisted, unregistered
    with pytest.raises(ValueError, match="allowlisted, registered"):
        env.admission(delegators={"extra": authority("extra")})  # registered, not allowlisted
    with pytest.raises(ValueError, match="keyed by its own worker id"):
        env.admission(delegators={"lead": {"worker_id": "lead"}})  # not an authority at all

    mutable = {"lead": authority("lead", "lead", "logs.read")}
    adapter = env.admission(delegators=mutable)
    mutable.clear()  # changing the host's dict afterwards grants and removes nothing
    assert "lead" in adapter.delegators
    with pytest.raises(TypeError):
        adapter.delegators["coder"] = authority("coder")  # type: ignore[index]
    assert asyncio.run(build(delegators={})).delegators == {}


@pytest.mark.parametrize(
    "bad",
    [
        {"worker_id": ""},
        {"role": ""},
        {"role": "   "},
        {"capabilities": {"logs.read"}},
        {"capabilities": frozenset({""})},
        {"capabilities": frozenset({1})},
    ],
    ids=lambda bad: next(iter(bad)) + "=" + repr(next(iter(bad.values()))),
)
def test_a_delegation_authority_must_be_well_formed(bad: dict[str, Any]) -> None:
    fields: dict[str, Any] = {"worker_id": "lead", "role": "lead", "capabilities": frozenset()}
    fields.update(bad)
    with pytest.raises(ValueError):
        DelegationAuthority(**fields)


def test_authorization_failures_are_preliminary_and_leave_no_trace(tmp_path: Path) -> None:
    async def scenario() -> None:
        from tests.test_lead_view import dump

        env = Env(tmp_path, delegators={})
        run = await env.running_run()
        lead = await env.lead(run)
        await env.store.save_task(delegated(run, "d1", lead, status=TaskStatus.COMPLETED))
        before = dump(env.store.path)
        decision: WorkflowAdmissionDecision = await env.ask(run, lead)
        denied(decision, "delegator_not_authorized")
        assert decision.preliminary is True and decision.reserves_nothing is True
        assert decision.outcome is AdmissionOutcome.DENIED
        assert dump(env.store.path) == before and env.executed == []

    run_async(scenario)


def test_function_workers_remain_unaffected_by_the_authority_source(tmp_path: Path) -> None:
    """The worker registry is only read; a registered worker is never invoked by admission."""

    async def scenario() -> None:
        env = Env(tmp_path)
        assert isinstance(env.flow.workers.get("lead"), FunctionWorker)
        run = await env.running_run()
        lead = await env.lead(run)
        assert (await env.ask(run, lead)).admitted
        assert env.executed == []

    run_async(scenario)


def test_a_child_worker_that_would_refuse_the_capabilities_is_not_admitted(tmp_path: Path) -> None:
    """A real `AgentRuntimeWorker` checks `allowed_capabilities` only when it executes (PR #13 R1).

    Admission now checks the same declared contract, so it never admits a child that is doomed to
    fail at execution, and it still allows what the child worker declares it accepts.
    """

    async def scenario() -> None:
        env = Real(
            tmp_path,
            coder_caps=frozenset({"logs.read"}),
            lead=authority("lead", "lead", "logs.read", "deploy"),
        )
        run = await env.running_run()
        parent = task(run.id, "lead", agent="lead", capabilities=frozenset({"logs.read"}))
        await env.flow.store.save_task(parent)

        async def ask(*caps: str) -> WorkflowAdmissionDecision:
            return await env.admission().evaluate(
                run_id=run.id,
                identity=owner(),
                parent_task_id=parent.id,
                proposal=proposal(spec("a", "coder", capabilities=frozenset(caps))),
            )

        # Within the host ceiling and the host-wide set, but beyond what this worker accepts.
        denied(await ask("deploy"), "worker_capability_mismatch", "a")
        assert (await ask("logs.read")).admitted

    run_async(scenario)

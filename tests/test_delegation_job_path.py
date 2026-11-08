"""M6.3.3-B: the existing job delegation path under the shared admission contract (ADR 0015).

`SubagentDelegator` keeps its public behavior. These tests pin that behavior where the shared
admission core now sits underneath it, and cover the audited gaps: a policy that raises must fail
closed with a sanitized outcome, approval-like decisions never authorize, and resource keys are
unrestricted by default (a documented gap) but can be allowlisted. Two limits are pinned as honest
characterizations, not guarantees: admission is point-in-time, and the per-instance lock gives no
cross-instance atomicity. Every wait is bounded.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from kaiwen_agent.jobs import (
    DelegationApprovalRequiredError,
    DelegationDeniedError,
    DelegationRequest,
    JobManager,
    SchedulerTaskRuntimeAdapter,
    SubagentDelegator,
)
from kaiwen_agent.policy import (
    DelegationPolicy,
    PolicyContext,
    PolicyDecision,
    PolicyDecisionType,
    PolicyEngine,
    PolicyRegistry,
)
from kaiwen_agent.tasks import AgentTask
from kaiwen_agent.workers import FunctionWorker, TaskExecutionContext, WorkerRegistry
from tests.test_delegation import immediate_worker, job_context

SECRET = "sk-live-SECRET-TOKEN password=hunter2"
TIMEOUT = 30


def run_async(factory: Any) -> Any:
    async def bounded() -> Any:
        return await asyncio.wait_for(factory(), timeout=TIMEOUT)

    return asyncio.run(bounded())


class Rule:
    """A custom central-policy rule for `agent.delegate`."""

    def __init__(
        self,
        decision: PolicyDecisionType | BaseException,
        reason: str = "because",
        *,
        policy_id: str = "test-rule",
    ) -> None:
        self.policy_id = policy_id
        self.decision = decision
        self.reason = reason
        self.contexts: list[PolicyContext] = []

    async def evaluate(self, context: PolicyContext) -> PolicyDecision | None:
        if context.action != "agent.delegate":
            return None
        self.contexts.append(context)
        if isinstance(self.decision, BaseException):
            raise self.decision
        return PolicyDecision(
            decision=self.decision,
            policy_id=self.policy_id,
            rule_id="test",
            reason=self.reason,
            modifications={"objective": "REWRITTEN"}
            if self.decision == PolicyDecisionType.MODIFY
            else None,
        )


def build(
    rules: list[Any], *, worker: Any = None, **delegator: Any
) -> tuple[Any, SubagentDelegator]:
    workers = WorkerRegistry()
    workers.register("research-agent", worker or FunctionWorker(immediate_worker))
    jobs = JobManager(
        runtime=SchedulerTaskRuntimeAdapter(workers=workers),
        allowed_worker_ids={"research-agent"},
    )
    engine = PolicyEngine(registry=PolicyRegistry(rules))
    return jobs, SubagentDelegator(jobs=jobs, policy_engine=engine, **delegator)


def allow_all() -> DelegationPolicy:
    return DelegationPolicy(
        max_depth=3, max_parallel_jobs=5, max_jobs_per_run=10, allowed_delegates={"research-agent"}
    )


def request(**fields: Any) -> DelegationRequest:
    return DelegationRequest(worker_agent_id="research-agent", objective="Inspect", **fields)


# ------------------------------------------------------------------ fail-closed policy outcomes


def test_a_policy_that_raises_fails_closed_with_a_sanitized_denial(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        caplog.set_level(logging.DEBUG)
        jobs, delegator = build([Rule(RuntimeError(SECRET))])
        with pytest.raises(DelegationDeniedError) as denied:
            await delegator.delegate(job_context(), request())
        assert SECRET not in str(denied.value) and "hunter2" not in repr(denied.value)
        assert denied.value.__cause__ is None and denied.value.__context__ is None
        assert await jobs.list(job_context()) == []  # nothing was submitted

    run_async(scenario)
    assert "SECRET" not in caplog.text and "hunter2" not in caplog.text


def test_cancellation_is_never_swallowed_by_policy_failure_handling() -> None:
    async def scenario() -> None:
        jobs, delegator = build([Rule(asyncio.CancelledError())])
        with pytest.raises(asyncio.CancelledError):
            await delegator.delegate(job_context(), request())
        assert await jobs.list(job_context()) == []

    run_async(scenario)


@pytest.mark.parametrize(
    "decision",
    [
        PolicyDecisionType.REQUIRE_APPROVAL,
        PolicyDecisionType.REVIEW,
        PolicyDecisionType.MODIFY,
    ],
)
def test_approval_like_decisions_never_authorize_and_never_modify(
    decision: PolicyDecisionType,
) -> None:
    async def scenario() -> None:
        jobs, delegator = build([Rule(decision, reason="needs a human")])
        with pytest.raises(DelegationApprovalRequiredError, match="needs a human"):
            await delegator.delegate(job_context(), request())
        assert await jobs.list(job_context()) == []  # not submitted, not rewritten

    run_async(scenario)


def test_deny_and_the_default_decision_fail_closed() -> None:
    async def scenario() -> None:
        jobs, delegator = build([Rule(PolicyDecisionType.DENY, reason="no way")])
        with pytest.raises(DelegationDeniedError, match="no way"):
            await delegator.delegate(job_context(), request())
        jobs, delegator = build([])  # no rule applies: the engine's default is DENY
        with pytest.raises(DelegationDeniedError):
            await delegator.delegate(job_context(), request())
        assert await jobs.list(job_context()) == []

    run_async(scenario)


def test_the_most_restrictive_of_several_decisions_wins() -> None:
    async def scenario() -> None:
        jobs, delegator = build([allow_all(), Rule(PolicyDecisionType.REVIEW, reason="look first")])
        with pytest.raises(DelegationApprovalRequiredError, match="look first"):
            await delegator.delegate(job_context(), request())
        assert await jobs.list(job_context()) == []

    run_async(scenario)


def test_the_policy_context_is_built_from_host_facts_only() -> None:
    async def scenario() -> None:
        rule = Rule(PolicyDecisionType.ALLOW)
        jobs, delegator = build([rule])
        await delegator.delegate(
            job_context(), request(requested_capabilities={"logs.read"}, priority=5)
        )
        (seen,) = rule.contexts
        assert (seen.tenant_id, seen.user_id, seen.agent_id) == (
            "tenant-1",
            "user-1",
            "admin-agent",
        )
        assert seen.action == "agent.delegate" and seen.resource == "research-agent"
        assert seen.metadata == {
            "delegation_depth": 0,
            "parallel_jobs": 0,
            "jobs_in_run": 0,
            "requested_capabilities": ["logs.read"],
            "parent_job_id": None,
        }
        assert seen.capabilities == job_context().capabilities
        await jobs.wait(job_context(), (await jobs.list(job_context()))[0].id)

    run_async(scenario)


# ------------------------------------------------------------------ ownership and isolation


def test_a_parent_job_of_another_owner_cannot_be_used() -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def hold(task: AgentTask, execution: TaskExecutionContext) -> dict[str, object]:
            del task, execution
            await release.wait()
            return {}

        jobs, delegator = build([allow_all()], worker=FunctionWorker(hold))
        parent = await delegator.delegate(job_context(), request())
        stranger = job_context().model_copy(update={"user_id": "user-2"})
        with pytest.raises(KeyError):  # not found in the stranger's trusted owner scope
            await delegator.delegate(stranger, request(parent_job_id=parent.id))
        assert await jobs.list(stranger) == []
        release.set()
        await jobs.wait(job_context(), parent.id)

    run_async(scenario)


def test_only_the_assigned_worker_may_delegate_a_child_and_depth_must_match() -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def hold(task: AgentTask, execution: TaskExecutionContext) -> dict[str, object]:
            del task, execution
            await release.wait()
            return {}

        jobs, delegator = build([allow_all()], worker=FunctionWorker(hold))
        parent = await delegator.delegate(job_context(), request())
        impostor = job_context(agent_id="someone-else", delegation_depth=1)
        with pytest.raises(DelegationDeniedError, match="assigned worker"):
            await delegator.delegate(impostor, request(parent_job_id=parent.id))
        forged_depth = job_context(agent_id="research-agent", delegation_depth=0)
        with pytest.raises(DelegationDeniedError, match="depth does not match"):
            await delegator.delegate(forged_depth, request(parent_job_id=parent.id))
        assert len(await jobs.list(job_context())) == 1  # only the parent exists
        release.set()
        await jobs.wait(job_context(), parent.id)

    run_async(scenario)


# ------------------------------------------------------------------ resource keys (audited gap)


def test_resource_keys_are_unrestricted_by_default_which_is_a_documented_gap() -> None:
    """Characterization: without an allowlist any model-authored key is accepted (ADR 0015)."""

    async def scenario() -> None:
        jobs, delegator = build([allow_all()])
        reference = await delegator.delegate(
            job_context(), request(resource_keys=["repo:main", "anything-at-all"])
        )
        job = await jobs.wait(job_context(), reference.id)
        assert job.resource_keys == ["repo:main", "anything-at-all"]

    run_async(scenario)


def test_a_configured_resource_allowlist_denies_before_any_job_exists() -> None:
    async def scenario() -> None:
        jobs, delegator = build([allow_all()], resource_allowlist=frozenset({"repo:main"}))
        with pytest.raises(DelegationDeniedError, match="resource"):
            await delegator.delegate(job_context(), request(resource_keys=["repo:other"]))
        with pytest.raises(DelegationDeniedError, match="resource"):
            await delegator.delegate(job_context(), request(resource_keys=["repo:main", "x"]))
        assert await jobs.list(job_context()) == []
        reference = await delegator.delegate(job_context(), request(resource_keys=["repo:main"]))
        await jobs.wait(job_context(), reference.id)
        none_needed = await delegator.delegate(job_context(), request())
        await jobs.wait(job_context(), none_needed.id)

    run_async(scenario)


def test_an_empty_resource_allowlist_denies_every_key() -> None:
    async def scenario() -> None:
        jobs, delegator = build([allow_all()], resource_allowlist=frozenset())
        with pytest.raises(DelegationDeniedError, match="resource"):
            await delegator.delegate(job_context(), request(resource_keys=["repo:main"]))
        assert await jobs.list(job_context()) == []

    run_async(scenario)


# ------------------------------------------------------------------ honest concurrency limits


def test_admission_is_point_in_time_and_the_per_instance_lock_is_not_cross_instance() -> None:
    """Pins a documented limit (ADR 0015 section 4.2), not a guarantee.

    Two delegators over one `JobManager` each hold their own lock. A policy that waits until
    both have counted lets both observe zero active jobs, so both are allowed and the parallel
    limit of 1 is exceeded. Within one delegator the same race is prevented
    (`test_concurrent_submissions_cannot_race_past_parallel_limit`).
    """

    class Barrier:
        policy_id = "barrier"

        def __init__(self) -> None:
            self.arrived = 0
            self.both = asyncio.Event()

        async def evaluate(self, context: PolicyContext) -> PolicyDecision | None:
            if context.action != "agent.delegate":
                return None
            self.arrived += 1
            if self.arrived == 2:
                self.both.set()
            await asyncio.wait_for(self.both.wait(), timeout=5)
            return PolicyDecision(
                decision=PolicyDecisionType.ALLOW,
                policy_id=self.policy_id,
                rule_id="barrier",
                reason="counted",
            )

    async def scenario() -> None:
        release = asyncio.Event()

        async def hold(task: AgentTask, execution: TaskExecutionContext) -> dict[str, object]:
            del task, execution
            await release.wait()
            return {}

        workers = WorkerRegistry()
        workers.register("research-agent", FunctionWorker(hold))
        jobs = JobManager(
            runtime=SchedulerTaskRuntimeAdapter(workers=workers),
            allowed_worker_ids={"research-agent"},
        )
        barrier = Barrier()
        engine = PolicyEngine(
            registry=PolicyRegistry(
                [
                    DelegationPolicy(
                        max_depth=3,
                        max_parallel_jobs=1,
                        allowed_delegates={"research-agent"},
                    ),
                    barrier,
                ]
            )
        )
        first = SubagentDelegator(jobs=jobs, policy_engine=engine)
        second = SubagentDelegator(jobs=jobs, policy_engine=engine)
        results = await asyncio.gather(
            first.delegate(job_context(), request()),
            second.delegate(job_context(), request()),
            return_exceptions=True,
        )
        accepted = [item for item in results if not isinstance(item, Exception)]
        assert len(accepted) == 2  # the limit of 1 was exceeded: no cross-instance atomicity
        release.set()
        for reference in accepted:
            await jobs.wait(job_context(), reference.id)

    run_async(scenario)

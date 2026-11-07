"""Deterministic multi-agent integration through the real team stack (M5).

These tests run `TeamRuntime`, `SubagentDelegator`, `JobManager`, and the task scheduler
together with fake workers: delegation chains, bounded handoffs, failure propagation,
cancellation cascades, capability inheritance, and resource serialization. They do not
introduce a second team engine.
"""

from __future__ import annotations

import asyncio
from typing import Any

from kaiwen_agent.agents import AgentDefinitionRegistry
from kaiwen_agent.jobs import (
    JobContext,
    JobManager,
    JobStatus,
    SchedulerTaskRuntimeAdapter,
    SubagentDelegator,
)
from kaiwen_agent.policy import DelegationPolicy, PolicyEngine, PolicyRegistry
from kaiwen_agent.teams import TeamDelegationRequest, TeamRuntime
from kaiwen_agent.workers import FunctionWorker, WorkerRegistry
from tests.test_teams import _agent, _context, _definition

SECRET = "sk-secretvalue1234567890"


def _build(coder: Any, reviewer: Any, *, max_depth: int = 3, max_parallel: int = 4) -> TeamRuntime:
    workers = WorkerRegistry()
    workers.register("coder", FunctionWorker(coder))
    workers.register("reviewer", FunctionWorker(reviewer))
    jobs = JobManager(
        runtime=SchedulerTaskRuntimeAdapter(workers=workers),
        allowed_worker_ids={"coder", "reviewer"},
    )
    policy = PolicyEngine(
        registry=PolicyRegistry(
            [
                DelegationPolicy(
                    max_depth=max_depth,
                    max_parallel_jobs=max_parallel,
                    max_jobs_per_run=20,
                    allowed_delegates={"coder", "reviewer"},
                )
            ]
        )
    )
    agents = AgentDefinitionRegistry(
        [
            _agent("lead", permissions=frozenset({"repo.read", "repo.write"})),
            _agent("coder", permissions=frozenset({"repo.read", "repo.write"})),
            _agent("reviewer", permissions=frozenset({"repo.read"})),
        ]
    )
    return TeamRuntime(
        definition=_definition(),
        agents=agents,
        delegator=SubagentDelegator(jobs=jobs, policy_engine=policy),
        jobs=jobs,
    )


def _worker_context(task: Any, *, agent_id: str) -> JobContext:
    """The identity a running worker would use to delegate, from its own task grant."""

    return JobContext(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        conversation_id="conversation-1",
        agent_id=agent_id,
        run_id="run-1",
        trace_id="trace-1",
        capabilities=task.capabilities,
        delegation_depth=task.delegation_depth,
    )


async def _noop(task: Any, context: Any) -> dict[str, Any]:
    return {}


def test_lead_coder_reviewer_chain_completes_with_bounded_handoffs() -> None:
    holder: dict[str, TeamRuntime] = {}

    async def reviewer(task: Any, context: Any) -> dict[str, Any]:
        return {"summary": "Reviewed", "private_scratchpad": "internal", "blockers": []}

    async def coder(task: Any, context: Any) -> dict[str, Any]:
        runtime = holder["runtime"]
        child_context = _worker_context(task, agent_id="coder")
        child = await runtime.delegate(
            child_context,
            TeamDelegationRequest(
                objective="Review the change",
                role="reviewer",
                requested_capabilities=frozenset({"repo.read"}),
                parent_job_id=task.metadata["job_id"],
            ),
        )
        handoff = await runtime.wait_handoff(child_context, child.id)
        return {"summary": f"Implemented; review {handoff.summary}", "artifact_refs": ["a://1"]}

    async def scenario() -> None:
        runtime = _build(coder, reviewer)
        holder["runtime"] = runtime
        lead = _context()
        reference = await runtime.delegate(
            lead,
            TeamDelegationRequest(
                objective="Implement",
                role="coder",
                requested_capabilities=frozenset({"repo.read", "repo.write"}),
            ),
        )
        handoff = await asyncio.wait_for(runtime.wait_handoff(lead, reference.id), timeout=10)

        assert handoff.status == JobStatus.COMPLETED
        assert handoff.summary == "Implemented; review Reviewed"
        children = await runtime.jobs.store.list_jobs(
            tenant_id="tenant-1", user_id="user-1", parent_job_id=reference.id
        )
        assert len(children) == 1
        parent = await runtime.jobs.get(lead, reference.id)
        assert (parent.delegation_depth, children[0].delegation_depth) == (1, 2)
        assert children[0].capabilities == frozenset({"repo.read"})
        assert children[0].status == JobStatus.COMPLETED

    asyncio.run(scenario())


def test_child_failure_propagates_without_leaking_internal_detail() -> None:
    holder: dict[str, TeamRuntime] = {}

    async def reviewer(task: Any, context: Any) -> dict[str, Any]:
        raise RuntimeError(f"reviewer exploded {SECRET}")

    async def coder(task: Any, context: Any) -> dict[str, Any]:
        runtime = holder["runtime"]
        child_context = _worker_context(task, agent_id="coder")
        child = await runtime.delegate(
            child_context,
            TeamDelegationRequest(
                objective="Review",
                role="reviewer",
                requested_capabilities=frozenset({"repo.read"}),
                parent_job_id=task.metadata["job_id"],
            ),
        )
        handoff = await runtime.wait_handoff(child_context, child.id)
        blockers = [] if handoff.status == JobStatus.COMPLETED else ["review did not complete"]
        return {"summary": "Implemented", "blockers": blockers}

    async def scenario() -> None:
        runtime = _build(coder, reviewer)
        holder["runtime"] = runtime
        lead = _context()
        reference = await runtime.delegate(
            lead,
            TeamDelegationRequest(
                objective="Implement",
                role="coder",
                requested_capabilities=frozenset({"repo.read", "repo.write"}),
            ),
        )
        handoff = await asyncio.wait_for(runtime.wait_handoff(lead, reference.id), timeout=10)

        assert handoff.status == JobStatus.COMPLETED
        assert handoff.blockers == ("review did not complete",)
        children = await runtime.jobs.store.list_jobs(
            tenant_id="tenant-1", user_id="user-1", parent_job_id=reference.id
        )
        assert [child.status for child in children] == [JobStatus.FAILED]
        child_handoff = await runtime.wait_handoff(lead, children[0].id)
        assert child_handoff.status == JobStatus.FAILED
        assert SECRET not in child_handoff.model_dump_json()

    asyncio.run(scenario())


def test_cancelling_the_parent_cascades_to_children_and_leaves_no_background_tasks() -> None:
    holder: dict[str, TeamRuntime] = {}
    child_started = asyncio.Event()

    async def reviewer(task: Any, context: Any) -> dict[str, Any]:
        child_started.set()
        while True:  # cooperative: cancellation is delivered at checkpoints
            await context.checkpoint()
            await asyncio.sleep(0.01)

    async def coder(task: Any, context: Any) -> dict[str, Any]:
        runtime = holder["runtime"]
        child_context = _worker_context(task, agent_id="coder")
        child = await runtime.delegate(
            child_context,
            TeamDelegationRequest(
                objective="Review",
                role="reviewer",
                requested_capabilities=frozenset({"repo.read"}),
                parent_job_id=task.metadata["job_id"],
            ),
        )
        await runtime.wait_handoff(child_context, child.id)
        while True:
            await context.checkpoint()
            await asyncio.sleep(0.01)

    async def scenario() -> None:
        runtime = _build(coder, reviewer)
        holder["runtime"] = runtime
        lead = _context()
        reference = await runtime.delegate(
            lead,
            TeamDelegationRequest(
                objective="Implement",
                role="coder",
                requested_capabilities=frozenset({"repo.read", "repo.write"}),
            ),
        )
        await asyncio.wait_for(child_started.wait(), timeout=10)
        await runtime.jobs.cancel(lead, reference.id)
        parent = await asyncio.wait_for(runtime.jobs.wait(lead, reference.id), timeout=10)
        children = await runtime.jobs.store.list_jobs(
            tenant_id="tenant-1", user_id="user-1", parent_job_id=reference.id
        )
        child = await asyncio.wait_for(runtime.jobs.wait(lead, children[0].id), timeout=10)

        assert parent.status == JobStatus.CANCELLED
        assert child.status == JobStatus.CANCELLED
        assert not runtime.jobs._background

    asyncio.run(scenario())


def test_capabilities_cannot_be_escalated_down_the_delegation_chain() -> None:
    holder: dict[str, TeamRuntime] = {}
    outcome: dict[str, str] = {}

    async def coder(task: Any, context: Any) -> dict[str, Any]:
        runtime = holder["runtime"]
        try:
            await runtime.delegate(
                _worker_context(task, agent_id="coder"),
                TeamDelegationRequest(
                    objective="Write more",
                    role="coder",
                    requested_capabilities=frozenset({"repo.write"}),
                    parent_job_id=task.metadata["job_id"],
                ),
            )
            outcome["result"] = "escalated"
        except PermissionError:
            outcome["result"] = "denied"
        return {}

    async def scenario() -> None:
        runtime = _build(coder, _noop)
        holder["runtime"] = runtime
        lead = _context()
        # The coder is granted read only, although its role could hold write.
        reference = await runtime.delegate(
            lead,
            TeamDelegationRequest(
                objective="Read only", role="coder", requested_capabilities=frozenset({"repo.read"})
            ),
        )
        await asyncio.wait_for(runtime.wait_handoff(lead, reference.id), timeout=10)
        assert outcome["result"] == "denied"

    asyncio.run(scenario())


def test_delegation_depth_is_bounded_in_a_real_chain() -> None:
    holder: dict[str, TeamRuntime] = {}
    outcome: dict[str, str] = {}

    async def coder(task: Any, context: Any) -> dict[str, Any]:
        runtime = holder["runtime"]
        try:
            await runtime.delegate(
                _worker_context(task, agent_id="coder"),
                TeamDelegationRequest(
                    objective="Go deeper",
                    role="coder",
                    requested_capabilities=frozenset({"repo.read"}),
                    parent_job_id=task.metadata["job_id"],
                ),
            )
            outcome["result"] = "delegated"
        except PermissionError:
            outcome["result"] = "denied"
        return {}

    async def scenario() -> None:
        runtime = _build(coder, _noop, max_depth=1)
        holder["runtime"] = runtime
        lead = _context()
        reference = await runtime.delegate(
            lead,
            TeamDelegationRequest(
                objective="Top", role="coder", requested_capabilities=frozenset({"repo.read"})
            ),
        )
        await asyncio.wait_for(runtime.wait_handoff(lead, reference.id), timeout=10)
        assert outcome["result"] == "denied"

    asyncio.run(scenario())


def test_writers_sharing_a_resource_run_one_at_a_time() -> None:
    active = 0
    peak = 0

    async def coder(task: Any, context: Any) -> dict[str, Any]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return {"summary": "wrote"}

    async def scenario() -> None:
        runtime = _build(coder, _noop)
        lead = _context()
        references = [
            await runtime.delegate(
                lead,
                TeamDelegationRequest(
                    objective=f"Write {index}",
                    role="coder",
                    requested_capabilities=frozenset({"repo.write"}),
                    resource_keys=("repo:main",),
                ),
            )
            for index in range(3)
        ]
        handoffs = await asyncio.wait_for(
            asyncio.gather(*(runtime.wait_handoff(lead, item.id) for item in references)),
            timeout=15,
        )
        assert all(item.status == JobStatus.COMPLETED for item in handoffs)
        assert peak == 1, "writers sharing a resource key overlapped"

    asyncio.run(scenario())

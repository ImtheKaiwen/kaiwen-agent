from __future__ import annotations

import asyncio

import pytest

from kaiwen_agent.agents import AgentDefinition, AgentDefinitionRegistry, VersionedReference
from kaiwen_agent.jobs import (
    JobContext,
    JobManager,
    JobSubmitRequest,
    SchedulerTaskRuntimeAdapter,
    SubagentDelegator,
)
from kaiwen_agent.policy import DelegationPolicy, PolicyEngine, PolicyRegistry
from kaiwen_agent.teams import (
    TeamCompositionError,
    TeamContextReference,
    TeamDefinition,
    TeamDefinitionRegistry,
    TeamDelegationRequest,
    TeamMemberDefinition,
    TeamRuntime,
)
from kaiwen_agent.workers import FunctionWorker, WorkerRegistry


def _agent(agent_id: str, *, permissions: frozenset[str]) -> AgentDefinition:
    return AgentDefinition(
        id=agent_id,
        version=1,
        role=agent_id,
        instructions_ref=VersionedReference(id=f"{agent_id}-prompt", version=1),
        provider_ref="fake",
        capability_manifest_ref=VersionedReference(id=agent_id, version=1),
        permissions=permissions,
    )


def _definition() -> TeamDefinition:
    return TeamDefinition(
        id="software",
        version=1,
        lead_agent_ref=VersionedReference(id="lead", version=1),
        members=(
            TeamMemberDefinition(
                role="coder",
                agent_ref=VersionedReference(id="coder", version=1),
                capabilities=frozenset({"repo.read", "repo.write"}),
                may_delegate=True,
            ),
            TeamMemberDefinition(
                role="reviewer",
                agent_ref=VersionedReference(id="reviewer", version=1),
                capabilities=frozenset({"repo.read"}),
            ),
        ),
    )


def _context(
    *, agent_id: str = "lead", capabilities: frozenset[str] = frozenset({"repo.read", "repo.write"})
) -> JobContext:
    return JobContext(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        conversation_id="conversation-1",
        agent_id=agent_id,
        run_id="run-1",
        trace_id="trace-1",
        capabilities=capabilities,
    )


def _runtime(worker) -> TeamRuntime:
    workers = WorkerRegistry()
    workers.register("coder", FunctionWorker(worker))
    workers.register("reviewer", FunctionWorker(worker))
    jobs = JobManager(
        runtime=SchedulerTaskRuntimeAdapter(workers=workers),
        allowed_worker_ids={"coder", "reviewer"},
    )
    policy = PolicyEngine(
        registry=PolicyRegistry(
            [
                DelegationPolicy(
                    max_depth=3,
                    max_parallel_jobs=2,
                    max_jobs_per_run=5,
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


def test_team_registry_is_versioned_and_immutable() -> None:
    registry = TeamDefinitionRegistry([_definition()])
    assert registry.get("software", 1) == _definition()
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_definition())


def test_team_selects_least_privileged_member_and_returns_bounded_handoff() -> None:
    async def worker(task, context):
        del context
        assert task.agent_id == "reviewer"
        assert task.metadata["context_ref"] == {
            "spec": "artifact://spec-1",
            "kaiwen.team": "software@1",
            "kaiwen.agent": "reviewer@1",
            "kaiwen.role": "reviewer",
        }
        return {
            "summary": "Review complete",
            "artifact_refs": ["artifact://report-1"],
            "evidence_refs": ["artifact://evidence-1"],
            "blockers": [],
            "follow_up_suggestions": ["Merge after approval"],
            "private_scratchpad": "must not leave the child boundary",
        }

    async def scenario() -> None:
        runtime = _runtime(worker)
        reference = await runtime.delegate(
            _context(),
            TeamDelegationRequest(
                objective="Review the change",
                requested_capabilities=frozenset({"repo.read"}),
                context_refs=(TeamContextReference(name="spec", reference="artifact://spec-1"),),
            ),
        )
        handoff = await runtime.wait_handoff(_context(), reference.id)
        assert handoff.role == "reviewer"
        assert handoff.summary == "Review complete"
        assert handoff.artifact_refs == ("artifact://report-1",)
        assert "private_scratchpad" not in handoff.model_dump()

    asyncio.run(scenario())


def test_team_rejects_role_capability_escalation_before_job_creation() -> None:
    async def worker(task, context):
        del task, context
        return {}

    async def scenario() -> None:
        runtime = _runtime(worker)
        with pytest.raises(TeamCompositionError, match="selected role"):
            await runtime.delegate(
                _context(),
                TeamDelegationRequest(
                    objective="Reviewer writes code",
                    role="reviewer",
                    requested_capabilities=frozenset({"repo.write"}),
                ),
            )
        assert await runtime.jobs.list(_context()) == []

    asyncio.run(scenario())


def test_non_lead_member_requires_explicit_delegation_permission() -> None:
    async def worker(task, context):
        del task, context
        return {}

    async def scenario() -> None:
        runtime = _runtime(worker)
        with pytest.raises(TeamCompositionError, match="may not delegate"):
            await runtime.delegate(
                _context(agent_id="reviewer", capabilities=frozenset({"repo.read"})),
                TeamDelegationRequest(
                    objective="Delegate from reviewer",
                    role="reviewer",
                    requested_capabilities=frozenset({"repo.read"}),
                ),
            )

    asyncio.run(scenario())


def test_handoff_rejects_job_without_exact_team_pins() -> None:
    async def worker(task, context):
        del task, context
        return {"summary": "Not a team job"}

    async def scenario() -> None:
        runtime = _runtime(worker)
        reference = await runtime.jobs.submit(
            _context(),
            JobSubmitRequest(worker_agent_id="reviewer", objective="External job"),
        )
        with pytest.raises(TeamCompositionError, match="not pinned"):
            await runtime.wait_handoff(_context(), reference.id)

    asyncio.run(scenario())


def test_team_definition_cannot_exceed_registered_agent_permissions() -> None:
    async def worker(task, context):
        del task, context
        return {}

    workers = WorkerRegistry()
    workers.register("coder", FunctionWorker(worker))
    workers.register("reviewer", FunctionWorker(worker))
    jobs = JobManager(
        runtime=SchedulerTaskRuntimeAdapter(workers=workers),
        allowed_worker_ids={"coder", "reviewer"},
    )
    agents = AgentDefinitionRegistry(
        [
            _agent("lead", permissions=frozenset({"repo.read"})),
            _agent("coder", permissions=frozenset({"repo.read"})),
            _agent("reviewer", permissions=frozenset({"repo.read"})),
        ]
    )
    with pytest.raises(TeamCompositionError, match="exceeds agent permissions"):
        TeamRuntime(
            definition=_definition(),
            agents=agents,
            delegator=SubagentDelegator(
                jobs=jobs,
                policy_engine=PolicyEngine(
                    registry=PolicyRegistry(
                        [
                            DelegationPolicy(
                                max_depth=1,
                                max_parallel_jobs=1,
                                allowed_delegates={"coder", "reviewer"},
                            )
                        ]
                    )
                ),
            ),
            jobs=jobs,
        )

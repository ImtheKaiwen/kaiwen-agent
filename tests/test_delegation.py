from __future__ import annotations

import asyncio

import pytest

from kaiwen_agent.context import AgentContext
from kaiwen_agent.jobs import (
    DelegationDeniedError,
    DelegationRequest,
    JobContext,
    JobManager,
    JobStatus,
    SchedulerTaskRuntimeAdapter,
    SubagentDelegator,
    TextJobBridge,
)
from kaiwen_agent.policy import DelegationPolicy, PolicyEngine, PolicyRegistry
from kaiwen_agent.realtime import (
    FakeRealtimeProvider,
    RealtimeJobBridge,
    RealtimeJobPermissionError,
    RealtimeSessionConfig,
    RealtimeSessionManager,
)
from kaiwen_agent.tasks import AgentTask
from kaiwen_agent.workers import FunctionWorker, TaskExecutionContext, WorkerRegistry


def job_context(
    *,
    agent_id: str = "admin-agent",
    capabilities: frozenset[str] = frozenset({"projects.read", "logs.read"}),
    delegation_depth: int = 0,
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
        delegation_depth=delegation_depth,
    )


def delegation_runtime(
    worker: FunctionWorker,
    *,
    max_depth: int = 2,
    max_parallel_jobs: int = 3,
    max_jobs_per_run: int | None = 10,
) -> tuple[JobManager, SubagentDelegator]:
    workers = WorkerRegistry()
    workers.register("research-agent", worker)
    jobs = JobManager(
        runtime=SchedulerTaskRuntimeAdapter(workers=workers),
        allowed_worker_ids={"research-agent"},
    )
    policy = PolicyEngine(
        registry=PolicyRegistry(
            [
                DelegationPolicy(
                    max_depth=max_depth,
                    max_parallel_jobs=max_parallel_jobs,
                    max_jobs_per_run=max_jobs_per_run,
                    allowed_delegates={"research-agent"},
                )
            ]
        )
    )
    return jobs, SubagentDelegator(jobs=jobs, policy_engine=policy)


async def immediate_worker(task: AgentTask, execution: TaskExecutionContext) -> dict[str, object]:
    del execution
    return {"objective": task.name}


def test_delegation_grants_only_requested_capability_subset() -> None:
    async def scenario() -> None:
        observed: list[AgentTask] = []

        async def capture_worker(
            task: AgentTask, execution: TaskExecutionContext
        ) -> dict[str, object]:
            del execution
            observed.append(task)
            return {}

        jobs, delegator = delegation_runtime(FunctionWorker(capture_worker))
        reference = await delegator.delegate(
            job_context(),
            DelegationRequest(
                worker_agent_id="research-agent",
                objective="Inspect logs",
                requested_capabilities={"logs.read"},
            ),
        )
        completed = await jobs.wait(job_context(), reference.id)
        assert completed.status == JobStatus.COMPLETED
        assert completed.capabilities == frozenset({"logs.read"})
        assert completed.delegation_depth == 1
        assert observed[0].capabilities == frozenset({"logs.read"})
        assert observed[0].delegation_depth == 1

    asyncio.run(scenario())


def test_capability_escalation_is_denied_before_job_creation() -> None:
    async def scenario() -> None:
        jobs, delegator = delegation_runtime(FunctionWorker(immediate_worker))
        with pytest.raises(DelegationDeniedError, match="exceed caller"):
            await delegator.delegate(
                job_context(),
                DelegationRequest(
                    worker_agent_id="research-agent",
                    objective="Deploy",
                    requested_capabilities={"deploy.write"},
                ),
            )
        assert await jobs.list(job_context()) == []

    asyncio.run(scenario())


def test_depth_and_parallel_limits_are_policy_enforced() -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def blocking_worker(
            task: AgentTask, execution: TaskExecutionContext
        ) -> dict[str, object]:
            del task, execution
            await release.wait()
            return {}

        jobs, delegator = delegation_runtime(
            FunctionWorker(blocking_worker), max_depth=2, max_parallel_jobs=1
        )
        first = await delegator.delegate(
            job_context(),
            DelegationRequest(worker_agent_id="research-agent", objective="First"),
        )
        await asyncio.sleep(0)
        with pytest.raises(DelegationDeniedError, match="parallel"):
            await delegator.delegate(
                job_context(),
                DelegationRequest(worker_agent_id="research-agent", objective="Second"),
            )
        with pytest.raises(DelegationDeniedError, match="depth"):
            await delegator.delegate(
                job_context(delegation_depth=2),
                DelegationRequest(worker_agent_id="research-agent", objective="Too deep"),
            )
        release.set()
        await jobs.wait(job_context(), first.id)

    asyncio.run(scenario())


def test_concurrent_submissions_cannot_race_past_parallel_limit() -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def blocking_worker(
            task: AgentTask, execution: TaskExecutionContext
        ) -> dict[str, object]:
            del task, execution
            await release.wait()
            return {}

        jobs, delegator = delegation_runtime(FunctionWorker(blocking_worker), max_parallel_jobs=1)
        results = await asyncio.gather(
            delegator.delegate(
                job_context(),
                DelegationRequest(worker_agent_id="research-agent", objective="First"),
            ),
            delegator.delegate(
                job_context(),
                DelegationRequest(worker_agent_id="research-agent", objective="Second"),
            ),
            return_exceptions=True,
        )
        references = [result for result in results if not isinstance(result, Exception)]
        denials = [result for result in results if isinstance(result, Exception)]
        assert len(references) == 1
        assert len(denials) == 1
        assert isinstance(denials[0], DelegationDeniedError)
        release.set()
        await jobs.wait(job_context(), references[0].id)

    asyncio.run(scenario())


def test_per_run_limit_counts_completed_delegations() -> None:
    async def scenario() -> None:
        jobs, delegator = delegation_runtime(FunctionWorker(immediate_worker), max_jobs_per_run=1)
        first = await delegator.delegate(
            job_context(),
            DelegationRequest(worker_agent_id="research-agent", objective="First"),
        )
        await jobs.wait(job_context(), first.id)
        with pytest.raises(DelegationDeniedError, match="per run"):
            await delegator.delegate(
                job_context(),
                DelegationRequest(worker_agent_id="research-agent", objective="Second"),
            )

    asyncio.run(scenario())


def test_child_cannot_exceed_parent_capabilities() -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def parent_worker(
            task: AgentTask, execution: TaskExecutionContext
        ) -> dict[str, object]:
            del task, execution
            await release.wait()
            return {}

        jobs, delegator = delegation_runtime(FunctionWorker(parent_worker))
        parent_ref = await delegator.delegate(
            job_context(),
            DelegationRequest(
                worker_agent_id="research-agent",
                objective="Parent",
                requested_capabilities={"logs.read"},
            ),
        )
        child_context = job_context(
            agent_id="research-agent",
            capabilities=frozenset({"logs.read"}),
            delegation_depth=1,
        )
        with pytest.raises(DelegationDeniedError, match="caller"):
            await delegator.delegate(
                child_context,
                DelegationRequest(
                    worker_agent_id="research-agent",
                    objective="Escalating child",
                    parent_job_id=parent_ref.id,
                    requested_capabilities={"projects.read"},
                ),
            )
        release.set()
        await jobs.wait(job_context(), parent_ref.id)

    asyncio.run(scenario())


def test_text_and_realtime_bridges_share_the_same_job_manager() -> None:
    async def scenario() -> None:
        jobs, delegator = delegation_runtime(FunctionWorker(immediate_worker))
        text = TextJobBridge(jobs=jobs, delegator=delegator)
        text_context = AgentContext(
            session_id="text-session",
            tenant_id="tenant-1",
            user_id="user-1",
            agent_id="admin-agent",
            conversation_id="conversation-1",
            trace_id="text-trace",
            permissions=frozenset({"logs.read"}),
            metadata={"run_id": "text-run"},
        )
        text_ref = await text.delegate(
            text_context,
            DelegationRequest(worker_agent_id="research-agent", objective="Text job"),
        )

        sessions = RealtimeSessionManager([FakeRealtimeProvider()])
        session = await sessions.create_session(
            RealtimeSessionConfig(provider="fake", model="fake-live"),
            conversation_id="conversation-1",
            tenant_id="tenant-1",
            user_id="user-1",
        )
        realtime = RealtimeJobBridge(
            sessions,
            jobs=jobs,
            delegator=delegator,
            agent_id="admin-agent",
            capability_resolver=lambda record: frozenset({"logs.read"}),
        )
        realtime_ref = await realtime.delegate(
            session.id,
            DelegationRequest(worker_agent_id="research-agent", objective="Voice job"),
        )
        await jobs.wait(job_context(), text_ref.id)
        await jobs.wait(job_context(), realtime_ref.id)
        visible = await jobs.list(job_context())
        assert {job.id for job in visible} == {text_ref.id, realtime_ref.id}
        assert (await realtime.status(session.id, realtime_ref.id)).id == realtime_ref.id

    asyncio.run(scenario())


def test_realtime_bridge_respects_session_delegation_flag() -> None:
    async def scenario() -> None:
        jobs, delegator = delegation_runtime(FunctionWorker(immediate_worker))
        sessions = RealtimeSessionManager([FakeRealtimeProvider()])
        session = await sessions.create_session(
            RealtimeSessionConfig(provider="fake", model="fake-live", task_delegation=False),
            conversation_id="conversation-1",
            tenant_id="tenant-1",
            user_id="user-1",
        )
        realtime = RealtimeJobBridge(
            sessions,
            jobs=jobs,
            delegator=delegator,
            agent_id="admin-agent",
            capability_resolver=lambda record: frozenset({"logs.read"}),
        )
        with pytest.raises(RealtimeJobPermissionError, match="disabled"):
            await realtime.delegate(
                session.id,
                DelegationRequest(worker_agent_id="research-agent", objective="Voice job"),
            )
        assert await jobs.list(job_context()) == []

    asyncio.run(scenario())

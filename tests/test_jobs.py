from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kaiwen_agent.dead_letter import DeadLetterContext, DeadLetterManager
from kaiwen_agent.events import InMemoryEventSink
from kaiwen_agent.jobs import (
    AgentJob,
    InMemoryJobStore,
    JobContext,
    JobManager,
    JobStatus,
    JobStore,
    JobSubmitRequest,
    LifecycleJobCompletionNotifier,
    SchedulerTaskRuntimeAdapter,
)
from kaiwen_agent.lifecycle import InMemoryLifecycleStore, WakeTriggerType, WakeupManager
from kaiwen_agent.persistence import SQLitePersistence
from kaiwen_agent.tasks import AgentTask
from kaiwen_agent.workers import FunctionWorker, TaskExecutionContext, WorkerRegistry


def context(*, tenant_id: str = "tenant-1", user_id: str = "user-1") -> JobContext:
    return JobContext(
        tenant_id=tenant_id,
        user_id=user_id,
        session_id="session-1",
        conversation_id="conversation-1",
        agent_id="admin-agent",
        run_id="run-1",
        trace_id="trace-1",
    )


def manager_for(worker: FunctionWorker, *, store: JobStore | None = None) -> JobManager:
    workers = WorkerRegistry()
    workers.register("worker-agent", worker)
    return JobManager(
        runtime=SchedulerTaskRuntimeAdapter(workers=workers),
        allowed_worker_ids={"worker-agent"},
        store=store,
    )


def test_submit_returns_without_blocking_and_reports_progress() -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def work(task: AgentTask, execution: TaskExecutionContext) -> dict[str, object]:
            del task
            await execution.report_progress(0.5, details={"phase": "half"})
            await release.wait()
            return {"ok": True}

        events = InMemoryEventSink()
        workers = WorkerRegistry()
        workers.register("worker-agent", FunctionWorker(work))
        manager = JobManager(
            runtime=SchedulerTaskRuntimeAdapter(workers=workers),
            allowed_worker_ids={"worker-agent"},
            events=events,
        )
        reference = await manager.submit(
            context(),
            JobSubmitRequest(worker_agent_id="worker-agent", objective="Check health"),
        )
        assert reference.status == JobStatus.QUEUED
        await asyncio.sleep(0)
        release.set()
        completed = await manager.wait(context(), reference.id)
        assert completed.status == JobStatus.COMPLETED
        assert completed.progress == 1
        assert completed.result == {"ok": True}
        types = [event.type for event in events.events]
        assert types[:2] == ["job.created", "job.queued"]
        assert "job.progress" in types
        assert types[-1] == "job.completed"

    asyncio.run(scenario())


def test_job_visibility_is_tenant_and_user_scoped() -> None:
    async def scenario() -> None:
        async def work(task: AgentTask, execution: TaskExecutionContext) -> dict[str, object]:
            del task, execution
            return {}

        manager = manager_for(FunctionWorker(work))
        reference = await manager.submit(
            context(),
            JobSubmitRequest(worker_agent_id="worker-agent", objective="Scoped"),
        )
        await manager.wait(context(), reference.id)
        with pytest.raises(KeyError):
            await manager.get(context(user_id="user-2"), reference.id)
        with pytest.raises(KeyError):
            await manager.get(context(tenant_id="tenant-2"), reference.id)

    asyncio.run(scenario())


def test_failed_job_wakes_requesting_agent_and_enters_dead_letter_queue() -> None:
    async def scenario() -> None:
        async def fail(task: AgentTask, execution: TaskExecutionContext) -> dict[str, object]:
            del task, execution
            raise RuntimeError("provider failed")

        lifecycle_store = InMemoryLifecycleStore()
        wakeups = WakeupManager(store=lifecycle_store)
        dead_letters = DeadLetterManager()
        workers = WorkerRegistry()
        workers.register("worker-agent", FunctionWorker(fail))
        manager = JobManager(
            runtime=SchedulerTaskRuntimeAdapter(workers=workers),
            allowed_worker_ids={"worker-agent"},
            completion_notifier=LifecycleJobCompletionNotifier(wakeups=wakeups),
            dead_letters=dead_letters,
        )
        reference = await manager.submit(
            context(),
            JobSubmitRequest(worker_agent_id="worker-agent", objective="Fail safely"),
        )
        failed = await manager.wait(context(), reference.id)
        assert failed.status == JobStatus.FAILED
        wakeup = await wakeups.next_scheduled(agent_id="admin-agent")
        assert wakeup is not None
        assert wakeup.trigger.type == WakeTriggerType.JOB_FAILED
        assert wakeup.trigger.metadata["job_id"] == reference.id
        records = await dead_letters.list(
            DeadLetterContext(tenant_id="tenant-1", user_id="user-1", trace_id="trace-1")
        )
        assert len(records) == 1
        assert records[0].reference_id == reference.id

    asyncio.run(scenario())


def test_worker_allowlist_is_checked_before_job_creation() -> None:
    async def scenario() -> None:
        async def work(task: AgentTask, execution: TaskExecutionContext) -> dict[str, object]:
            del task, execution
            return {}

        manager = manager_for(FunctionWorker(work))
        with pytest.raises(PermissionError):
            await manager.submit(
                context(),
                JobSubmitRequest(worker_agent_id="unknown", objective="Not allowed"),
            )
        assert await manager.list(context()) == []

    asyncio.run(scenario())


def test_running_job_cancels_cooperatively() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        checkpoint = asyncio.Event()

        async def work(task: AgentTask, execution: TaskExecutionContext) -> dict[str, object]:
            del task
            started.set()
            await checkpoint.wait()
            await execution.checkpoint(state={"safe": True})
            return {}

        manager = manager_for(FunctionWorker(work))
        reference = await manager.submit(
            context(),
            JobSubmitRequest(worker_agent_id="worker-agent", objective="Cancelable"),
        )
        await started.wait()
        requested = await manager.cancel(context(), reference.id)
        assert requested.status == JobStatus.CANCEL_REQUESTED
        checkpoint.set()
        cancelled = await manager.wait(context(), reference.id)
        assert cancelled.status == JobStatus.CANCELLED

    asyncio.run(scenario())


def test_child_progress_updates_parent() -> None:
    async def scenario() -> None:
        parent_release = asyncio.Event()

        async def work(task: AgentTask, execution: TaskExecutionContext) -> dict[str, object]:
            del execution
            if task.input.get("parent"):
                await parent_release.wait()
            return {"done": True}

        manager = manager_for(FunctionWorker(work))
        parent_ref = await manager.submit(
            context(),
            JobSubmitRequest(
                worker_agent_id="worker-agent",
                objective="Parent",
                input={"parent": True},
            ),
        )
        await asyncio.sleep(0)
        child_ref = await manager.submit(
            context(),
            JobSubmitRequest(
                worker_agent_id="worker-agent",
                objective="Child",
                parent_job_id=parent_ref.id,
            ),
        )
        child = await manager.wait(context(), child_ref.id)
        assert child.status == JobStatus.COMPLETED
        parent = await manager.get(context(), parent_ref.id)
        assert parent.progress == 1
        parent_release.set()
        await manager.wait(context(), parent_ref.id)

    asyncio.run(scenario())


def test_sqlite_restart_requeues_and_completes_job(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "jobs.db"
        first_store = SQLitePersistence(path)
        interrupted = AgentJob(
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            conversation_id="conversation-1",
            requested_by_agent_id="admin-agent",
            worker_agent_id="worker-agent",
            objective="Recover me",
            status=JobStatus.RUNNING,
        )
        await first_store.save_job(interrupted)

        async def work(task: AgentTask, execution: TaskExecutionContext) -> dict[str, object]:
            del task, execution
            return {"recovered": True}

        restarted = SQLitePersistence(path)
        manager = manager_for(FunctionWorker(work), store=restarted)
        assert await manager.recover() == 1
        completed = await manager.wait(context(), interrupted.id)
        assert completed.status == JobStatus.COMPLETED
        assert completed.result == {"recovered": True}

    asyncio.run(scenario())


def test_sqlite_counts_delegations_by_agent_run_and_activity(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "job-counts.db")
        active = AgentJob(
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            requested_by_agent_id="admin-agent",
            worker_agent_id="worker-agent",
            objective="Active",
            parent_run_id="run-1",
        )
        completed = active.model_copy(
            update={
                "id": "job_completed",
                "task_id": "task_completed",
                "status": JobStatus.COMPLETED,
            }
        )
        other_agent = active.model_copy(
            update={
                "id": "job_other",
                "task_id": "task_other",
                "requested_by_agent_id": "other-agent",
            }
        )
        for job in (active, completed, other_agent):
            await store.save_job(job)

        assert (
            await store.count_jobs(
                tenant_id="tenant-1",
                user_id="user-1",
                requested_by_agent_id="admin-agent",
                parent_run_id="run-1",
            )
            == 2
        )
        assert (
            await store.count_jobs(
                tenant_id="tenant-1",
                user_id="user-1",
                requested_by_agent_id="admin-agent",
                active_only=True,
            )
            == 1
        )

    asyncio.run(scenario())


def test_atomic_job_claim_allows_one_runner() -> None:
    async def scenario() -> None:
        store = InMemoryJobStore()
        job = AgentJob(
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            requested_by_agent_id="admin-agent",
            worker_agent_id="worker-agent",
            objective="Claim once",
        )
        await store.save_job(job)
        claims = await asyncio.gather(store.claim_job(job.id), store.claim_job(job.id))
        assert sum(claim is not None for claim in claims) == 1

    asyncio.run(scenario())


def test_runtime_adapter_enforces_global_concurrency() -> None:
    async def scenario() -> None:
        active = 0
        maximum = 0
        first_started = asyncio.Event()
        release = asyncio.Event()

        async def work(task: AgentTask, execution: TaskExecutionContext) -> dict[str, object]:
            nonlocal active, maximum
            del task, execution
            active += 1
            maximum = max(maximum, active)
            first_started.set()
            await release.wait()
            active -= 1
            return {}

        workers = WorkerRegistry()
        workers.register("worker-agent", FunctionWorker(work))
        manager = JobManager(
            runtime=SchedulerTaskRuntimeAdapter(workers=workers, max_concurrency=1),
            allowed_worker_ids={"worker-agent"},
        )
        first = await manager.submit(
            context(), JobSubmitRequest(worker_agent_id="worker-agent", objective="First")
        )
        second = await manager.submit(
            context(), JobSubmitRequest(worker_agent_id="worker-agent", objective="Second")
        )
        await first_started.wait()
        await asyncio.sleep(0)
        assert maximum == 1
        release.set()
        await manager.wait(context(), first.id)
        await manager.wait(context(), second.id)
        assert maximum == 1

    asyncio.run(scenario())

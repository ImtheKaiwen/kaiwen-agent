from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kaiwen_agent.audit import SecretRedactor
from kaiwen_agent.backpressure import (
    BackpressureController,
    BackpressureLimits,
    WorkloadKind,
)
from kaiwen_agent.dead_letter import (
    DeadLetterContext,
    DeadLetterKind,
    DeadLetterManager,
    DeadLetterStatus,
    SQLiteDeadLetterStore,
)
from kaiwen_agent.evaluation import (
    AssertionOperator,
    EvaluationAssertion,
    EvaluationCase,
    EvaluationCategory,
    EvaluationRunner,
    EvaluationSuite,
)
from kaiwen_agent.events import AgentEvent
from kaiwen_agent.health import (
    HealthStatus,
    HealthThresholds,
    HeartbeatMonitor,
)
from kaiwen_agent.policy import PolicyContext, PolicyDecisionType
from kaiwen_agent.replay import (
    InMemoryReplayStore,
    ReplayPlayer,
    ReplayRecorder,
    SimulationSideEffectPolicy,
    SQLiteReplayStore,
)
from kaiwen_agent.secrets import (
    EnvironmentSecretProvider,
    SecretNotFoundError,
)


class FakeEvaluationTarget:
    async def evaluate(self, case: EvaluationCase, *, simulation: bool) -> dict[str, Any]:
        return {
            "simulation": simulation,
            "memory": {"stored": False},
            "tools": ["projects.read"],
            "case_id": case.id,
        }


def test_evaluation_runner_uses_structured_assertions_and_simulation() -> None:
    suite = EvaluationSuite(
        id="memory-safety",
        version=1,
        cases=(
            EvaluationCase(
                id="transient-fact",
                category=EvaluationCategory.MEMORY_OVERCOLLECTION,
                input={"text": "I am drinking coffee right now"},
                assertions=(
                    EvaluationAssertion(
                        path="memory.stored",
                        operator=AssertionOperator.EQUALS,
                        expected=False,
                    ),
                    EvaluationAssertion(
                        path="tools",
                        operator=AssertionOperator.NOT_CONTAINS,
                        expected="email.send",
                    ),
                ),
            ),
        ),
    )
    report = asyncio.run(EvaluationRunner(target=FakeEvaluationTarget()).run(suite))
    assert report.passed
    assert report.results[0].assertions[0].actual is False


class ReplayCollector:
    def __init__(self) -> None:
        self.events: list[tuple[str, bool]] = []

    async def observe(self, event: AgentEvent, *, simulation: bool) -> None:
        self.events.append((event.id, simulation))


def test_replay_is_ordered_idempotent_and_observation_only() -> None:
    async def scenario() -> None:
        store = InMemoryReplayStore()
        recorder = ReplayRecorder(store=store)
        first = AgentEvent(
            id="evt_1",
            type="run.started",
            trace_id="trace_1",
            session_id="session_1",
            payload={"api_key": "secret"},
        )
        second = AgentEvent(
            id="evt_2",
            type="run.completed",
            trace_id="trace_1",
            session_id="session_1",
        )
        await recorder.append(first)
        await recorder.append(first)
        await recorder.append(second)
        collector = ReplayCollector()
        result = await ReplayPlayer(store=store).play("trace_1", collector)
        assert result.delivered_frames == 2
        assert collector.events == [("evt_1", True), ("evt_2", True)]
        trace = await ReplayPlayer(store=store).load("trace_1")
        assert trace.frames[0].event.payload == {"api_key": "[REDACTED]"}

    asyncio.run(scenario())


def test_sqlite_replay_survives_restart(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "replay.db"
        event = AgentEvent(
            id="evt_restart",
            type="run.started",
            trace_id="trace_restart",
            session_id="session_1",
        )
        await ReplayRecorder(store=SQLiteReplayStore(path)).append(event)
        trace = await ReplayPlayer(store=SQLiteReplayStore(path)).load("trace_restart")
        assert [frame.event.id for frame in trace.frames] == ["evt_restart"]

    asyncio.run(scenario())


def test_simulation_policy_denies_side_effecting_tools() -> None:
    async def scenario() -> None:
        policy = SimulationSideEffectPolicy()
        decision = await policy.evaluate(
            PolicyContext(
                agent_id="agent_1",
                session_id="session_1",
                trace_id="trace_1",
                environment="simulation",
                action="tool.execute:email.send",
                metadata={"side_effect": "reversible"},
            )
        )
        assert decision is not None
        assert decision.decision == PolicyDecisionType.DENY

    asyncio.run(scenario())


def test_dead_letter_queue_is_scoped_redacted_and_single_requeue() -> None:
    async def scenario() -> None:
        manager = DeadLetterManager()
        owner = DeadLetterContext(tenant_id="tenant_1", user_id="user_1", trace_id="trace_1")
        record = await manager.enqueue(
            owner,
            kind=DeadLetterKind.JOB,
            reference_id="job_1",
            reason_code="retries_exhausted",
            reason="Worker failed with sk-abcdefghijklmnop",
            payload={"api_key": "secret", "message": "Bearer abc.def"},
            attempts=3,
        )
        assert record.payload == {
            "api_key": "[REDACTED]",
            "message": "[REDACTED]",
        }
        assert record.reason == "Worker failed with [REDACTED]"
        with pytest.raises(KeyError):
            await manager.get(
                DeadLetterContext(tenant_id="tenant_2", user_id="user_1", trace_id="trace_2"),
                record.id,
            )
        requeued = await manager.mark_requeued(owner, record.id)
        assert requeued.status == DeadLetterStatus.REQUEUED
        with pytest.raises(ValueError, match="pending"):
            await manager.mark_requeued(owner, record.id)

    asyncio.run(scenario())


def test_sqlite_dead_letter_queue_survives_restart(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "dlq.db"
        context = DeadLetterContext(tenant_id="tenant_1", user_id="user_1", trace_id="trace_1")
        record = await DeadLetterManager(store=SQLiteDeadLetterStore(path)).enqueue(
            context,
            kind=DeadLetterKind.WEBHOOK,
            reference_id="webhook_1",
            reason_code="retries_exhausted",
            reason="Delivery failed",
        )
        loaded = await DeadLetterManager(store=SQLiteDeadLetterStore(path)).get(context, record.id)
        assert loaded.reference_id == "webhook_1"

    asyncio.run(scenario())


def test_dead_letter_queue_preserves_tenant_isolation_under_concurrency() -> None:
    async def scenario() -> None:
        manager = DeadLetterManager()
        contexts = {
            tenant_id: DeadLetterContext(
                tenant_id=tenant_id,
                user_id="user_1",
                trace_id=f"trace_{tenant_id}",
            )
            for tenant_id in ("tenant_a", "tenant_b")
        }
        await asyncio.gather(
            *(
                manager.enqueue(
                    contexts[tenant_id],
                    kind=DeadLetterKind.EVENT,
                    reference_id=f"{tenant_id}:{index}",
                    reason_code="delivery_failed",
                    reason="Delivery failed",
                )
                for tenant_id in contexts
                for index in range(20)
            )
        )
        tenant_a = await manager.list(contexts["tenant_a"], limit=100)
        tenant_b = await manager.list(contexts["tenant_b"], limit=100)
        assert len(tenant_a) == 20
        assert len(tenant_b) == 20
        assert all(record.tenant_id == "tenant_a" for record in tenant_a)
        assert all(record.tenant_id == "tenant_b" for record in tenant_b)

    asyncio.run(scenario())


def test_backpressure_enforces_concurrency_rate_and_tenant_isolation() -> None:
    async def scenario() -> None:
        controller = BackpressureController(
            limits={
                WorkloadKind.JOB: BackpressureLimits(
                    max_queue_depth=3,
                    max_concurrent=1,
                    max_admissions_per_minute=1,
                    low_priority_threshold=0,
                    low_priority_queue_depth=1,
                )
            }
        )
        now = datetime(2026, 1, 1, tzinfo=UTC)
        lease = await controller.acquire(
            tenant_id="tenant_1",
            kind=WorkloadKind.JOB,
            queue_depth=0,
            now=now,
        )
        assert lease.decision.admitted
        concurrent = await controller.acquire(
            tenant_id="tenant_1",
            kind=WorkloadKind.JOB,
            queue_depth=0,
            now=now,
        )
        assert concurrent.decision.reason_code == "concurrency_limit"
        other = await controller.acquire(
            tenant_id="tenant_2",
            kind=WorkloadKind.JOB,
            queue_depth=0,
            now=now,
        )
        assert other.decision.admitted
        await lease.release()
        rate_limited = await controller.acquire(
            tenant_id="tenant_1",
            kind=WorkloadKind.JOB,
            queue_depth=0,
            now=now,
        )
        assert rate_limited.decision.reason_code == "rate_limit"
        await other.release()

    asyncio.run(scenario())


def test_heartbeat_reports_unknown_healthy_degraded_and_offline() -> None:
    async def scenario() -> None:
        monitor = HeartbeatMonitor(
            {"scheduler": HealthThresholds(degraded_after_seconds=10, offline_after_seconds=20)}
        )
        now = datetime(2026, 1, 1, tzinfo=UTC)
        assert (await monitor.report(now=now)).status == HealthStatus.UNKNOWN
        await monitor.beat("scheduler", now=now)
        assert (await monitor.report(now=now)).status == HealthStatus.HEALTHY
        assert (
            await monitor.report(now=now + timedelta(seconds=10))
        ).status == HealthStatus.DEGRADED
        assert (
            await monitor.report(now=now + timedelta(seconds=20))
        ).status == HealthStatus.OFFLINE

    asyncio.run(scenario())


def test_environment_secret_provider_is_allowlisted_and_redacted() -> None:
    async def scenario() -> None:
        provider = EnvironmentSecretProvider(
            allowed_keys={"OPENAI_API_KEY"},
            environ={"OPENAI_API_KEY": "sk-abcdefghijklmnop"},
        )
        secret = await provider.get("OPENAI_API_KEY")
        assert secret.reveal() == "sk-abcdefghijklmnop"
        assert "abcdefghijklmnop" not in repr(secret)
        assert SecretRedactor().redact(secret.model_dump()) == {
            "key": "OPENAI_API_KEY",
            "value": "[REDACTED]",
        }
        with pytest.raises(PermissionError):
            await provider.get("UNLISTED_SECRET")
        missing = EnvironmentSecretProvider(allowed_keys={"OPENAI_API_KEY"}, environ={})
        with pytest.raises(SecretNotFoundError):
            await missing.get("OPENAI_API_KEY")

    asyncio.run(scenario())

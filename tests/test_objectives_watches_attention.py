from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from kaiwen_agent.attention import (
    AttentionContext,
    AttentionEngine,
    AttentionRequest,
    AttentionSeverity,
)
from kaiwen_agent.lifecycle import InMemoryLifecycleStore, WakeupManager, WakeupStatus
from kaiwen_agent.objectives import (
    InvalidObjectiveTransition,
    ObjectiveContext,
    ObjectiveCreate,
    ObjectiveManager,
    ObjectiveStatus,
)
from kaiwen_agent.persistence import SQLiteCoordinationStore
from kaiwen_agent.triggers import TriggerEvent
from kaiwen_agent.types import utc_now
from kaiwen_agent.watches import (
    WatchCondition,
    WatchContext,
    WatchCreate,
    WatchEngine,
    WatchStatus,
    WatchStrategy,
)


def objective_context(*, tenant_id: str = "tenant-1") -> ObjectiveContext:
    return ObjectiveContext(
        tenant_id=tenant_id,
        user_id="user-1",
        agent_id="operations-agent",
        conversation_id="conversation-1",
        trace_id="trace-1",
    )


def watch_context(*, tenant_id: str = "tenant-1") -> WatchContext:
    return WatchContext(**objective_context(tenant_id=tenant_id).model_dump())


def test_objective_state_machine_and_owner_scope() -> None:
    async def scenario() -> None:
        async def verify(_objective: object, evidence: dict[str, object]) -> bool:
            return evidence.get("status") == "completed"

        manager = ObjectiveManager(completion_verifier=verify)
        objective = await manager.create(
            objective_context(),
            ObjectiveCreate(
                title="Track deployment",
                description="Wait for production deployment",
                success_condition="Deployment reaches completed",
            ),
        )
        active = await manager.transition(objective_context(), objective.id, ObjectiveStatus.ACTIVE)
        waiting = await manager.transition(
            objective_context(), objective.id, ObjectiveStatus.WAITING
        )
        assert active.started_at is not None
        assert waiting.status == ObjectiveStatus.WAITING
        with pytest.raises(KeyError):
            await manager.get(objective_context(tenant_id="tenant-2"), objective.id)
        with pytest.raises(KeyError):
            await manager.get(
                objective_context().model_copy(update={"agent_id": "other-agent"}),
                objective.id,
            )
        resumed = await manager.transition(
            objective_context(), objective.id, ObjectiveStatus.ACTIVE
        )
        assert resumed.status == ObjectiveStatus.ACTIVE
        with pytest.raises(InvalidObjectiveTransition):
            await manager.complete(
                objective_context(), objective.id, evidence={"status": "running"}
            )
        completed = await manager.complete(
            objective_context(), objective.id, evidence={"status": "completed"}
        )
        assert completed.completed_at is not None
        with pytest.raises(InvalidObjectiveTransition):
            await manager.transition(objective_context(), objective.id, ObjectiveStatus.ACTIVE)

    asyncio.run(scenario())


def test_event_watch_resumes_objective_and_wakes_agent() -> None:
    async def scenario() -> None:
        lifecycle = InMemoryLifecycleStore()
        objectives = ObjectiveManager()
        objective = await objectives.create(
            objective_context(),
            ObjectiveCreate(
                title="Deployment",
                description="Wait",
                success_condition="status completed",
            ),
        )
        await objectives.transition(objective_context(), objective.id, ObjectiveStatus.ACTIVE)
        await objectives.transition(objective_context(), objective.id, ObjectiveStatus.WAITING)
        engine = WatchEngine(wakeups=WakeupManager(store=lifecycle), objectives=objectives)
        watch = await engine.create(
            watch_context(),
            WatchCreate(
                resource="deployment:123",
                condition=WatchCondition(field="status", value="completed"),
                strategy=WatchStrategy.EVENT,
                objective_id=objective.id,
                expires_at=utc_now() + timedelta(hours=1),
            ),
        )
        cross_tenant = await engine.ingest(
            TriggerEvent(
                type="deployment.changed",
                tenant_id="tenant-2",
                resource="deployment:123",
                payload={"status": "completed"},
            )
        )
        assert cross_tenant == []
        matched = await engine.ingest(
            TriggerEvent(
                id="deployment-event",
                type="deployment.changed",
                tenant_id="tenant-1",
                resource="deployment:123",
                payload={"status": "completed"},
            )
        )
        assert [item.id for item in matched] == [watch.id]
        assert (
            await objectives.get(objective_context(), objective.id)
        ).status == ObjectiveStatus.ACTIVE
        wakeups = await lifecycle.list_wakeups(
            agent_id="operations-agent", status=WakeupStatus.SCHEDULED
        )
        assert wakeups[0].trigger.metadata["watch_id"] == watch.id

    asyncio.run(scenario())


def test_polling_watch_reschedules_then_matches() -> None:
    async def scenario() -> None:
        created_at = utc_now()
        engine = WatchEngine(wakeups=WakeupManager(store=InMemoryLifecycleStore()))
        watch = await engine.create(
            watch_context(),
            WatchCreate(
                resource="job:1",
                condition=WatchCondition(field="progress", value=100),
                strategy=WatchStrategy.POLLING,
                polling_interval_seconds=30,
                expires_at=created_at + timedelta(hours=1),
            ),
        )
        now = watch.next_check_at
        assert now is not None
        values = iter([{"progress": 50}, {"progress": 100}])

        async def probe(_watch: object) -> dict[str, int]:
            return next(values)

        assert await engine.poll_due(tenant_id="tenant-1", probe=probe, now=now) == []
        stored = await engine.store.get_watch(watch.id)
        assert stored is not None and stored.next_check_at == now + timedelta(seconds=30)
        matched = await engine.poll_due(
            tenant_id="tenant-1", probe=probe, now=now + timedelta(seconds=30)
        )
        assert matched[0].status == WatchStatus.MATCHED

    asyncio.run(scenario())


def test_attention_severity_controls_wakeup_and_notification() -> None:
    async def scenario() -> None:
        lifecycle = InMemoryLifecycleStore()
        notified: list[str] = []

        async def notify(item: object) -> None:
            notified.append(item.id)  # type: ignore[attr-defined]

        engine = AttentionEngine(wakeups=WakeupManager(store=lifecycle), notifier=notify)
        context = AttentionContext(**objective_context().model_dump())
        info = await engine.submit(
            context,
            AttentionRequest(
                severity=AttentionSeverity.INFO,
                title="FYI",
                summary="Informational",
                source="test",
            ),
        )
        warning = await engine.submit(
            context,
            AttentionRequest(
                severity=AttentionSeverity.WARNING,
                title="Watch later",
                summary="Include next cycle",
                source="test",
            ),
        )
        critical = await engine.submit(
            context,
            AttentionRequest(
                severity=AttentionSeverity.CRITICAL,
                title="Act now",
                summary="Critical",
                source="test",
            ),
        )
        assert info.wakeup_id is None
        assert warning.include_next_cycle is True and warning.wakeup_id is None
        assert critical.wakeup_id is not None
        assert notified == [critical.id]

    asyncio.run(scenario())


def test_sqlite_coordination_store_survives_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "coordination.db"
        store = SQLiteCoordinationStore(path)
        objectives = ObjectiveManager(store=store)
        objective = await objectives.create(
            objective_context(),
            ObjectiveCreate(
                title="Persistent",
                description="Persist objective",
                success_condition="Stored",
            ),
        )
        restarted = SQLiteCoordinationStore(path)
        loaded = await restarted.get_objective(objective.id)
        assert loaded is not None and loaded.title == "Persistent"

        watch_engine = WatchEngine(
            store=store, wakeups=WakeupManager(store=InMemoryLifecycleStore())
        )
        watch = await watch_engine.create(
            watch_context(),
            WatchCreate(
                resource="deployment:persistent",
                condition=WatchCondition(field="status", value="done"),
                strategy=WatchStrategy.EVENT,
                expires_at=utc_now() + timedelta(hours=1),
            ),
        )
        loaded_watch = await restarted.get_watch(watch.id)
        assert loaded_watch is not None and loaded_watch.resource == watch.resource

        attention = AttentionEngine(
            store=store, wakeups=WakeupManager(store=InMemoryLifecycleStore())
        )
        item = await attention.submit(
            AttentionContext(**objective_context().model_dump()),
            AttentionRequest(
                severity=AttentionSeverity.INFO,
                title="Persistent attention",
                summary="Stored",
                source="test",
            ),
        )
        loaded_attention = await restarted.list_attention(tenant_id="tenant-1", user_id="user-1")
        assert [entry.id for entry in loaded_attention] == [item.id]

    asyncio.run(scenario())

import asyncio
from datetime import timedelta

import pytest

from kaiwen_agent.events import InMemoryEventSink
from kaiwen_agent.lifecycle import (
    InMemoryLifecycleStore,
    InvalidLifecycleTransition,
    LifecycleCycleStatus,
    LifecycleObservationBatch,
    LifecyclePausedError,
    LifecycleRuntime,
    LifecycleSchedulerAdapter,
    LifecycleState,
    WakeTrigger,
    WakeTriggerType,
    WakeupManager,
    WakeupStatus,
    validate_lifecycle_transition,
)
from kaiwen_agent.persistence import SQLitePersistence
from kaiwen_agent.types import utc_now


def test_lifecycle_transition_table_rejects_invalid_jump() -> None:
    with pytest.raises(InvalidLifecycleTransition):
        validate_lifecycle_transition(LifecycleState.SLEEPING, LifecycleState.ACTING)


def test_manual_wake_runs_deterministic_cycle_without_reasoning() -> None:
    async def scenario() -> None:
        store = InMemoryLifecycleStore()
        events = InMemoryEventSink()
        runtime = LifecycleRuntime(agent_id="admin-agent", store=store, events=events)
        cycle = await runtime.manual_wake(reason="operator requested status")
        assert cycle.status == LifecycleCycleStatus.COMPLETED
        assert cycle.reasoning_used is False
        assert cycle.state == LifecycleState.SLEEPING
        assert (await runtime.status()).state == LifecycleState.SLEEPING
        assert [event.type for event in events.events] == [
            "lifecycle.wakeup.scheduled",
            "lifecycle.cycle.started",
            "lifecycle.cycle.observing",
            "lifecycle.cycle.persisting",
            "lifecycle.cycle.scheduling",
            "lifecycle.cycle.sleeping",
            "lifecycle.cycle.completed",
        ]

    asyncio.run(scenario())


def test_scheduler_adapter_claims_only_due_wakeup() -> None:
    async def scenario() -> None:
        store = InMemoryLifecycleStore()
        runtime = LifecycleRuntime(agent_id="agent", store=store)
        await runtime.start()
        await runtime.wakeups.schedule(
            agent_id="agent",
            trigger=WakeTrigger(type=WakeTriggerType.SCHEDULE, reason="future check"),
            scheduled_at=utc_now() + timedelta(hours=1),
        )
        assert await LifecycleSchedulerAdapter(runtime).tick() is None
        await runtime.wakeups.schedule(
            agent_id="agent",
            trigger=WakeTrigger(type=WakeTriggerType.SCHEDULE, reason="due check"),
            scheduled_at=utc_now() - timedelta(seconds=1),
            priority=1,
        )
        cycle = await LifecycleSchedulerAdapter(runtime).tick()
        assert cycle is not None
        assert cycle.trigger.reason == "due check"

    asyncio.run(scenario())


def test_replace_key_cancels_previous_scheduled_wakeup() -> None:
    async def scenario() -> None:
        store = InMemoryLifecycleStore()
        manager = WakeupManager(store=store)
        first = await manager.schedule(
            agent_id="agent",
            trigger=WakeTrigger(type=WakeTriggerType.SCHEDULE, reason="first"),
            scheduled_at=utc_now() + timedelta(minutes=10),
            replace_key="agent:health",
        )
        second = await manager.schedule(
            agent_id="agent",
            trigger=WakeTrigger(type=WakeTriggerType.SCHEDULE, reason="replacement"),
            scheduled_at=utc_now() + timedelta(minutes=20),
            replace_key="agent:health",
        )
        stored_first = await store.get_wakeup(first.id)
        stored_second = await store.get_wakeup(second.id)
        assert stored_first is not None
        assert stored_first.status == WakeupStatus.CANCELLED
        assert stored_second is not None
        assert stored_second.status == WakeupStatus.SCHEDULED

    asyncio.run(scenario())


def test_pause_keeps_due_wakeup_unclaimed_until_resume() -> None:
    async def scenario() -> None:
        store = InMemoryLifecycleStore()
        runtime = LifecycleRuntime(agent_id="agent", store=store)
        await runtime.start()
        wakeup = await runtime.wakeups.schedule(
            agent_id="agent",
            trigger=WakeTrigger(type=WakeTriggerType.SCHEDULE, reason="due"),
            scheduled_at=utc_now() - timedelta(seconds=1),
        )
        await runtime.pause()
        assert await runtime.run_next_due() is None
        stored = await store.get_wakeup(wakeup.id)
        assert stored is not None and stored.status == WakeupStatus.SCHEDULED
        await runtime.resume()
        assert await runtime.run_next_due() is not None

    asyncio.run(scenario())


def test_manual_wake_while_paused_remains_queued() -> None:
    async def scenario() -> None:
        store = InMemoryLifecycleStore()
        runtime = LifecycleRuntime(agent_id="agent", store=store)
        await runtime.start()
        await runtime.pause()
        with pytest.raises(LifecyclePausedError):
            await runtime.manual_wake(reason="queued while paused")
        wakeups = await store.list_wakeups(agent_id="agent", status=WakeupStatus.SCHEDULED)
        assert len(wakeups) == 1
        await runtime.resume()
        cycle = await runtime.run_next_due()
        assert cycle is not None
        assert cycle.trigger.reason == "queued while paused"

    asyncio.run(scenario())


def test_reasoning_request_fails_closed_in_skeleton() -> None:
    class ReasoningObserver:
        async def observe(self, trigger):
            del trigger
            return LifecycleObservationBatch(requires_reasoning=True)

    async def scenario() -> None:
        store = InMemoryLifecycleStore()
        runtime = LifecycleRuntime(agent_id="agent", store=store, observer=ReasoningObserver())
        with pytest.raises(RuntimeError, match="Reasoning is not part"):
            await runtime.manual_wake(reason="needs reasoning")
        assert (await runtime.status()).state == LifecycleState.FAILED

    asyncio.run(scenario())


def test_sqlite_lifecycle_state_and_atomic_claim_survive_restart(tmp_path) -> None:
    database = tmp_path / "lifecycle.sqlite3"

    async def scenario() -> None:
        first = SQLitePersistence(database)
        manager = WakeupManager(store=first)
        wakeup = await manager.schedule(
            agent_id="agent",
            trigger=WakeTrigger(type=WakeTriggerType.SCHEDULE, reason="durable"),
            scheduled_at=utc_now() - timedelta(seconds=1),
        )
        left = SQLitePersistence(database)
        right = SQLitePersistence(database)
        claims = await asyncio.gather(
            left.claim_next_wakeup(agent_id="agent", due_before=utc_now()),
            right.claim_next_wakeup(agent_id="agent", due_before=utc_now()),
        )
        assert sum(claim is not None for claim in claims) == 1
        restarted = SQLitePersistence(database)
        stored = await restarted.get_wakeup(wakeup.id)
        assert stored is not None and stored.status == WakeupStatus.CLAIMED

    asyncio.run(scenario())


def test_sqlite_completed_cycle_and_runtime_status_survive_restart(tmp_path) -> None:
    database = tmp_path / "runtime.sqlite3"

    async def scenario() -> None:
        runtime = LifecycleRuntime(agent_id="admin-agent", store=SQLitePersistence(database))
        cycle = await runtime.manual_wake(reason="durable cycle")
        restarted = SQLitePersistence(database)
        runtime_record = await restarted.get_runtime("admin-agent")
        stored_cycle = await restarted.get_cycle(cycle.id)
        assert runtime_record is not None
        assert runtime_record.state == LifecycleState.SLEEPING
        assert runtime_record.last_cycle_id == cycle.id
        assert stored_cycle is not None
        assert stored_cycle.status == LifecycleCycleStatus.COMPLETED

    asyncio.run(scenario())

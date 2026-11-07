from __future__ import annotations

import asyncio
from datetime import timedelta

from kaiwen_agent.events import InMemoryEventSink
from kaiwen_agent.lifecycle import InMemoryLifecycleStore, WakeupManager, WakeupStatus
from kaiwen_agent.triggers import TriggerEvent, TriggerRouter, TriggerRule
from kaiwen_agent.types import utc_now


def test_trigger_router_filters_deduplicates_and_coalesces() -> None:
    async def scenario() -> None:
        lifecycle = InMemoryLifecycleStore()
        events = InMemoryEventSink()
        router = TriggerRouter(
            rules=[
                TriggerRule(
                    id="service-health",
                    event_type="service.health.changed",
                    agent_id="operations-agent",
                    tenant_id="tenant-1",
                    payload_equals={"status": "down"},
                    debounce_seconds=30,
                    coalesce_seconds=60,
                    priority=75,
                )
            ],
            wakeups=WakeupManager(store=lifecycle, events=events),
            events=events,
        )
        start = utc_now()
        ignored = await router.route(
            TriggerEvent(
                id="evt-ignored",
                type="service.health.changed",
                tenant_id="tenant-1",
                payload={"status": "healthy"},
                occurred_at=start,
            ),
            received_at=start,
        )
        assert ignored.matched_rule_ids == []

        first_event = TriggerEvent(
            id="evt-1",
            type="service.health.changed",
            tenant_id="tenant-1",
            user_id="user-1",
            conversation_id="conversation-1",
            payload={"status": "down", "secret": "must-not-reach-wakeup"},
            occurred_at=start,
        )
        first = await router.route(first_event, received_at=start)
        duplicate = await router.route(first_event, received_at=start)
        second = await router.route(
            first_event.model_copy(
                update={"id": "evt-2", "occurred_at": start + timedelta(seconds=10)}
            ),
            received_at=start + timedelta(seconds=10),
        )
        assert first.matched_rule_ids == ["service-health"]
        assert duplicate.duplicate is True
        assert second.pending_batch_keys == first.pending_batch_keys
        assert await router.flush_due(now=start + timedelta(seconds=39)) == 0
        assert await router.flush_due(now=start + timedelta(seconds=40)) == 1

        wakeups = await lifecycle.list_wakeups(
            agent_id="operations-agent", status=WakeupStatus.SCHEDULED
        )
        assert len(wakeups) == 1
        assert wakeups[0].trigger.metadata["event_count"] == 2
        assert wakeups[0].trigger.metadata["event_ids"] == ["evt-1", "evt-2"]
        assert "secret" not in wakeups[0].trigger.metadata
        assert [event.type for event in events.events].count("trigger.event.deduplicated") == 1

    asyncio.run(scenario())


def test_trigger_router_scopes_rules_by_tenant() -> None:
    async def scenario() -> None:
        router = TriggerRouter(
            rules=[
                TriggerRule(
                    id="tenant-only",
                    event_type="deployment.changed",
                    agent_id="operations-agent",
                    tenant_id="tenant-1",
                )
            ],
            wakeups=WakeupManager(store=InMemoryLifecycleStore()),
        )
        result = await router.route(TriggerEvent(type="deployment.changed", tenant_id="tenant-2"))
        assert result.matched_rule_ids == []

    asyncio.run(scenario())

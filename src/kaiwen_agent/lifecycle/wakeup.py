from __future__ import annotations

from datetime import UTC, datetime

from kaiwen_agent.events import AgentEvent, EventSink, EventType, InMemoryEventSink
from kaiwen_agent.lifecycle.models import (
    LifecycleWakeup,
    WakeTrigger,
    WakeTriggerType,
    WakeupStatus,
)
from kaiwen_agent.lifecycle.store import LifecycleStore
from kaiwen_agent.types import utc_now


class WakeupManager:
    """Persists wakeups; it does not own a timer loop or call a model."""

    def __init__(
        self,
        *,
        store: LifecycleStore,
        events: EventSink | None = None,
    ) -> None:
        self.store = store
        self.events = events or InMemoryEventSink()

    async def schedule(
        self,
        *,
        agent_id: str,
        trigger: WakeTrigger,
        scheduled_at: datetime,
        replace_key: str | None = None,
        priority: int = 0,
    ) -> LifecycleWakeup:
        if scheduled_at.tzinfo is None:
            raise ValueError("scheduled_at must be timezone-aware")
        scheduled_at = scheduled_at.astimezone(UTC)
        wakeup = LifecycleWakeup(
            agent_id=agent_id,
            trigger=trigger,
            scheduled_at=scheduled_at,
            replace_key=replace_key,
            priority=priority,
        )
        cancelled = await self.store.schedule_wakeup(wakeup)
        for replaced in cancelled:
            await self._emit("lifecycle.wakeup.cancelled", replaced)
        await self._emit("lifecycle.wakeup.scheduled", wakeup)
        return wakeup

    async def schedule_manual(
        self,
        *,
        agent_id: str,
        reason: str,
        tenant_id: str | None = None,
        user_id: str | None = None,
        conversation_id: str | None = None,
    ) -> LifecycleWakeup:
        return await self.schedule(
            agent_id=agent_id,
            trigger=WakeTrigger(
                type=WakeTriggerType.MANUAL,
                reason=reason,
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
            ),
            scheduled_at=utc_now(),
            priority=100,
        )

    async def claim_due(
        self, *, agent_id: str, now: datetime | None = None
    ) -> LifecycleWakeup | None:
        return await self.store.claim_next_wakeup(agent_id=agent_id, due_before=now or utc_now())

    async def complete(self, wakeup_id: str, *, cycle_id: str) -> LifecycleWakeup:
        wakeup = await self.store.get_wakeup(wakeup_id)
        if wakeup is None:
            raise KeyError(f"Unknown wakeup: {wakeup_id}")
        if wakeup.status != WakeupStatus.CLAIMED:
            raise ValueError("Only a claimed wakeup can be completed")
        wakeup.status = WakeupStatus.COMPLETED
        wakeup.cycle_id = cycle_id
        wakeup.completed_at = utc_now()
        wakeup.updated_at = wakeup.completed_at
        await self.store.save_wakeup(wakeup)
        return wakeup

    async def fail(self, wakeup_id: str, *, cycle_id: str) -> LifecycleWakeup:
        wakeup = await self.store.get_wakeup(wakeup_id)
        if wakeup is None:
            raise KeyError(f"Unknown wakeup: {wakeup_id}")
        if wakeup.status != WakeupStatus.CLAIMED:
            raise ValueError("Only a claimed wakeup can fail")
        wakeup.status = WakeupStatus.FAILED
        wakeup.cycle_id = cycle_id
        wakeup.completed_at = utc_now()
        wakeup.updated_at = wakeup.completed_at
        await self.store.save_wakeup(wakeup)
        return wakeup

    async def next_scheduled(self, *, agent_id: str) -> LifecycleWakeup | None:
        wakeups = await self.store.list_wakeups(
            agent_id=agent_id, status=WakeupStatus.SCHEDULED, limit=1
        )
        return wakeups[0] if wakeups else None

    async def _emit(self, event_type: EventType, wakeup: LifecycleWakeup) -> None:
        await self.events.append(
            AgentEvent(
                type=event_type,
                trace_id=wakeup.trigger.metadata.get("trace_id", wakeup.id),
                session_id=f"lifecycle:{wakeup.agent_id}",
                payload={
                    "agent_id": wakeup.agent_id,
                    "wakeup_id": wakeup.id,
                    "trigger_type": wakeup.trigger.type.value,
                    "scheduled_at": wakeup.scheduled_at.isoformat(),
                    "replace_key": wakeup.replace_key,
                },
            )
        )

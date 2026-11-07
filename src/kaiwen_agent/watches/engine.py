from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from kaiwen_agent.events import AgentEvent, EventSink, EventType, InMemoryEventSink
from kaiwen_agent.lifecycle import WakeTrigger, WakeTriggerType, WakeupManager
from kaiwen_agent.objectives import ObjectiveContext, ObjectiveManager, ObjectiveStatus
from kaiwen_agent.triggers import TriggerEvent
from kaiwen_agent.types import utc_now
from kaiwen_agent.watches.models import (
    Watch,
    WatchContext,
    WatchCreate,
    WatchOperator,
    WatchStatus,
    WatchStrategy,
)
from kaiwen_agent.watches.store import InMemoryWatchStore, WatchStore

WatchProbe = Callable[[Watch], Awaitable[dict[str, Any]]]


class WatchEngine:
    def __init__(
        self,
        *,
        wakeups: WakeupManager,
        store: WatchStore | None = None,
        objectives: ObjectiveManager | None = None,
        events: EventSink | None = None,
    ) -> None:
        self.wakeups = wakeups
        self.store = store or InMemoryWatchStore()
        self.objectives = objectives
        self.events = events or InMemoryEventSink()
        self._lock = asyncio.Lock()

    async def create(self, context: WatchContext, request: WatchCreate) -> Watch:
        now = utc_now()
        if request.expires_at <= now:
            raise ValueError("Watch expiry must be in the future")
        if request.objective_id is not None:
            if self.objectives is None:
                raise ValueError("Objective manager is required for objective watches")
            objective = await self.objectives.get(_objective_context(context), request.objective_id)
            if objective.status != ObjectiveStatus.WAITING:
                raise ValueError("Objective must be waiting before a watch is created")
        watch = Watch(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            conversation_id=context.conversation_id,
            agent_id=context.agent_id,
            objective_id=request.objective_id,
            job_id=request.job_id,
            resource=request.resource,
            condition=request.condition,
            strategy=request.strategy,
            polling_interval_seconds=request.polling_interval_seconds,
            next_check_at=now if request.strategy == WatchStrategy.POLLING else None,
            expires_at=request.expires_at,
        )
        await self.store.save_watch(watch)
        await self._emit("watch.created", context.trace_id, watch)
        return watch

    async def cancel(self, context: WatchContext, watch_id: str) -> Watch:
        watch = await self._owned(context, watch_id)
        if watch.status != WatchStatus.ACTIVE:
            return watch
        updated = watch.model_copy(
            update={"status": WatchStatus.CANCELLED, "updated_at": utc_now()}
        )
        await self.store.save_watch(updated)
        await self._emit("watch.cancelled", context.trace_id, updated)
        return updated

    async def ingest(
        self,
        event: TriggerEvent,
        *,
        strategy: WatchStrategy = WatchStrategy.EVENT,
        received_at: datetime | None = None,
    ) -> list[Watch]:
        now = received_at or utc_now()
        if event.tenant_id is None or event.resource is None:
            return []
        async with self._lock:
            watches = await self.store.list_watches(
                tenant_id=event.tenant_id,
                user_id=event.user_id,
                resource=event.resource,
                strategy=strategy,
                status=WatchStatus.ACTIVE,
                limit=1000,
            )
            return await self._evaluate(watches, event.payload, now, event.id)

    async def poll_due(
        self,
        *,
        tenant_id: str,
        probe: WatchProbe,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[Watch]:
        checked_at = now or utc_now()
        async with self._lock:
            watches = await self.store.list_watches(
                tenant_id=tenant_id,
                strategy=WatchStrategy.POLLING,
                status=WatchStatus.ACTIVE,
                due_before=checked_at,
                limit=limit,
            )
            matched: list[Watch] = []
            for watch in watches:
                if watch.expires_at <= checked_at:
                    await self._expire(watch, f"poll:{watch.id}")
                    continue
                payload = await probe(watch)
                result = await self._evaluate([watch], payload, checked_at, f"poll:{watch.id}")
                matched.extend(result)
                if not result:
                    interval = watch.polling_interval_seconds
                    if interval is None:
                        raise RuntimeError("Polling watch is missing its interval")
                    updated = watch.model_copy(
                        update={
                            "next_check_at": checked_at + timedelta(seconds=interval),
                            "updated_at": checked_at,
                        }
                    )
                    await self.store.save_watch(updated)
            return matched

    async def _evaluate(
        self,
        watches: list[Watch],
        payload: dict[str, Any],
        now: datetime,
        trace_id: str,
    ) -> list[Watch]:
        matched: list[Watch] = []
        for watch in watches:
            if watch.expires_at <= now:
                await self._expire(watch, trace_id)
                continue
            if not _condition_matches(watch, payload):
                continue
            updated = watch.model_copy(
                update={
                    "status": WatchStatus.MATCHED,
                    "matched_at": now,
                    "updated_at": now,
                    "next_check_at": None,
                }
            )
            if updated.objective_id is not None and self.objectives is not None:
                await self.objectives.transition(
                    ObjectiveContext(
                        tenant_id=updated.tenant_id,
                        user_id=updated.user_id,
                        agent_id=updated.agent_id,
                        conversation_id=updated.conversation_id,
                        trace_id=trace_id,
                    ),
                    updated.objective_id,
                    ObjectiveStatus.ACTIVE,
                )
            await self.wakeups.schedule(
                agent_id=updated.agent_id,
                trigger=WakeTrigger(
                    type=WakeTriggerType.WATCH_MATCHED,
                    reason=f"Watch matched for {updated.resource}",
                    tenant_id=updated.tenant_id,
                    user_id=updated.user_id,
                    conversation_id=updated.conversation_id,
                    metadata={
                        "watch_id": updated.id,
                        "objective_id": updated.objective_id,
                        "job_id": updated.job_id,
                    },
                ),
                scheduled_at=now,
                replace_key=f"watch:{updated.id}",
                priority=100,
            )
            await self.store.save_watch(updated)
            await self._emit("watch.matched", trace_id, updated)
            matched.append(updated)
        return matched

    async def _expire(self, watch: Watch, trace_id: str) -> Watch:
        expired = watch.model_copy(update={"status": WatchStatus.EXPIRED, "updated_at": utc_now()})
        await self.store.save_watch(expired)
        await self._emit("watch.expired", trace_id, expired)
        return expired

    async def _owned(self, context: WatchContext, watch_id: str) -> Watch:
        watch = await self.store.get_watch(watch_id)
        if (
            watch is None
            or watch.tenant_id != context.tenant_id
            or watch.user_id != context.user_id
            or watch.agent_id != context.agent_id
        ):
            raise KeyError("Watch not found in trusted owner scope")
        return watch

    async def _emit(self, event_type: EventType, trace_id: str, watch: Watch) -> None:
        await self.events.append(
            AgentEvent(
                type=event_type,
                trace_id=trace_id,
                session_id=watch.conversation_id or f"watch:{watch.user_id}",
                tenant_id=watch.tenant_id,
                user_id=watch.user_id,
                payload={
                    "watch_id": watch.id,
                    "status": watch.status.value,
                    "objective_id": watch.objective_id,
                },
            )
        )


def _objective_context(context: WatchContext) -> ObjectiveContext:
    return ObjectiveContext(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        agent_id=context.agent_id,
        conversation_id=context.conversation_id,
        trace_id=context.trace_id,
    )


def _condition_matches(watch: Watch, payload: dict[str, Any]) -> bool:
    actual = _value_at(payload, watch.condition.field)
    expected = watch.condition.value
    operator = watch.condition.operator
    if actual is _MISSING:
        return False
    if operator == WatchOperator.EQUALS:
        return bool(actual == expected)
    if operator == WatchOperator.NOT_EQUALS:
        return bool(actual != expected)
    if operator == WatchOperator.IN:
        return isinstance(expected, list | tuple) and actual in expected
    if operator == WatchOperator.CONTAINS:
        if isinstance(actual, str):
            return isinstance(expected, str) and expected in actual
        if isinstance(actual, list | tuple):
            return expected in actual
        if isinstance(actual, dict):
            return isinstance(expected, str) and expected in actual
        return False
    if operator == WatchOperator.GREATER_THAN_OR_EQUAL:
        return _ordered(actual, expected, greater=True)
    if operator == WatchOperator.LESS_THAN_OR_EQUAL:
        return _ordered(actual, expected, greater=False)
    return False


def _ordered(actual: Any, expected: Any, *, greater: bool) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return False
    if not isinstance(actual, int | float) or not isinstance(expected, int | float):
        return False
    return actual >= expected if greater else actual <= expected


def _value_at(payload: dict[str, Any], path: str) -> Any:
    value: Any = payload
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


_MISSING = object()

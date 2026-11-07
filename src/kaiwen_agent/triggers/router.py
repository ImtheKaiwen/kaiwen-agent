from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from kaiwen_agent.events import AgentEvent, EventSink, EventType, InMemoryEventSink
from kaiwen_agent.lifecycle import WakeTrigger, WakeTriggerType, WakeupManager
from kaiwen_agent.triggers.models import (
    TriggerBatch,
    TriggerEvent,
    TriggerRouteResult,
    TriggerRule,
)
from kaiwen_agent.triggers.store import InMemoryTriggerStore, TriggerStore
from kaiwen_agent.types import utc_now


class TriggerRouter:
    """Deterministic filter/dedupe/debounce/coalescing before lifecycle wakeups."""

    def __init__(
        self,
        *,
        rules: list[TriggerRule],
        wakeups: WakeupManager,
        store: TriggerStore | None = None,
        events: EventSink | None = None,
    ) -> None:
        ids = [rule.id for rule in rules]
        if len(ids) != len(set(ids)):
            raise ValueError("Trigger rule ids must be unique")
        self.rules = tuple(rules)
        self.wakeups = wakeups
        self.store = store or InMemoryTriggerStore()
        self.events = events or InMemoryEventSink()
        self._route_lock = asyncio.Lock()

    async def route(
        self, event: TriggerEvent, *, received_at: datetime | None = None
    ) -> TriggerRouteResult:
        async with self._route_lock:
            accepted_at = received_at or utc_now()
            if accepted_at.tzinfo is None:
                raise ValueError("received_at must be timezone-aware")
            matching = [rule for rule in self.rules if _matches(rule, event)]
            if not matching:
                return TriggerRouteResult(event_id=event.id)
            longest_ttl = max(rule.dedupe_ttl_seconds for rule in matching)
            is_new = await self.store.mark_event_once(
                event.id,
                expires_at=accepted_at + timedelta(seconds=longest_ttl),
                now=accepted_at,
            )
            if not is_new:
                await self._emit("trigger.event.deduplicated", event, {})
                return TriggerRouteResult(event_id=event.id, duplicate=True)

            batch_keys: list[str] = []
            for rule in matching:
                key = _batch_key(rule, event)
                existing = await self.store.get_batch(key)
                debounce_due = accepted_at + timedelta(seconds=rule.debounce_seconds)
                if existing is None:
                    window_ends = accepted_at + timedelta(
                        seconds=max(rule.coalesce_seconds, rule.debounce_seconds)
                    )
                    batch = TriggerBatch(
                        key=key,
                        rule_id=rule.id,
                        agent_id=rule.agent_id,
                        tenant_id=event.tenant_id,
                        user_id=event.user_id,
                        conversation_id=event.conversation_id,
                        event_ids=[event.id],
                        event_types=[event.type],
                        first_event_at=accepted_at,
                        last_event_at=accepted_at,
                        due_at=min(debounce_due, window_ends),
                        window_ends_at=window_ends,
                        priority=rule.priority,
                    )
                else:
                    batch = existing.model_copy(
                        update={
                            "event_ids": [*existing.event_ids, event.id],
                            "event_types": sorted({*existing.event_types, event.type}),
                            "last_event_at": accepted_at,
                            "due_at": min(debounce_due, existing.window_ends_at),
                        }
                    )
                await self.store.save_batch(batch)
                batch_keys.append(key)
            await self._emit(
                "trigger.event.accepted",
                event,
                {"matched_rule_ids": [rule.id for rule in matching]},
            )
            return TriggerRouteResult(
                event_id=event.id,
                matched_rule_ids=[rule.id for rule in matching],
                pending_batch_keys=batch_keys,
            )

    async def flush_due(self, *, now: datetime | None = None, limit: int = 100) -> int:
        due_before = now or utc_now()
        batches = await self.store.claim_due(due_before=due_before, limit=limit)
        delivered = 0
        for batch in batches:
            try:
                wakeup = await self.wakeups.schedule(
                    agent_id=batch.agent_id,
                    trigger=WakeTrigger(
                        type=WakeTriggerType.EVENT,
                        reason=_batch_reason(batch),
                        tenant_id=batch.tenant_id,
                        user_id=batch.user_id,
                        conversation_id=batch.conversation_id,
                        metadata={
                            "trigger_rule_id": batch.rule_id,
                            "event_count": len(batch.event_ids),
                            "event_ids": batch.event_ids,
                            "event_types": batch.event_types,
                        },
                    ),
                    scheduled_at=due_before,
                    replace_key=f"trigger:{batch.key}",
                    priority=batch.priority,
                )
            except Exception:
                await self.store.save_batch(batch)
                raise
            await self.events.append(
                AgentEvent(
                    type="trigger.wakeup.scheduled",
                    trace_id=wakeup.id,
                    session_id=f"lifecycle:{batch.agent_id}",
                    tenant_id=batch.tenant_id,
                    user_id=batch.user_id,
                    payload={
                        "rule_id": batch.rule_id,
                        "event_count": len(batch.event_ids),
                        "wakeup_id": wakeup.id,
                    },
                )
            )
            delivered += 1
        return delivered

    async def _emit(
        self,
        event_type: EventType,
        event: TriggerEvent,
        payload: dict[str, object],
    ) -> None:
        await self.events.append(
            AgentEvent(
                type=event_type,
                trace_id=event.id,
                session_id=event.conversation_id or f"trigger:{event.tenant_id or 'global'}",
                tenant_id=event.tenant_id,
                user_id=event.user_id,
                payload={
                    "trigger_event_id": event.id,
                    "event_type": event.type,
                    **payload,
                },
            )
        )


def _matches(rule: TriggerRule, event: TriggerEvent) -> bool:
    if rule.event_type != event.type:
        return False
    if rule.tenant_id is not None and rule.tenant_id != event.tenant_id:
        return False
    return all(
        _payload_value(event.payload, path) == expected
        for path, expected in rule.payload_equals.items()
    )


def _payload_value(payload: dict[str, Any], path: str) -> Any:
    value: Any = payload
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _batch_key(rule: TriggerRule, event: TriggerEvent) -> str:
    identity = json.dumps(
        [rule.id, event.tenant_id, event.user_id, event.conversation_id],
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def _batch_reason(batch: TriggerBatch) -> str:
    count = len(batch.event_ids)
    kinds = ", ".join(batch.event_types)
    suffix = "s" if count != 1 else ""
    return f"{count} relevant event{suffix}: {kinds}"


_MISSING = object()

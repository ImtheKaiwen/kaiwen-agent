from __future__ import annotations

from collections.abc import Awaitable, Callable

from kaiwen_agent.attention.models import (
    AttentionContext,
    AttentionItem,
    AttentionRequest,
    AttentionSeverity,
)
from kaiwen_agent.attention.store import AttentionStore, InMemoryAttentionStore
from kaiwen_agent.events import AgentEvent, EventSink, InMemoryEventSink
from kaiwen_agent.lifecycle import WakeTrigger, WakeTriggerType, WakeupManager
from kaiwen_agent.types import utc_now

AttentionNotifier = Callable[[AttentionItem], Awaitable[None]]


class AttentionEngine:
    def __init__(
        self,
        *,
        wakeups: WakeupManager,
        store: AttentionStore | None = None,
        notifier: AttentionNotifier | None = None,
        events: EventSink | None = None,
    ) -> None:
        self.wakeups = wakeups
        self.store = store or InMemoryAttentionStore()
        self.notifier = notifier
        self.events = events or InMemoryEventSink()

    async def submit(self, context: AttentionContext, request: AttentionRequest) -> AttentionItem:
        include_next_cycle = request.severity == AttentionSeverity.WARNING
        item = AttentionItem(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            conversation_id=context.conversation_id,
            agent_id=context.agent_id,
            severity=request.severity,
            title=request.title,
            summary=request.summary,
            source=request.source,
            resource=request.resource,
            include_next_cycle=include_next_cycle,
        )
        if request.severity in {
            AttentionSeverity.ACTION_REQUIRED,
            AttentionSeverity.CRITICAL,
        }:
            wakeup = await self.wakeups.schedule(
                agent_id=context.agent_id,
                trigger=WakeTrigger(
                    type=WakeTriggerType.SYSTEM_ALERT,
                    reason=request.title,
                    tenant_id=context.tenant_id,
                    user_id=context.user_id,
                    conversation_id=context.conversation_id,
                    metadata={
                        "attention_id": item.id,
                        "severity": item.severity.value,
                        "source": item.source,
                    },
                ),
                scheduled_at=utc_now(),
                replace_key=f"attention:{item.id}",
                priority=100 if request.severity == AttentionSeverity.CRITICAL else 80,
            )
            item = item.model_copy(update={"wakeup_id": wakeup.id})
        await self.store.save_attention(item)
        if request.severity == AttentionSeverity.CRITICAL and self.notifier is not None:
            await self.notifier(item)
            item = item.model_copy(update={"notification_requested": True})
            await self.store.save_attention(item)
        await self.events.append(
            AgentEvent(
                type="attention.created",
                trace_id=context.trace_id,
                session_id=context.conversation_id or f"attention:{context.user_id}",
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                payload={
                    "attention_id": item.id,
                    "severity": item.severity.value,
                    "wakeup_id": item.wakeup_id,
                    "notification_requested": item.notification_requested,
                },
            )
        )
        return item

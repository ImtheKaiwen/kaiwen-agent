from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from kaiwen_agent.events import AgentEvent, EventSink, EventType, InMemoryEventSink
from kaiwen_agent.objectives.models import (
    TERMINAL_OBJECTIVE_STATUSES,
    Objective,
    ObjectiveContext,
    ObjectiveCreate,
    ObjectiveStatus,
)
from kaiwen_agent.objectives.store import InMemoryObjectiveStore, ObjectiveStore
from kaiwen_agent.types import utc_now

_TRANSITIONS = {
    ObjectiveStatus.PENDING: {ObjectiveStatus.ACTIVE, ObjectiveStatus.CANCELLED},
    ObjectiveStatus.ACTIVE: {
        ObjectiveStatus.WAITING,
        ObjectiveStatus.BLOCKED,
        ObjectiveStatus.COMPLETED,
        ObjectiveStatus.FAILED,
        ObjectiveStatus.CANCELLED,
    },
    ObjectiveStatus.WAITING: {
        ObjectiveStatus.ACTIVE,
        ObjectiveStatus.BLOCKED,
        ObjectiveStatus.FAILED,
        ObjectiveStatus.CANCELLED,
    },
    ObjectiveStatus.BLOCKED: {
        ObjectiveStatus.ACTIVE,
        ObjectiveStatus.FAILED,
        ObjectiveStatus.CANCELLED,
    },
    ObjectiveStatus.COMPLETED: set(),
    ObjectiveStatus.FAILED: set(),
    ObjectiveStatus.CANCELLED: set(),
}


class InvalidObjectiveTransition(ValueError):
    pass


ObjectiveCompletionVerifier = Callable[[Objective, dict[str, Any]], Awaitable[bool]]


class ObjectiveManager:
    def __init__(
        self,
        *,
        store: ObjectiveStore | None = None,
        events: EventSink | None = None,
        completion_verifier: ObjectiveCompletionVerifier | None = None,
    ) -> None:
        self.store = store or InMemoryObjectiveStore()
        self.events = events or InMemoryEventSink()
        self.completion_verifier = completion_verifier
        self._transition_lock = asyncio.Lock()

    async def create(self, context: ObjectiveContext, request: ObjectiveCreate) -> Objective:
        if request.parent_objective_id is not None:
            parent = await self.get(context, request.parent_objective_id)
            if parent.terminal:
                raise ValueError("Cannot add a child to a terminal objective")
        objective = Objective(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            conversation_id=context.conversation_id,
            agent_id=context.agent_id,
            title=request.title,
            description=request.description,
            success_condition=request.success_condition,
            priority=request.priority,
            parent_objective_id=request.parent_objective_id,
        )
        await self.store.save_objective(objective)
        await self._emit("objective.created", context, objective)
        return objective

    async def get(self, context: ObjectiveContext, objective_id: str) -> Objective:
        objective = await self.store.get_objective(objective_id)
        if (
            objective is None
            or objective.tenant_id != context.tenant_id
            or objective.user_id != context.user_id
            or objective.agent_id != context.agent_id
        ):
            raise KeyError("Objective not found in trusted owner scope")
        return objective

    async def transition(
        self,
        context: ObjectiveContext,
        objective_id: str,
        status: ObjectiveStatus,
    ) -> Objective:
        if status == ObjectiveStatus.COMPLETED:
            raise InvalidObjectiveTransition("Use complete() so the success condition is verified")
        return await self._transition(context, objective_id, status)

    async def complete(
        self,
        context: ObjectiveContext,
        objective_id: str,
        *,
        evidence: dict[str, Any],
    ) -> Objective:
        objective = await self.get(context, objective_id)
        if self.completion_verifier is None:
            raise RuntimeError("Objective completion verifier is not configured")
        if not await self.completion_verifier(objective, evidence):
            raise InvalidObjectiveTransition("Objective success condition is not met")
        return await self._transition(context, objective_id, ObjectiveStatus.COMPLETED)

    async def _transition(
        self,
        context: ObjectiveContext,
        objective_id: str,
        status: ObjectiveStatus,
    ) -> Objective:
        async with self._transition_lock:
            objective = await self.get(context, objective_id)
            if status == objective.status:
                return objective
            if status not in _TRANSITIONS[objective.status]:
                raise InvalidObjectiveTransition(
                    f"Cannot transition objective from {objective.status} to {status}"
                )
            now = utc_now()
            updated = objective.model_copy(
                update={
                    "status": status,
                    "started_at": objective.started_at
                    or (now if status == ObjectiveStatus.ACTIVE else None),
                    "completed_at": (now if status in TERMINAL_OBJECTIVE_STATUSES else None),
                    "updated_at": now,
                }
            )
            await self.store.save_objective(updated)
            await self._emit("objective.status.changed", context, updated)
            return updated

    async def _emit(
        self, event_type: EventType, context: ObjectiveContext, objective: Objective
    ) -> None:
        await self.events.append(
            AgentEvent(
                type=event_type,
                trace_id=context.trace_id,
                session_id=context.conversation_id or f"objective:{context.user_id}",
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                payload={
                    "objective_id": objective.id,
                    "status": objective.status.value,
                    "agent_id": objective.agent_id,
                },
            )
        )

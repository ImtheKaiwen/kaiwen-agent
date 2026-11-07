from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kaiwen_agent.audit import SecretRedactor
from kaiwen_agent.types import utc_now

_EVENT_REDACTOR = SecretRedactor()

EventType = Literal[
    "conversation.input",
    "conversation.delta",
    "conversation.completed",
    "run.started",
    "run.status",
    "run.completed",
    "run.failed",
    "tool.started",
    "tool.completed",
    "tool.failed",
    "task.created",
    "task.queued",
    "task.started",
    "task.progress",
    "task.retrying",
    "task.paused",
    "task.resumed",
    "task.completed",
    "task.failed",
    "task.cancelled",
    "job.created",
    "job.queued",
    "job.assigned",
    "job.started",
    "job.progress",
    "job.waiting",
    "job.waiting_approval",
    "job.completed",
    "job.failed",
    "job.cancel_requested",
    "job.cancelled",
    "resource.waiting",
    "resource.acquired",
    "resource.released",
    "approval.requested",
    "approval.resolved",
    "artifact.created",
    "client.connected",
    "client.capabilities",
    "client.state",
    "client_action.requested",
    "client_action.result",
    "client_action.failed",
    "lifecycle.wakeup.scheduled",
    "lifecycle.wakeup.cancelled",
    "lifecycle.cycle.started",
    "lifecycle.cycle.observing",
    "lifecycle.cycle.thinking",
    "lifecycle.cycle.acting",
    "lifecycle.cycle.persisting",
    "lifecycle.cycle.scheduling",
    "lifecycle.cycle.sleeping",
    "lifecycle.cycle.completed",
    "lifecycle.cycle.failed",
    "policy.allowed",
    "policy.denied",
    "policy.approval_required",
    "policy.review_required",
    "policy.modified",
    "trigger.event.accepted",
    "trigger.event.deduplicated",
    "trigger.wakeup.scheduled",
    "objective.created",
    "objective.status.changed",
    "watch.created",
    "watch.matched",
    "watch.expired",
    "watch.cancelled",
    "attention.created",
]


class AgentEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    id: str = Field(default_factory=lambda: f"evt_{uuid4().hex}")
    type: EventType
    timestamp: datetime = Field(default_factory=utc_now)
    trace_id: str
    session_id: str
    tenant_id: str | None = Field(default=None, min_length=1, max_length=200)
    user_id: str | None = Field(default=None, min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload", mode="after")
    @classmethod
    def redact_secrets(cls, payload: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], _EVENT_REDACTOR.redact(payload))


def redact_agent_event(event: AgentEvent) -> AgentEvent:
    """Return a detached event with its current payload redacted."""
    return event.model_copy(
        deep=True,
        update={"payload": _EVENT_REDACTOR.redact(event.payload)},
    )


class EventSink(Protocol):
    async def append(self, event: AgentEvent) -> None: ...


class EventStore(EventSink, Protocol):
    async def list_events(
        self,
        *,
        session_id: str,
        after_id: str | None = None,
        limit: int = 100,
    ) -> list[AgentEvent]: ...


class EventScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)


class ScopedEventStore(EventStore, Protocol):
    async def list_events_scoped(
        self,
        *,
        scope: EventScope,
        after_id: str | None = None,
        limit: int = 100,
    ) -> list[AgentEvent]: ...


class InMemoryEventSink:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    async def append(self, event: AgentEvent) -> None:
        self.events.append(redact_agent_event(event))

    async def list_events(
        self,
        *,
        session_id: str,
        after_id: str | None = None,
        limit: int = 100,
    ) -> list[AgentEvent]:
        events = [event for event in self.events if event.session_id == session_id]
        if after_id is not None:
            index = next(
                (index for index, event in enumerate(events) if event.id == after_id),
                len(events),
            )
            events = events[index + 1 :]
        return [event.model_copy(deep=True) for event in events[:limit]]

    async def list_events_scoped(
        self,
        *,
        scope: EventScope,
        after_id: str | None = None,
        limit: int = 100,
    ) -> list[AgentEvent]:
        events = [
            event
            for event in self.events
            if event.session_id == scope.session_id
            and event.tenant_id == scope.tenant_id
            and event.user_id == scope.user_id
        ]
        if after_id is not None:
            index = next(
                (index for index, event in enumerate(events) if event.id == after_id),
                len(events),
            )
            events = events[index + 1 :]
        return [event.model_copy(deep=True) for event in events[:limit]]

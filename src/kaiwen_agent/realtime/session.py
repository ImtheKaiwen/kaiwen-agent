from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.realtime.config import RealtimeChannel, RealtimeSessionConfig
from kaiwen_agent.types import utc_now


class RealtimeSessionStatus(StrEnum):
    CREATED = "created"
    CONNECTING = "connecting"
    ACTIVE = "active"
    INTERRUPTED = "interrupted"
    RECONNECTING = "reconnecting"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


ACTIVE_REALTIME_SESSION_STATUSES = frozenset(
    {
        RealtimeSessionStatus.CREATED,
        RealtimeSessionStatus.CONNECTING,
        RealtimeSessionStatus.ACTIVE,
        RealtimeSessionStatus.INTERRUPTED,
        RealtimeSessionStatus.RECONNECTING,
        RealtimeSessionStatus.CLOSING,
    }
)


class RealtimeSessionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"rt_{uuid4().hex}")
    conversation_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    client_id: str | None = None
    channel: RealtimeChannel
    provider: str = Field(min_length=1)
    provider_session_id: str | None = None
    status: RealtimeSessionStatus = RealtimeSessionStatus.CREATED
    config: RealtimeSessionConfig
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    connected_at: datetime | None = None
    disconnected_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_REALTIME_SESSION_STATUSES


ALLOWED_REALTIME_SESSION_TRANSITIONS: dict[
    RealtimeSessionStatus, frozenset[RealtimeSessionStatus]
] = {
    RealtimeSessionStatus.CREATED: frozenset(
        {RealtimeSessionStatus.CONNECTING, RealtimeSessionStatus.FAILED}
    ),
    RealtimeSessionStatus.CONNECTING: frozenset(
        {
            RealtimeSessionStatus.ACTIVE,
            RealtimeSessionStatus.CLOSING,
            RealtimeSessionStatus.FAILED,
        }
    ),
    RealtimeSessionStatus.ACTIVE: frozenset(
        {
            RealtimeSessionStatus.INTERRUPTED,
            RealtimeSessionStatus.RECONNECTING,
            RealtimeSessionStatus.CLOSING,
            RealtimeSessionStatus.FAILED,
        }
    ),
    RealtimeSessionStatus.INTERRUPTED: frozenset(
        {
            RealtimeSessionStatus.ACTIVE,
            RealtimeSessionStatus.RECONNECTING,
            RealtimeSessionStatus.CLOSING,
            RealtimeSessionStatus.FAILED,
        }
    ),
    RealtimeSessionStatus.RECONNECTING: frozenset(
        {
            RealtimeSessionStatus.ACTIVE,
            RealtimeSessionStatus.CLOSING,
            RealtimeSessionStatus.FAILED,
        }
    ),
    RealtimeSessionStatus.CLOSING: frozenset(
        {RealtimeSessionStatus.CLOSED, RealtimeSessionStatus.FAILED}
    ),
    RealtimeSessionStatus.CLOSED: frozenset(),
    RealtimeSessionStatus.FAILED: frozenset(),
}


class InvalidRealtimeSessionTransitionError(ValueError):
    pass


def transition_realtime_session(
    session: RealtimeSessionRecord,
    status: RealtimeSessionStatus,
) -> None:
    if status == session.status:
        return
    if status not in ALLOWED_REALTIME_SESSION_TRANSITIONS[session.status]:
        raise InvalidRealtimeSessionTransitionError(
            f"Cannot transition {session.status} -> {status}"
        )
    session.status = status
    session.updated_at = utc_now()
    if status == RealtimeSessionStatus.ACTIVE and session.connected_at is None:
        session.connected_at = session.updated_at
    if status in {RealtimeSessionStatus.CLOSED, RealtimeSessionStatus.FAILED}:
        session.disconnected_at = session.updated_at


class RealtimeSessionStore(Protocol):
    async def save(self, session: RealtimeSessionRecord) -> None: ...

    async def get(self, session_id: str) -> RealtimeSessionRecord | None: ...

    async def list_active(self) -> list[RealtimeSessionRecord]: ...


class InMemoryRealtimeSessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, RealtimeSessionRecord] = {}

    async def save(self, session: RealtimeSessionRecord) -> None:
        self._sessions[session.id] = session.model_copy(deep=True)

    async def get(self, session_id: str) -> RealtimeSessionRecord | None:
        session = self._sessions.get(session_id)
        return session.model_copy(deep=True) if session is not None else None

    async def list_active(self) -> list[RealtimeSessionRecord]:
        return [
            session.model_copy(deep=True) for session in self._sessions.values() if session.active
        ]

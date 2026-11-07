from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.types import utc_now

if TYPE_CHECKING:
    from kaiwen_agent.realtime.config import RealtimeSessionConfig
    from kaiwen_agent.realtime.session import RealtimeSessionRecord


class IncomingPhoneCall(BaseModel):
    """Verified provider webhook data; caller metadata never grants authority."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    provider_call_id: str = Field(min_length=1)
    caller: str | None = None
    callee: str | None = None
    received_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TelephonyControl(Protocol):
    async def accept(
        self,
        call: IncomingPhoneCall,
        *,
        config: RealtimeSessionConfig,
        conversation_id: str,
        tenant_id: str,
        user_id: str,
    ) -> RealtimeSessionRecord: ...

    async def reject(self, call: IncomingPhoneCall, *, status_code: int = 603) -> None: ...

    async def refer(self, call: IncomingPhoneCall, *, target_uri: str) -> None: ...

    async def hangup(self, call: IncomingPhoneCall) -> None: ...


class OutboundPhoneCall(BaseModel):
    """Provider-neutral request to originate one PSTN call."""

    model_config = ConfigDict(extra="forbid")

    to_number: str = Field(pattern=r"^\+[1-9]\d{7,14}$")
    status_callback_url: str | None = None
    sip_headers: dict[str, str] = Field(default_factory=dict)


class OutboundPhoneCallReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    provider_call_id: str
    status: str


class OutboundCallProvider(Protocol):
    async def place_call(self, request: OutboundPhoneCall) -> OutboundPhoneCallReceipt: ...


class IncomingCallDecisionStore(Protocol):
    async def claim(self, event_id: str) -> bool: ...

    async def complete(self, event_id: str) -> None: ...

    async def release(self, event_id: str) -> None: ...


class InMemoryIncomingCallDecisionStore:
    """Process-local webhook dedupe reference; production consumers provide durable storage."""

    def __init__(self) -> None:
        self._processing: set[str] = set()
        self._completed: set[str] = set()
        self._lock = asyncio.Lock()

    async def claim(self, event_id: str) -> bool:
        if not event_id:
            raise ValueError("event_id must not be empty")
        async with self._lock:
            if event_id in self._processing or event_id in self._completed:
                return False
            self._processing.add(event_id)
            return True

    async def complete(self, event_id: str) -> None:
        async with self._lock:
            if event_id not in self._processing:
                raise ValueError("event_id is not claimed")
            self._processing.remove(event_id)
            self._completed.add(event_id)

    async def release(self, event_id: str) -> None:
        async with self._lock:
            self._processing.discard(event_id)


__all__ = [
    "IncomingCallDecisionStore",
    "IncomingPhoneCall",
    "InMemoryIncomingCallDecisionStore",
    "OutboundCallProvider",
    "OutboundPhoneCall",
    "OutboundPhoneCallReceipt",
    "TelephonyControl",
]

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.types import utc_now


class DistributedLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    token: str = Field(min_length=1)
    fencing_token: int = Field(ge=1)
    acquired_at: datetime
    expires_at: datetime


class LeaseStore(Protocol):
    async def acquire(
        self,
        *,
        key: str,
        owner_id: str,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> DistributedLease | None: ...

    async def renew(
        self,
        lease: DistributedLease,
        *,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> DistributedLease | None: ...

    async def release(self, lease: DistributedLease) -> bool: ...


class InMemoryLeaseStore:
    """Deterministic reference lease store with fencing tokens."""

    def __init__(self) -> None:
        self._leases: dict[str, DistributedLease] = {}
        self._fences: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        *,
        key: str,
        owner_id: str,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> DistributedLease | None:
        observed_at = _validate(key, owner_id, ttl_seconds, now)
        async with self._lock:
            current = self._leases.get(key)
            if current is not None and current.expires_at > observed_at:
                return None
            fencing_token = self._fences.get(key, 0) + 1
            self._fences[key] = fencing_token
            lease = DistributedLease(
                key=key,
                owner_id=owner_id,
                token=f"lease_{uuid4().hex}",
                fencing_token=fencing_token,
                acquired_at=observed_at,
                expires_at=observed_at + timedelta(seconds=ttl_seconds),
            )
            self._leases[key] = lease
            return lease

    async def renew(
        self,
        lease: DistributedLease,
        *,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> DistributedLease | None:
        observed_at = _validate(lease.key, lease.owner_id, ttl_seconds, now)
        async with self._lock:
            current = self._leases.get(lease.key)
            if current is None or current.token != lease.token or current.expires_at <= observed_at:
                return None
            renewed = current.model_copy(
                update={
                    "expires_at": observed_at + timedelta(seconds=ttl_seconds),
                }
            )
            self._leases[lease.key] = renewed
            return renewed

    async def release(self, lease: DistributedLease) -> bool:
        async with self._lock:
            current = self._leases.get(lease.key)
            if current is None or current.token != lease.token:
                return False
            del self._leases[lease.key]
            return True


def _validate(
    key: str,
    owner_id: str,
    ttl_seconds: float,
    now: datetime | None,
) -> datetime:
    if not key or not owner_id:
        raise ValueError("Lease key and owner_id are required")
    if not 0 < ttl_seconds <= 86_400:
        raise ValueError("ttl_seconds must be between 0 and 86400")
    observed_at = now or utc_now()
    if observed_at.tzinfo is None:
        raise ValueError("Lease timestamps must be timezone-aware")
    return observed_at

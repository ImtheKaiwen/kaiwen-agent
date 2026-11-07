from __future__ import annotations

import importlib
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, Protocol, cast
from uuid import uuid4

from kaiwen_agent.coordination.leases import DistributedLease, _validate

_ACQUIRE_SCRIPT = """
if redis.call('exists', KEYS[1]) == 1 then
  return nil
end
local fence = redis.call('incr', KEYS[2])
redis.call('psetex', KEYS[1], ARGV[2], ARGV[1])
return fence
"""

_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


class AsyncRedisClient(Protocol):
    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object: ...


class RedisLeaseStore:
    """Redis-backed distributed lease with monotonic fencing tokens."""

    def __init__(
        self,
        client: AsyncRedisClient,
        *,
        key_prefix: str = "kaiwen:coordination",
    ) -> None:
        if not key_prefix or any(character.isspace() for character in key_prefix):
            raise ValueError("key_prefix must be non-empty and contain no whitespace")
        self.client = client
        self.key_prefix = key_prefix.rstrip(":")

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        key_prefix: str = "kaiwen:coordination",
        **options: Any,
    ) -> RedisLeaseStore:
        try:
            module = importlib.import_module("redis.asyncio")
        except ImportError as error:
            raise RuntimeError(
                "Install Redis support with `pip install kaiwen-agent[redis]`."
            ) from error
        client = module.Redis.from_url(url, **options)
        return cls(cast(AsyncRedisClient, client), key_prefix=key_prefix)

    async def acquire(
        self,
        *,
        key: str,
        owner_id: str,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> DistributedLease | None:
        observed_at = _validate(key, owner_id, ttl_seconds, now)
        token = f"lease_{uuid4().hex}"
        lease_key, fence_key = self._keys(key)
        result = await self.client.eval(
            _ACQUIRE_SCRIPT,
            2,
            lease_key,
            fence_key,
            token,
            _ttl_ms(ttl_seconds),
        )
        if result is None or result is False:
            return None
        fencing_token = int(cast(int | str | bytes, result))
        return DistributedLease(
            key=key,
            owner_id=owner_id,
            token=token,
            fencing_token=fencing_token,
            acquired_at=observed_at,
            expires_at=observed_at + timedelta(seconds=ttl_seconds),
        )

    async def renew(
        self,
        lease: DistributedLease,
        *,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> DistributedLease | None:
        observed_at = _validate(lease.key, lease.owner_id, ttl_seconds, now)
        lease_key, _ = self._keys(lease.key)
        result = await self.client.eval(
            _RENEW_SCRIPT,
            1,
            lease_key,
            lease.token,
            _ttl_ms(ttl_seconds),
        )
        if int(cast(int | str | bytes, result or 0)) != 1:
            return None
        return lease.model_copy(update={"expires_at": observed_at + timedelta(seconds=ttl_seconds)})

    async def release(self, lease: DistributedLease) -> bool:
        lease_key, _ = self._keys(lease.key)
        result = await self.client.eval(
            _RELEASE_SCRIPT,
            1,
            lease_key,
            lease.token,
        )
        return int(cast(int | str | bytes, result or 0)) == 1

    def _keys(self, key: str) -> tuple[str, str]:
        digest = sha256(key.encode()).hexdigest()
        return (
            f"{self.key_prefix}:lease:{digest}",
            f"{self.key_prefix}:fence:{digest}",
        )


def _ttl_ms(ttl_seconds: float) -> int:
    return max(1, int(ttl_seconds * 1000))

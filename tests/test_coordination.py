from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from kaiwen_agent.coordination import InMemoryLeaseStore, RedisLeaseStore


def test_in_memory_lease_fencing_and_stale_owner_protection() -> None:
    async def scenario() -> None:
        store = InMemoryLeaseStore()
        now = datetime(2026, 1, 1, tzinfo=UTC)
        first = await store.acquire(
            key="jobs:worker-1", owner_id="process-1", ttl_seconds=10, now=now
        )
        assert first is not None and first.fencing_token == 1
        assert (
            await store.acquire(
                key="jobs:worker-1",
                owner_id="process-2",
                ttl_seconds=10,
                now=now,
            )
            is None
        )
        second = await store.acquire(
            key="jobs:worker-1",
            owner_id="process-2",
            ttl_seconds=10,
            now=now + timedelta(seconds=11),
        )
        assert second is not None and second.fencing_token == 2
        assert not await store.release(first)
        assert await store.renew(first, ttl_seconds=10, now=now + timedelta(seconds=11)) is None
        assert await store.release(second)

    asyncio.run(scenario())


def test_in_memory_lease_has_one_winner_under_concurrency() -> None:
    async def scenario() -> None:
        store = InMemoryLeaseStore()
        now = datetime(2026, 1, 1, tzinfo=UTC)
        first_round = await asyncio.gather(
            *(
                store.acquire(
                    key="scheduler:leader",
                    owner_id=f"process-{index}",
                    ttl_seconds=5,
                    now=now,
                )
                for index in range(64)
            )
        )
        winners = [lease for lease in first_round if lease is not None]
        assert len(winners) == 1
        assert winners[0].fencing_token == 1

        second_round = await asyncio.gather(
            *(
                store.acquire(
                    key="scheduler:leader",
                    owner_id=f"replacement-{index}",
                    ttl_seconds=5,
                    now=now + timedelta(seconds=6),
                )
                for index in range(64)
            )
        )
        replacements = [lease for lease in second_round if lease is not None]
        assert len(replacements) == 1
        assert replacements[0].fencing_token == 2

    asyncio.run(scenario())


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.fences: dict[str, int] = {}
        self.seen_keys: list[str] = []

    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object:
        keys = [str(item) for item in keys_and_args[:numkeys]]
        args = [str(item) for item in keys_and_args[numkeys:]]
        self.seen_keys.extend(keys)
        lease_key = keys[0]
        if "incr" in script:
            if lease_key in self.values:
                return None
            fence_key = keys[1]
            self.fences[fence_key] = self.fences.get(fence_key, 0) + 1
            self.values[lease_key] = args[0]
            return self.fences[fence_key]
        if "pexpire" in script:
            return 1 if self.values.get(lease_key) == args[0] else 0
        if "del" in script and self.values.get(lease_key) == args[0]:
            del self.values[lease_key]
            return 1
        return 0


def test_redis_lease_adapter_uses_tokens_fences_and_hashed_keys() -> None:
    async def scenario() -> None:
        client = FakeRedis()
        store = RedisLeaseStore(client)
        lease = await store.acquire(
            key="tenant-secret-resource",
            owner_id="process-1",
            ttl_seconds=5,
        )
        assert lease is not None and lease.fencing_token == 1
        assert all("tenant-secret-resource" not in key for key in client.seen_keys)
        renewed = await store.renew(lease, ttl_seconds=10)
        assert renewed is not None and renewed.fencing_token == 1
        assert await store.release(renewed)

    asyncio.run(scenario())

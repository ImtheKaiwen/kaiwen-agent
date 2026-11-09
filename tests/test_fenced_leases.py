"""Package 1: `SQLiteLeaseStore` (ADR 0016).

Real SQLite files, a deterministic clock injected into the persistence adapter (the one clock that
decides expiry for the lease store and for every fenced write) and, for competition, separate
processes on the same file. No sleeps decide an outcome.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import kaiwen_agent
from kaiwen_agent.coordination import InMemoryLeaseStore, SQLiteLeaseStore
from kaiwen_agent.exceptions import FencedConfigurationError, OwnershipLostError
from kaiwen_agent.persistence import SQLitePersistence

TTL = 30.0


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def make(
    tmp_path: Path, name: str = "db.sqlite3"
) -> tuple[SQLitePersistence, SQLiteLeaseStore, Clock]:
    persistence = SQLitePersistence(tmp_path / name)
    clock = Clock()
    persistence.lease_clock = clock
    return persistence, SQLiteLeaseStore(persistence), clock


def run(factory):  # type: ignore[no-untyped-def]
    async def bounded():  # type: ignore[no-untyped-def]
        return await asyncio.wait_for(factory(), timeout=60)

    return asyncio.run(bounded())


def test_epochs_increase_per_key_and_never_reset_on_release_or_expiry(tmp_path: Path) -> None:
    async def scenario() -> None:
        _, leases, clock = make(tmp_path)
        first = await leases.acquire(key="k", owner_id="a", ttl_seconds=TTL)
        assert first is not None and first.fencing_token == 1
        assert await leases.acquire(key="k", owner_id="b", ttl_seconds=TTL) is None  # held
        assert await leases.release(first) is True
        second = await leases.acquire(key="k", owner_id="b", ttl_seconds=TTL)
        assert second is not None and second.fencing_token == 2  # release did not reset it
        clock.advance(TTL + 1)
        third = await leases.acquire(key="k", owner_id="c", ttl_seconds=TTL)
        assert third is not None and third.fencing_token == 3  # expiry did not reset it
        other = await leases.acquire(key="other", owner_id="a", ttl_seconds=TTL)
        assert other is not None and other.fencing_token == 1  # counters are per key

    run(scenario)


def test_a_lease_is_held_until_exactly_its_expiry_and_expiry_is_never_revived(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        _, leases, clock = make(tmp_path)
        lease = await leases.acquire(key="k", owner_id="a", ttl_seconds=TTL)
        assert lease is not None
        clock.advance(TTL - 1)
        renewed = await leases.renew(lease, ttl_seconds=TTL)
        assert renewed is not None and renewed.expires_at > lease.expires_at
        clock.advance(TTL)  # past the renewed expiry
        assert await leases.renew(renewed, ttl_seconds=TTL) is None  # not revived
        assert await leases.acquire(key="k", owner_id="b", ttl_seconds=TTL) is not None

    run(scenario)


def test_an_old_owner_cannot_renew_or_release_a_successors_lease(tmp_path: Path) -> None:
    async def scenario() -> None:
        _, leases, clock = make(tmp_path)
        old = await leases.acquire(key="k", owner_id="a", ttl_seconds=TTL)
        assert old is not None
        clock.advance(TTL + 1)
        new = await leases.acquire(key="k", owner_id="b", ttl_seconds=TTL)
        assert new is not None
        assert await leases.renew(old, ttl_seconds=TTL) is None
        assert await leases.release(old) is False
        # The successor is untouched and still holds the lease.
        assert await leases.renew(new, ttl_seconds=TTL) is not None
        # Same owner id but a stale credential or generation is not the holder either.
        forged = old.model_copy(update={"owner_id": "b"})
        assert await leases.renew(forged, ttl_seconds=TTL) is None
        assert await leases.release(forged) is False

    run(scenario)


def test_the_lease_database_must_be_a_real_file_with_the_fenced_contract(tmp_path: Path) -> None:
    with pytest.raises(FencedConfigurationError):
        SQLiteLeaseStore(SQLitePersistence(":memory:"))
    with pytest.raises(FencedConfigurationError):
        SQLiteLeaseStore(SQLitePersistence("file::memory:?cache=shared"))
    with pytest.raises(FencedConfigurationError):
        SQLiteLeaseStore(object())  # type: ignore[arg-type]  # a name match is not a contract


def test_the_in_memory_store_is_unchanged_and_not_a_fenced_authority() -> None:
    assert not hasattr(InMemoryLeaseStore, "issue_owner_epoch")


CHILD = """
import asyncio, sys
from kaiwen_agent.persistence import SQLitePersistence
from kaiwen_agent.coordination import SQLiteLeaseStore

async def main():
    leases = SQLiteLeaseStore(SQLitePersistence(sys.argv[1]))
    lease = await leases.acquire(key="k", owner_id=sys.argv[2], ttl_seconds=600)
    print("EPOCH", lease.fencing_token if lease else "none", flush=True)

asyncio.run(main())
"""


def test_processes_competing_for_one_lease_on_the_same_file_have_exactly_one_winner(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "shared.sqlite3"
        await SQLitePersistence(path).initialize()
        root = str(Path(kaiwen_agent.__file__).resolve().parent.parent)
        env = {**os.environ, "PYTHONPATH": root}

        async def contender(index: int) -> str:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                CHILD,
                str(path),
                f"owner-{index}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            out, err = await process.communicate()
            assert process.returncode == 0, err.decode()
            return out.decode().strip()

        results = await asyncio.gather(*(contender(i) for i in range(5)))
        winners = [line for line in results if line != "EPOCH none"]
        assert winners == ["EPOCH 1"], results  # one holder, one generation
        # A later acquirer in this process sees the same authoritative row.
        leases = SQLiteLeaseStore(SQLitePersistence(path))
        assert await leases.acquire(key="k", owner_id="late", ttl_seconds=TTL) is None

    run(scenario)


def test_corrupted_lease_state_is_refused_not_interpreted(tmp_path: Path) -> None:
    async def scenario() -> None:
        persistence, leases, _ = make(tmp_path)
        lease = await leases.acquire(key="k", owner_id="a", ttl_seconds=TTL)
        assert lease is not None
        with sqlite3.connect(persistence.path) as connection:
            connection.execute("UPDATE fence_leases SET epoch = 'garbage' WHERE key = 'k'")
        for call in (
            leases.acquire(key="k", owner_id="b", ttl_seconds=TTL),
            leases.renew(lease, ttl_seconds=TTL),
            leases.release(lease),
        ):
            with pytest.raises(OwnershipLostError) as caught:
                await call
            assert caught.value.code == "authority_unavailable"

    run(scenario)


def test_lock_contention_fails_within_the_configured_bound(tmp_path: Path) -> None:
    async def scenario() -> None:
        persistence = SQLitePersistence(tmp_path / "busy.sqlite3", busy_timeout_seconds=0.2)
        persistence.lease_clock = Clock()
        leases = SQLiteLeaseStore(persistence)
        await persistence.initialize()
        blocker = sqlite3.connect(persistence.path)
        blocker.execute("BEGIN IMMEDIATE")  # another writer holds the database
        try:
            with pytest.raises(sqlite3.OperationalError):
                await leases.acquire(key="k", owner_id="a", ttl_seconds=TTL)
        finally:
            blocker.rollback()
            blocker.close()
        # Nothing was granted while the database was busy.
        assert (await leases.acquire(key="k", owner_id="a", ttl_seconds=TTL)) is not None

    run(scenario)


def test_the_lease_tables_are_added_to_an_existing_database_without_touching_data(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        persistence, leases, _ = make(tmp_path)
        await persistence.initialize()
        with sqlite3.connect(persistence.path) as connection:
            connection.execute("DROP TABLE fence_leases")
            connection.execute("DROP TABLE fence_meta")
            connection.execute("INSERT INTO sessions VALUES ('s', '{}', 't', 't')")
        upgraded, upgraded_leases, _ = make(tmp_path)
        assert await upgraded.database_id()
        assert await upgraded_leases.acquire(key="k", owner_id="a", ttl_seconds=TTL) is not None
        with sqlite3.connect(persistence.path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1

    run(scenario)

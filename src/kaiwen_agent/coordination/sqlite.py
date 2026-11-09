"""A lease store that lives in the same SQLite database as the records it protects (ADR 0016).

Acquisition, epoch allocation, renewal and release are each one `BEGIN IMMEDIATE` transaction on
the same file `SQLitePersistence` writes its tasks, runs and action intents to. A fenced write
(`save_task_fenced`, fenced run transitions, fenced action-intent writes) evaluates the lease row
inside its own transaction, so a lease that has expired or been taken over is refused when the
write runs, not when someone last checked.

Epochs are per lease key, start at 1, only ever increase, and survive release and expiry: a row is
never deleted, a release only clears its credential.

Clock policy. Expiry is judged against `persistence.lease_clock` (UTC wall clock, millisecond
resolution), the single clock shared by this store and every fenced write, so they cannot disagree.
Tests inject a deterministic clock there. Wall-clock expiry assumes a monotonic-enough local clock:
a clock that steps backwards can extend a lease, and one that steps forward can end it early. This
is why the store is limited to one local database file (one host, one clock); it is not a
distributed lease and it does not replace `RedisLeaseStore` for multi-host, non-delegating use.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from kaiwen_agent.coordination.fencing import FENCED_RUN_LEASE_PREFIX, OwnerEpoch
from kaiwen_agent.coordination.leases import DistributedLease, _validate
from kaiwen_agent.exceptions import FencedConfigurationError, OwnershipLostError

if TYPE_CHECKING:
    from kaiwen_agent.persistence.sqlite import SQLitePersistence

_CONTRACT = "kaiwen.fenced-execution.v1"


def read_lease_row(connection: sqlite3.Connection, key: str) -> tuple[str, str, int, int] | None:
    """(owner_id, token, epoch, expires_at_ms) of a lease key, or `None` if it never existed.

    A row that is not exactly that shape is corrupt: authority cannot be established, so it is
    refused (`authority_unavailable`) rather than interpreted.
    """

    row = connection.execute(
        "SELECT owner_id, token, epoch, expires_at_ms FROM fence_leases WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    owner_id, token, epoch, expires = (
        row["owner_id"],
        row["token"],
        row["epoch"],
        row["expires_at_ms"],
    )
    if (
        not isinstance(owner_id, str)
        or not isinstance(token, str)
        or type(epoch) is not int
        or epoch < 1
        or type(expires) is not int
    ):
        raise OwnershipLostError("authority_unavailable")
    return owner_id, token, epoch, expires


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


class SQLiteLeaseStore:
    """`LeaseStore` with transactional epochs, in the database of the protected records."""

    def __init__(self, persistence: SQLitePersistence) -> None:
        if getattr(persistence, "fenced_execution_contract", None) != _CONTRACT:
            raise FencedConfigurationError(
                "The persistence adapter does not declare the fenced execution contract"
            )
        location = str(persistence.path)
        if location in {"", ":memory:"} or "mode=memory" in location or ":memory:" in location:
            raise FencedConfigurationError(
                "Fenced execution needs a database file; in-memory databases are not supported"
            )
        self.persistence = persistence

    def _clock(self, now: datetime | None) -> datetime:
        """The time of the transaction: an explicit `now` (tests), else the shared clock.

        Always called inside the transaction, after the write lock is held, never before waiting
        for it, so expiry is judged at the protected boundary and not at a stale sample.
        """

        moment = now if now is not None else self.persistence.lease_clock()
        if moment.tzinfo is None:
            raise ValueError("Lease timestamps must be timezone-aware")
        return moment

    async def acquire(
        self,
        *,
        key: str,
        owner_id: str,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> DistributedLease | None:
        _validate(key, owner_id, ttl_seconds, now)
        token = f"lease_{uuid4().hex}"
        await self.persistence.initialize()

        def operation(connection: sqlite3.Connection) -> tuple[int, datetime, int, int] | None:
            connection.execute("BEGIN IMMEDIATE")
            observed = self._clock(now)
            now_ms = _ms(observed)
            expires_ms = now_ms + max(1, int(ttl_seconds * 1000))
            row = read_lease_row(connection, key)
            if row is not None and row[1] != "" and row[3] > now_ms:
                return None  # held and unexpired
            epoch = (row[2] if row is not None else 0) + 1
            connection.execute(
                """INSERT INTO fence_leases (key, owner_id, token, epoch, expires_at_ms)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    owner_id = excluded.owner_id, token = excluded.token,
                    epoch = excluded.epoch, expires_at_ms = excluded.expires_at_ms""",
                (key, owner_id, token, epoch, expires_ms),
            )
            return epoch, observed, now_ms, expires_ms

        granted = await self.persistence._run(operation)
        if granted is None:
            return None
        epoch, observed, now_ms, expires_ms = granted
        return DistributedLease(
            key=key,
            owner_id=owner_id,
            token=token,
            fencing_token=epoch,
            acquired_at=observed,
            expires_at=observed + timedelta(milliseconds=expires_ms - now_ms),
        )

    async def renew(
        self,
        lease: DistributedLease,
        *,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> DistributedLease | None:
        _validate(lease.key, lease.owner_id, ttl_seconds, now)
        await self.persistence.initialize()

        def operation(connection: sqlite3.Connection) -> tuple[datetime, int, int] | None:
            connection.execute("BEGIN IMMEDIATE")
            observed = self._clock(now)
            now_ms = _ms(observed)
            expires_ms = now_ms + max(1, int(ttl_seconds * 1000))
            row = read_lease_row(connection, lease.key)
            if (
                row is None
                or row[1] != lease.token
                or row[0] != lease.owner_id
                or row[2] != lease.fencing_token
                or row[3] <= now_ms
            ):
                return None  # not the holder, or already expired: expiry is never revived
            connection.execute(
                "UPDATE fence_leases SET expires_at_ms = ? WHERE key = ?", (expires_ms, lease.key)
            )
            return observed, now_ms, expires_ms

        renewed = await self.persistence._run(operation)
        if renewed is None:
            return None
        observed, now_ms, expires_ms = renewed
        return lease.model_copy(
            update={"expires_at": observed + timedelta(milliseconds=expires_ms - now_ms)}
        )

    async def release(self, lease: DistributedLease) -> bool:
        await self.persistence.initialize()

        def operation(connection: sqlite3.Connection) -> bool:
            connection.execute("BEGIN IMMEDIATE")
            row = read_lease_row(connection, lease.key)
            if (
                row is None
                or row[1] != lease.token
                or row[0] != lease.owner_id
                or row[2] != lease.fencing_token
            ):
                return False  # never a successor's lease
            # The row (and its epoch) stays: counters never reset.
            connection.execute(
                "UPDATE fence_leases SET owner_id = '', token = '', expires_at_ms = 0 "
                "WHERE key = ?",
                (lease.key,),
            )
            return True

        return bool(await self.persistence._run(operation))

    async def issue_owner_epoch(
        self,
        lease: DistributedLease,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        session_id: str,
    ) -> OwnerEpoch:
        """The authority of `lease`'s holder over `run_id`, issued only while it is live.

        This is the one place an `OwnerEpoch` is created from a lease. It re-reads the
        authoritative lease row, so a lease that is no longer held cannot become an epoch.
        """

        if lease.key != f"{FENCED_RUN_LEASE_PREFIX}{run_id}":
            raise FencedConfigurationError("The lease does not belong to this run")
        await self.persistence.initialize()

        def operation(connection: sqlite3.Connection) -> str:
            connection.execute("BEGIN IMMEDIATE")
            now_ms = _ms(self._clock(None))
            row = read_lease_row(connection, lease.key)
            if row is None or row[1] != lease.token or row[2] != lease.fencing_token:
                raise OwnershipLostError("lease_not_held")
            if row[3] <= now_ms:
                raise OwnershipLostError("lease_expired")
            meta = connection.execute("SELECT database_id FROM fence_meta WHERE id = 1").fetchone()
            if meta is None or not isinstance(meta["database_id"], str):
                raise OwnershipLostError("authority_unavailable")
            return str(meta["database_id"])

        database_id = await self.persistence._run(operation)
        return OwnerEpoch(
            database_id=database_id,
            lease_key=lease.key,
            owner_id=lease.owner_id,
            epoch=lease.fencing_token,
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            credential=lease.token,
        )

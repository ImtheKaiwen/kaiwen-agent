"""Host-owned proof of execution ownership for fenced execution (ADR 0016).

An `OwnerEpoch` is issued by `SQLiteLeaseStore.issue_owner_epoch` from a lease that was actually
acquired, and it names one durable run, its owner, the lease key, the fencing generation and the
database the lease lives in. It is not trusted because of its type: every protected write
re-validates it against the authoritative lease row inside the write's own transaction, and the
lease credential it carries is a random secret that only the lease holder knows. A forged or
stale epoch is therefore refused at the enforcement boundary.

The epoch is host state. It is never read from task fields, tool arguments, prompts or model
output, its credential is excluded from `repr`, and nothing here reaches a model.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

FENCED_RUN_LEASE_PREFIX = "runtime-run:"


@dataclass(frozen=True, slots=True)
class OwnerEpoch:
    """The authority of one executor over one durable run, at one fencing generation."""

    database_id: str
    lease_key: str
    owner_id: str
    epoch: int
    run_id: str
    tenant_id: str
    user_id: str
    session_id: str
    credential: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        values = (
            self.database_id,
            self.lease_key,
            self.owner_id,
            self.run_id,
            self.tenant_id,
            self.user_id,
            self.session_id,
            self.credential,
        )
        if (
            not all(isinstance(value, str) and value for value in values)
            or type(self.epoch) is not int
            or self.epoch < 1
            or self.lease_key != f"{FENCED_RUN_LEASE_PREFIX}{self.run_id}"
        ):
            raise ValueError("Invalid owner epoch")


_CURRENT: ContextVar[OwnerEpoch | None] = ContextVar("kaiwen_owner_epoch", default=None)


def current_owner_epoch() -> OwnerEpoch | None:
    """The epoch the running controller bound for this execution context, if any."""

    return _CURRENT.get()


@contextmanager
def bind_owner_epoch(epoch: OwnerEpoch) -> Iterator[None]:
    """Bind `epoch` for the code the controller runs (scheduler, workers, tools) in this context."""

    token = _CURRENT.set(epoch)
    try:
        yield
    finally:
        _CURRENT.reset(token)

"""Composition object for fenced execution over one local SQLite database (ADR 0016).

`FencedExecution` is the explicit, reviewed way to switch a `WorkflowController` into fenced
mode. It exists to make unsupported combinations impossible to assemble silently:

- the lease store must be a `SQLiteLeaseStore` and its database must be the very same
  `SQLitePersistence` object the tasks, runs and action intents are stored in (not a second adapter
  on the same path, which would have its own clock, and never another file or a memory database);
- the task store handed to schedulers is the `FencedTaskStore` it owns, so a scheduler cannot be
  composed over an unfenced store and still be accepted;
- a custom store is never accepted because it has a matching method name: the persistence adapter
  must declare the fenced execution contract, and only `SQLitePersistence` implements it.

Redis leases, in-memory stores, other databases and custom task stores are refused at construction.
Nothing here silently downgrades to ordinary writes.
"""

from __future__ import annotations

from typing import Any

from kaiwen_agent.coordination.sqlite import SQLiteLeaseStore
from kaiwen_agent.exceptions import FencedConfigurationError
from kaiwen_agent.persistence.sqlite import SQLitePersistence
from kaiwen_agent.tasks.fenced import FencedTaskStore


class FencedExecution:
    """One SQLite database that is the authority for leases, runs, tasks and action intents."""

    def __init__(self, *, persistence: SQLitePersistence, leases: SQLiteLeaseStore) -> None:
        if not isinstance(persistence, SQLitePersistence):
            raise FencedConfigurationError(
                "Fenced execution supports only SQLitePersistence in this release"
            )
        if not isinstance(leases, SQLiteLeaseStore):
            raise FencedConfigurationError(
                "Fenced execution needs a SQLiteLeaseStore; Redis and in-memory leases are not "
                "supported"
            )
        if leases.persistence is not persistence:
            raise FencedConfigurationError(
                "The lease store and the protected records must share one SQLitePersistence "
                "instance (one authoritative database)"
            )
        self.persistence = persistence
        self.leases = leases
        self.task_store = FencedTaskStore(persistence)

    def accepts_scheduler(self, scheduler: Any) -> bool:
        """Whether `scheduler` writes only through this execution's fenced task store."""

        return getattr(scheduler, "store", None) is self.task_store

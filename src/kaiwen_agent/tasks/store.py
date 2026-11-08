from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from kaiwen_agent.tasks.models import AgentTask, TaskStatus

MAX_TASK_RUN_PAGE = 200


class TaskStore(Protocol):
    async def save_task(self, task: AgentTask) -> None: ...

    async def get_task(self, task_id: str) -> AgentTask | None: ...

    async def list_tasks(self, *, status: TaskStatus | None = None) -> list[AgentTask]: ...


@dataclass(frozen=True, slots=True)
class TaskRunPage:
    """One bounded page of the tasks of a single durable run.

    `next_after_id` is the keyset cursor for the following page, or `None` when this page was
    the last one. `unreadable` counts rows that matched the scope but could not be
    materialized as a task of that exact owner and run; they are never returned.
    """

    tasks: tuple[AgentTask, ...]
    next_after_id: str | None = None
    unreadable: int = 0


@runtime_checkable
class TaskRunQuery(Protocol):
    """Optional capability: owner- and run-scoped, bounded, keyset-paginated task listing.

    Implementations filter by tenant, user and durable run before materializing any row, order
    by task id, and reject `limit` outside `1..MAX_TASK_RUN_PAGE` with `ValueError`. A task is
    returned only if the store can verify that its claimed owner and run are an existing run of
    that owner; a task whose claim cannot be verified is not returned. This is membership, not
    proof of who wrote the task: callers of `save_task` are trusted (ADR 0015). A store without
    this capability is not queried through `list_tasks`: consumers fail closed instead.

    A conforming page examines at least one row when it reports a cursor, returns tasks in
    strictly ascending id order after `after_id`, never more than `limit` rows in total
    (tasks plus `unreadable`), and a cursor that strictly advances. Consumers must not rely on
    this: `LeadViewAssembler` rejects a page that violates it.
    """

    async def list_run_tasks(
        self,
        *,
        tenant_id: str,
        user_id: str,
        runtime_run_id: str,
        limit: int = 100,
        after_id: str | None = None,
    ) -> TaskRunPage: ...


class InMemoryTaskStore:
    def __init__(self) -> None:
        self._tasks: dict[str, AgentTask] = {}

    async def save_task(self, task: AgentTask) -> None:
        self._tasks[task.id] = task.model_copy(deep=True)

    async def get_task(self, task_id: str) -> AgentTask | None:
        task = self._tasks.get(task_id)
        return task.model_copy(deep=True) if task else None

    async def list_tasks(self, *, status: TaskStatus | None = None) -> list[AgentTask]:
        tasks = [task for task in self._tasks.values() if status is None or task.status == status]
        return [task.model_copy(deep=True) for task in tasks]

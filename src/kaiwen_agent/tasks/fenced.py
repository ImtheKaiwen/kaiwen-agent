"""A task store whose every write is fenced by the controller's `OwnerEpoch` (ADR 0016).

`FencedTaskStore` is what a `TaskScheduler` is given when its writes must stop the moment the
executor loses the run. It has no ordinary-save path: `save_task` writes through
`SQLitePersistence.save_task_fenced` with the epoch the controller bound for this execution
context, and refuses (`OwnershipLostError("no_epoch_bound")`) when there is none. It never falls
back to `save_task` on the underlying store.

It is not a security boundary against arbitrary Python in a worker (that code can call the
underlying persistence directly); it is the composition that makes the platform's own scheduler
and tools fail closed. Reads delegate unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kaiwen_agent.coordination.fencing import current_owner_epoch
from kaiwen_agent.exceptions import FencedConfigurationError, OwnershipLostError
from kaiwen_agent.tasks.models import AgentTask, TaskStatus
from kaiwen_agent.tasks.store import TaskRunPage

if TYPE_CHECKING:
    from kaiwen_agent.persistence.sqlite import SQLitePersistence

_CONTRACT = "kaiwen.fenced-execution.v1"


class FencedTaskStore:
    """`TaskStore` (and `TaskRunQuery`) over a persistence adapter that enforces fenced writes."""

    fenced_task_writes = True

    def __init__(self, persistence: SQLitePersistence) -> None:
        if getattr(persistence, "fenced_execution_contract", None) != _CONTRACT:
            raise FencedConfigurationError(
                "The task store does not declare the fenced execution contract"
            )
        self.persistence = persistence

    async def save_task(self, task: AgentTask) -> None:
        epoch = current_owner_epoch()
        if epoch is None:
            raise OwnershipLostError("no_epoch_bound")
        await self.persistence.save_task_fenced(task, epoch)

    async def get_task(self, task_id: str) -> AgentTask | None:
        return await self.persistence.get_task(task_id)

    async def list_tasks(self, *, status: TaskStatus | None = None) -> list[AgentTask]:
        return await self.persistence.list_tasks(status=status)

    async def list_run_tasks(
        self,
        *,
        tenant_id: str,
        user_id: str,
        runtime_run_id: str,
        limit: int = 100,
        after_id: str | None = None,
    ) -> TaskRunPage:
        return await self.persistence.list_run_tasks(
            tenant_id=tenant_id,
            user_id=user_id,
            runtime_run_id=runtime_run_id,
            limit=limit,
            after_id=after_id,
        )

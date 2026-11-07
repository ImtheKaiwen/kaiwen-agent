from __future__ import annotations

from typing import Protocol

from kaiwen_agent.tasks.models import AgentTask, TaskStatus


class TaskStore(Protocol):
    async def save_task(self, task: AgentTask) -> None: ...

    async def get_task(self, task_id: str) -> AgentTask | None: ...

    async def list_tasks(self, *, status: TaskStatus | None = None) -> list[AgentTask]: ...


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

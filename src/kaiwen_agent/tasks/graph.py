from __future__ import annotations

from collections import deque

from kaiwen_agent.tasks.models import AgentTask, TaskStatus
from kaiwen_agent.tasks.state import transition_task


class TaskGraphError(ValueError):
    pass


class TaskGraph:
    def __init__(self, tasks: list[AgentTask] | None = None) -> None:
        self._tasks: dict[str, AgentTask] = {}
        for task in tasks or []:
            self.add_task(task)
        self.validate()

    def add_task(self, task: AgentTask) -> None:
        if task.id in self._tasks:
            raise TaskGraphError(f"Duplicate task: {task.id}")
        self._tasks[task.id] = task

    def get(self, task_id: str) -> AgentTask:
        try:
            return self._tasks[task_id]
        except KeyError as error:
            raise TaskGraphError(f"Unknown task: {task_id}") from error

    def tasks(self) -> tuple[AgentTask, ...]:
        return tuple(self._tasks.values())

    def add_dependency(self, task_id: str, depends_on: str) -> None:
        task = self.get(task_id)
        self.get(depends_on)
        if task_id == depends_on:
            raise TaskGraphError("A task cannot depend on itself")
        if depends_on in task.dependencies:
            return
        task.dependencies.append(depends_on)
        try:
            self.validate()
        except TaskGraphError:
            task.dependencies.remove(depends_on)
            raise

    def validate(self) -> None:
        for task in self._tasks.values():
            unknown = set(task.dependencies) - self._tasks.keys()
            if unknown:
                raise TaskGraphError(f"Task {task.id} has unknown dependencies: {sorted(unknown)}")
        self.topological_order()

    def topological_order(self) -> tuple[str, ...]:
        indegree = {task_id: 0 for task_id in self._tasks}
        dependents: dict[str, list[str]] = {task_id: [] for task_id in self._tasks}
        for task in self._tasks.values():
            indegree[task.id] = len(task.dependencies)
            for dependency in task.dependencies:
                if dependency not in dependents:
                    raise TaskGraphError(f"Unknown dependency: {dependency}")
                dependents[dependency].append(task.id)
        queue = deque(sorted(task_id for task_id, degree in indegree.items() if degree == 0))
        ordered: list[str] = []
        while queue:
            task_id = queue.popleft()
            ordered.append(task_id)
            for dependent in sorted(dependents[task_id]):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    queue.append(dependent)
        if len(ordered) != len(self._tasks):
            raise TaskGraphError("Task graph contains a cycle")
        return tuple(ordered)

    def refresh_states(self) -> None:
        for task_id in self.topological_order():
            task = self._tasks[task_id]
            if task.status not in {TaskStatus.PENDING, TaskStatus.WAITING_DEPENDENCY}:
                continue
            dependencies = [self._tasks[item] for item in task.dependencies]
            dependency_failed = any(
                item.status in {TaskStatus.FAILED, TaskStatus.CANCELLED} for item in dependencies
            )
            if dependency_failed:
                if task.status == TaskStatus.PENDING:
                    transition_task(task, TaskStatus.WAITING_DEPENDENCY)
                task.error = "A dependency did not complete"
                task.error_code = "dependency_failed"
                transition_task(task, TaskStatus.FAILED)
            elif all(item.status == TaskStatus.COMPLETED for item in dependencies):
                transition_task(task, TaskStatus.READY)
            elif task.status == TaskStatus.PENDING:
                transition_task(task, TaskStatus.WAITING_DEPENDENCY)

    def ready_tasks(self) -> tuple[AgentTask, ...]:
        self.refresh_states()
        return tuple(
            self._tasks[task_id]
            for task_id in self.topological_order()
            if self._tasks[task_id].status == TaskStatus.READY
        )

    @property
    def complete(self) -> bool:
        return all(task.terminal for task in self._tasks.values())

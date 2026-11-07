from __future__ import annotations

from kaiwen_agent.tasks.models import AgentTask, TaskStatus
from kaiwen_agent.types import utc_now

ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {TaskStatus.READY, TaskStatus.WAITING_DEPENDENCY, TaskStatus.CANCELLED}
    ),
    TaskStatus.WAITING_DEPENDENCY: frozenset(
        {TaskStatus.READY, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.READY: frozenset(
        {TaskStatus.QUEUED, TaskStatus.WAITING_RESOURCE, TaskStatus.CANCELLED}
    ),
    TaskStatus.WAITING_RESOURCE: frozenset(
        {TaskStatus.READY, TaskStatus.QUEUED, TaskStatus.CANCELLED}
    ),
    TaskStatus.QUEUED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.PAUSED,
            TaskStatus.WAITING_USER,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.WAITING_RESOURCE,
        }
    ),
    TaskStatus.PAUSED: frozenset({TaskStatus.RUNNING, TaskStatus.READY, TaskStatus.CANCELLED}),
    TaskStatus.WAITING_USER: frozenset({TaskStatus.READY, TaskStatus.CANCELLED}),
    TaskStatus.WAITING_APPROVAL: frozenset({TaskStatus.READY, TaskStatus.CANCELLED}),
    TaskStatus.FAILED: frozenset({TaskStatus.READY, TaskStatus.CANCELLED}),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


class InvalidTaskTransitionError(ValueError):
    pass


def transition_task(task: AgentTask, status: TaskStatus) -> None:
    if status == task.status:
        return
    if status not in ALLOWED_TRANSITIONS[task.status]:
        raise InvalidTaskTransitionError(f"Cannot transition {task.status} -> {status}")
    task.status = status
    if status == TaskStatus.RUNNING and task.started_at is None:
        task.started_at = utc_now()
    if status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
        task.completed_at = utc_now()
        if status == TaskStatus.COMPLETED:
            task.progress = 1.0
    elif task.completed_at is not None:
        task.completed_at = None

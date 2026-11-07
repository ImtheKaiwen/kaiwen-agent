from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from kaiwen_agent.control.mailbox import ControlMessage
from kaiwen_agent.control.runtime import RunControl
from kaiwen_agent.tasks.models import AgentTask

ProgressCallback = Callable[[float, dict[str, Any]], Awaitable[None]]


class TaskExecutionContext:
    def __init__(
        self,
        *,
        task_id: str,
        control: RunControl,
        progress_callback: ProgressCallback,
    ) -> None:
        self.task_id = task_id
        self.control = control
        self.progress_callback = progress_callback

    async def checkpoint(self, *, state: dict[str, Any] | None = None) -> list[ControlMessage]:
        return await self.control.checkpoint(self.task_id, state=state)

    async def report_progress(
        self, progress: float, *, details: dict[str, Any] | None = None
    ) -> None:
        if not 0 <= progress <= 1:
            raise ValueError("progress must be between 0 and 1")
        await self.progress_callback(progress, details or {})


class Worker(Protocol):
    async def execute(self, task: AgentTask, context: TaskExecutionContext) -> dict[str, Any]: ...


WorkerFunction = Callable[[AgentTask, TaskExecutionContext], Awaitable[dict[str, Any]]]


class FunctionWorker:
    def __init__(self, function: WorkerFunction) -> None:
        self.function = function

    async def execute(self, task: AgentTask, context: TaskExecutionContext) -> dict[str, Any]:
        return await self.function(task, context)


class RetryableTaskError(Exception):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code

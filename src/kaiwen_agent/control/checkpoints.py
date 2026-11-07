from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.types import utc_now


class TaskCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"checkpoint_{uuid4().hex}")
    task_id: str
    sequence: int = Field(ge=1)
    state: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class CheckpointStore(Protocol):
    async def save_checkpoint(self, checkpoint: TaskCheckpoint) -> None: ...

    async def latest_checkpoint(self, task_id: str) -> TaskCheckpoint | None: ...


class InMemoryCheckpointStore:
    def __init__(self) -> None:
        self._checkpoints: dict[str, list[TaskCheckpoint]] = {}

    async def save_checkpoint(self, checkpoint: TaskCheckpoint) -> None:
        self._checkpoints.setdefault(checkpoint.task_id, []).append(
            checkpoint.model_copy(deep=True)
        )

    async def latest_checkpoint(self, task_id: str) -> TaskCheckpoint | None:
        checkpoints = self._checkpoints.get(task_id, [])
        return checkpoints[-1].model_copy(deep=True) if checkpoints else None

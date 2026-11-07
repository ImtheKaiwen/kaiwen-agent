from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.types import utc_now

ControlType = Literal["instruction", "cancel", "pause", "resume", "approval"]


class ControlMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"control_{uuid4().hex}")
    type: ControlType
    content: str = ""
    created_by: str
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class InMemoryMailbox:
    def __init__(self) -> None:
        self._messages: dict[str, deque[ControlMessage]] = defaultdict(deque)
        self._conditions: dict[str, asyncio.Condition] = defaultdict(asyncio.Condition)
        self._paused: set[str] = set()
        self._cancelled: set[str] = set()

    async def send(self, task_id: str, message: ControlMessage) -> None:
        condition = self._conditions[task_id]
        async with condition:
            self._messages[task_id].append(message.model_copy(deep=True))
            if message.type == "cancel":
                self._cancelled.add(task_id)
            elif message.type == "pause":
                self._paused.add(task_id)
            elif message.type == "resume":
                self._paused.discard(task_id)
            condition.notify_all()

    async def checkpoint(self, task_id: str) -> list[ControlMessage]:
        condition = self._conditions[task_id]
        async with condition:
            if task_id in self._cancelled:
                raise TaskCancelledError(f"Task cancelled: {task_id}")
            while task_id in self._paused:
                await condition.wait()
                if task_id in self._cancelled:
                    raise TaskCancelledError(f"Task cancelled: {task_id}")
            messages = list(self._messages[task_id])
            self._messages[task_id].clear()
            return [message.model_copy(deep=True) for message in messages]


class TaskCancelledError(Exception):
    pass

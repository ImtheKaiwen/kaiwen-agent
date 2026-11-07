from __future__ import annotations

from typing import Any

from kaiwen_agent.control.checkpoints import (
    CheckpointStore,
    InMemoryCheckpointStore,
    TaskCheckpoint,
)
from kaiwen_agent.control.mailbox import ControlMessage, InMemoryMailbox


class RunControl:
    def __init__(
        self,
        mailbox: InMemoryMailbox | None = None,
        checkpoints: CheckpointStore | None = None,
    ) -> None:
        self.mailbox = mailbox or InMemoryMailbox()
        self.checkpoints = checkpoints or InMemoryCheckpointStore()
        self._sequences: dict[str, int] = {}

    async def send(self, task_id: str, message: ControlMessage) -> None:
        await self.mailbox.send(task_id, message)

    async def checkpoint(
        self,
        task_id: str,
        *,
        state: dict[str, Any] | None = None,
    ) -> list[ControlMessage]:
        current_sequence = self._sequences.get(task_id)
        if current_sequence is None:
            latest = await self.checkpoints.latest_checkpoint(task_id)
            current_sequence = latest.sequence if latest is not None else 0
        sequence = current_sequence + 1
        self._sequences[task_id] = sequence
        await self.checkpoints.save_checkpoint(
            TaskCheckpoint(task_id=task_id, sequence=sequence, state=state or {})
        )
        return await self.mailbox.checkpoint(task_id)

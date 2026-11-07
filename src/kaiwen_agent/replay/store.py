from __future__ import annotations

import asyncio
from typing import Protocol

from kaiwen_agent.replay.models import ReplayFrame


class ReplayStore(Protocol):
    async def append(self, trace_id: str, frame: ReplayFrame) -> bool: ...

    async def list_frames(self, trace_id: str) -> list[ReplayFrame]: ...


class InMemoryReplayStore:
    def __init__(self) -> None:
        self._frames: dict[str, list[ReplayFrame]] = {}
        self._lock = asyncio.Lock()

    async def append(self, trace_id: str, frame: ReplayFrame) -> bool:
        async with self._lock:
            frames = self._frames.setdefault(trace_id, [])
            if any(item.event.id == frame.event.id for item in frames):
                return False
            if frame.sequence != len(frames):
                raise ValueError("Replay frame sequence must be contiguous")
            frames.append(frame.model_copy(deep=True))
            return True

    async def list_frames(self, trace_id: str) -> list[ReplayFrame]:
        async with self._lock:
            return [frame.model_copy(deep=True) for frame in self._frames.get(trace_id, [])]

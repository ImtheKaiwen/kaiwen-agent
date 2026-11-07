from __future__ import annotations

from typing import Protocol

from kaiwen_agent.events import AgentEvent
from kaiwen_agent.replay.models import ReplayResult, ReplayTrace
from kaiwen_agent.replay.store import ReplayStore


class ReplayObserver(Protocol):
    async def observe(self, event: AgentEvent, *, simulation: bool) -> None: ...


class ReplayPlayer:
    """Replays observations only; it never executes tools or side effects."""

    def __init__(self, *, store: ReplayStore) -> None:
        self.store = store

    async def load(self, trace_id: str) -> ReplayTrace:
        frames = await self.store.list_frames(trace_id)
        if not frames:
            raise KeyError(f"Unknown replay trace: {trace_id}")
        return ReplayTrace(trace_id=trace_id, frames=tuple(frames))

    async def play(
        self,
        trace_id: str,
        observer: ReplayObserver,
        *,
        simulation: bool = True,
    ) -> ReplayResult:
        trace = await self.load(trace_id)
        for frame in trace.frames:
            await observer.observe(frame.event.model_copy(deep=True), simulation=simulation)
        return ReplayResult(
            trace_id=trace_id,
            simulation=simulation,
            delivered_frames=len(trace.frames),
        )

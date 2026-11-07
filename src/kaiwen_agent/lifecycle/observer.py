from __future__ import annotations

from typing import Protocol

from kaiwen_agent.lifecycle.models import LifecycleObservationBatch, WakeTrigger


class LifecycleObserver(Protocol):
    async def observe(self, trigger: WakeTrigger) -> LifecycleObservationBatch: ...


class EmptyLifecycleObserver:
    async def observe(self, trigger: WakeTrigger) -> LifecycleObservationBatch:
        del trigger
        return LifecycleObservationBatch()

from __future__ import annotations

from kaiwen_agent.lifecycle.models import (
    LifecycleCycle,
    LifecycleRuntimeCard,
    LifecycleRuntimeStatus,
)
from kaiwen_agent.lifecycle.runtime import LifecycleRuntime


class LifecycleControlService:
    """Framework-neutral service for product-owned runtime HTTP routes."""

    def __init__(self, runtime: LifecycleRuntime) -> None:
        self.runtime = runtime

    async def status(self) -> LifecycleRuntimeStatus:
        return await self.runtime.status()

    async def card(self) -> LifecycleRuntimeCard:
        return await self.runtime.card()

    async def wake(self, *, reason: str) -> LifecycleCycle:
        return await self.runtime.manual_wake(reason=reason)

    async def pause(self) -> LifecycleRuntimeStatus:
        return await self.runtime.pause()

    async def resume(self) -> LifecycleRuntimeStatus:
        return await self.runtime.resume()

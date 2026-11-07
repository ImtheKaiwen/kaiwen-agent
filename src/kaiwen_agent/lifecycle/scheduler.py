from __future__ import annotations

from kaiwen_agent.lifecycle.models import LifecycleCycle
from kaiwen_agent.lifecycle.runtime import LifecycleRuntime


class LifecycleSchedulerAdapter:
    """Narrow adapter invoked by a host scheduler without owning a polling loop."""

    def __init__(self, runtime: LifecycleRuntime) -> None:
        self.runtime = runtime

    async def tick(self) -> LifecycleCycle | None:
        return await self.runtime.run_next_due()

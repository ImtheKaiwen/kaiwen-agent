from __future__ import annotations

import asyncio
from typing import cast

from kaiwen_agent.events import AgentEvent, EventSink, EventType, InMemoryEventSink
from kaiwen_agent.lifecycle.models import (
    LifecycleCycle,
    LifecycleCycleStatus,
    LifecycleRuntimeCard,
    LifecycleRuntimeRecord,
    LifecycleRuntimeStatus,
    LifecycleState,
    WakeTrigger,
)
from kaiwen_agent.lifecycle.observer import EmptyLifecycleObserver, LifecycleObserver
from kaiwen_agent.lifecycle.state import validate_lifecycle_transition
from kaiwen_agent.lifecycle.store import LifecycleStore
from kaiwen_agent.lifecycle.wakeup import WakeupManager
from kaiwen_agent.types import utc_now


class LifecyclePausedError(RuntimeError):
    pass


class LifecycleReasoningUnavailableError(RuntimeError):
    pass


class LifecycleRuntime:
    """Deterministic lifecycle skeleton without a model loop."""

    def __init__(
        self,
        *,
        agent_id: str,
        store: LifecycleStore,
        observer: LifecycleObserver | None = None,
        events: EventSink | None = None,
    ) -> None:
        if not agent_id:
            raise ValueError("agent_id must not be empty")
        self.agent_id = agent_id
        self.store = store
        self.observer = observer or EmptyLifecycleObserver()
        self.events = events or InMemoryEventSink()
        self.wakeups = WakeupManager(store=store, events=self.events)
        self._lock = asyncio.Lock()

    async def start(self) -> LifecycleRuntimeStatus:
        async with self._lock:
            runtime = await self._runtime_record()
            if runtime.state in {LifecycleState.SLEEPING, LifecycleState.PAUSED}:
                return await self._status(runtime)
            self._transition_runtime(runtime, LifecycleState.STARTING)
            await self.store.save_runtime(runtime)
            self._transition_runtime(runtime, LifecycleState.SLEEPING)
            await self.store.save_runtime(runtime)
            return await self._status(runtime)

    async def stop(self) -> LifecycleRuntimeStatus:
        async with self._lock:
            runtime = await self._runtime_record()
            if runtime.active_cycle_id is not None:
                raise RuntimeError("Cannot stop while a lifecycle cycle is active")
            if runtime.state != LifecycleState.STOPPED:
                self._transition_runtime(runtime, LifecycleState.STOPPED)
                runtime.paused_from = None
                await self.store.save_runtime(runtime)
            return await self._status(runtime)

    async def pause(self) -> LifecycleRuntimeStatus:
        async with self._lock:
            runtime = await self._runtime_record()
            if runtime.state == LifecycleState.PAUSED:
                return await self._status(runtime)
            if runtime.active_cycle_id is not None:
                raise RuntimeError("A running lifecycle cycle must reach a safe boundary")
            runtime.paused_from = runtime.state
            self._transition_runtime(runtime, LifecycleState.PAUSED)
            await self.store.save_runtime(runtime)
            return await self._status(runtime)

    async def resume(self) -> LifecycleRuntimeStatus:
        async with self._lock:
            runtime = await self._runtime_record()
            if runtime.state != LifecycleState.PAUSED:
                raise ValueError("Lifecycle runtime is not paused")
            target = runtime.paused_from or LifecycleState.SLEEPING
            self._transition_runtime(runtime, target)
            runtime.paused_from = None
            await self.store.save_runtime(runtime)
            return await self._status(runtime)

    async def status(self) -> LifecycleRuntimeStatus:
        return await self._status(await self._runtime_record())

    async def card(self) -> LifecycleRuntimeCard:
        status = await self.status()
        return LifecycleRuntimeCard(
            agent_id=status.agent_id,
            state=status.state,
            last_wake_at=status.last_woke_at,
            next_wake_at=(
                status.next_wakeup.scheduled_at if status.next_wakeup is not None else None
            ),
            active_cycle_id=status.active_cycle_id,
            paused=status.paused,
        )

    async def wake(self, trigger: WakeTrigger) -> LifecycleCycle:
        runtime = await self._runtime_record()
        if runtime.state == LifecycleState.STOPPED:
            await self.start()
        wakeup = await self.wakeups.schedule(
            agent_id=self.agent_id,
            trigger=trigger,
            scheduled_at=utc_now(),
            priority=100,
        )
        cycle = await self._run_wakeup(wakeup_id=wakeup.id)
        if cycle is None:
            raise RuntimeError("The immediate wakeup could not be claimed")
        return cycle

    async def manual_wake(
        self,
        *,
        reason: str,
        tenant_id: str | None = None,
        user_id: str | None = None,
        conversation_id: str | None = None,
    ) -> LifecycleCycle:
        runtime = await self._runtime_record()
        if runtime.state == LifecycleState.STOPPED:
            await self.start()
        wakeup = await self.wakeups.schedule_manual(
            agent_id=self.agent_id,
            reason=reason,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        cycle = await self._run_wakeup(wakeup_id=wakeup.id)
        if cycle is None:
            raise RuntimeError("The manual wakeup could not be claimed")
        return cycle

    async def run_next_due(self) -> LifecycleCycle | None:
        if (await self._runtime_record()).state == LifecycleState.STOPPED:
            await self.start()
        return await self._run_wakeup()

    async def _run_wakeup(self, *, wakeup_id: str | None = None) -> LifecycleCycle | None:
        async with self._lock:
            runtime = await self._runtime_record()
            if runtime.state == LifecycleState.PAUSED:
                if wakeup_id is None:
                    return None
                raise LifecyclePausedError("Lifecycle runtime is paused")
            if runtime.state != LifecycleState.SLEEPING:
                raise RuntimeError(f"Lifecycle runtime is not ready: {runtime.state}")
            if wakeup_id is None:
                wakeup = await self.wakeups.claim_due(agent_id=self.agent_id)
                if wakeup is None:
                    return None
            else:
                wakeup = await self.store.claim_wakeup(wakeup_id=wakeup_id, claimed_at=utc_now())
                if wakeup is None:
                    raise RuntimeError("The requested wakeup could not be claimed")
            cycle = LifecycleCycle(
                agent_id=self.agent_id,
                trigger=wakeup.trigger,
                wakeup_id=wakeup.id,
            )
            runtime.active_cycle_id = cycle.id
            runtime.last_woke_at = cycle.woke_at
            self._transition_runtime(runtime, LifecycleState.WAKING)
            await self.store.save_runtime(runtime)
            await self.store.save_cycle(cycle)
            await self._emit(cycle, "lifecycle.cycle.started")
            try:
                await self._advance(cycle, runtime, LifecycleState.OBSERVING)
                cycle.observe_started_at = utc_now()
                observations = await self.observer.observe(cycle.trigger)
                cycle.observations_count = len(observations.observations)
                if observations.requires_reasoning:
                    raise LifecycleReasoningUnavailableError(
                        "Reasoning is not part of the lifecycle skeleton"
                    )
                await self._advance(cycle, runtime, LifecycleState.PERSISTING)
                await self._advance(cycle, runtime, LifecycleState.SCHEDULING)
                cycle.status = LifecycleCycleStatus.COMPLETED
                cycle.completed_at = utc_now()
                await self._advance(cycle, runtime, LifecycleState.SLEEPING)
                runtime.active_cycle_id = None
                runtime.last_cycle_id = cycle.id
                await self.store.save_cycle(cycle)
                await self.store.save_runtime(runtime)
                await self.wakeups.complete(wakeup.id, cycle_id=cycle.id)
                await self._emit(cycle, "lifecycle.cycle.completed")
                return cycle
            except Exception as error:
                cycle.status = LifecycleCycleStatus.FAILED
                cycle.state = LifecycleState.FAILED
                cycle.completed_at = utc_now()
                cycle.error_code = type(error).__name__
                cycle.error_message = str(error)
                runtime.state = LifecycleState.FAILED
                runtime.active_cycle_id = None
                runtime.last_cycle_id = cycle.id
                runtime.updated_at = utc_now()
                await self.store.save_cycle(cycle)
                await self.store.save_runtime(runtime)
                await self.wakeups.fail(wakeup.id, cycle_id=cycle.id)
                await self._emit(cycle, "lifecycle.cycle.failed")
                raise

    async def _advance(
        self,
        cycle: LifecycleCycle,
        runtime: LifecycleRuntimeRecord,
        state: LifecycleState,
    ) -> None:
        validate_lifecycle_transition(runtime.state, state)
        runtime.state = state
        runtime.updated_at = utc_now()
        cycle.state = state
        await self.store.save_cycle(cycle)
        await self.store.save_runtime(runtime)
        event_name = {
            LifecycleState.OBSERVING: "lifecycle.cycle.observing",
            LifecycleState.PERSISTING: "lifecycle.cycle.persisting",
            LifecycleState.SCHEDULING: "lifecycle.cycle.scheduling",
            LifecycleState.SLEEPING: "lifecycle.cycle.sleeping",
        }.get(state)
        if event_name is not None:
            await self._emit(cycle, cast(EventType, event_name))

    async def _emit(self, cycle: LifecycleCycle, event_type: EventType) -> None:
        await self.events.append(
            AgentEvent(
                type=event_type,
                trace_id=cycle.trace_id,
                session_id=f"lifecycle:{self.agent_id}",
                payload={
                    "agent_id": self.agent_id,
                    "cycle_id": cycle.id,
                    "wakeup_id": cycle.wakeup_id,
                    "trigger_type": cycle.trigger.type.value,
                    "state": cycle.state.value,
                    "status": cycle.status.value,
                },
            )
        )

    async def _runtime_record(self) -> LifecycleRuntimeRecord:
        runtime = await self.store.get_runtime(self.agent_id)
        return runtime or LifecycleRuntimeRecord(agent_id=self.agent_id)

    async def _status(self, runtime: LifecycleRuntimeRecord) -> LifecycleRuntimeStatus:
        return LifecycleRuntimeStatus(
            agent_id=self.agent_id,
            state=runtime.state,
            active_cycle_id=runtime.active_cycle_id,
            last_cycle_id=runtime.last_cycle_id,
            last_woke_at=runtime.last_woke_at,
            next_wakeup=await self.wakeups.next_scheduled(agent_id=self.agent_id),
            paused=runtime.state == LifecycleState.PAUSED,
        )

    @staticmethod
    def _transition_runtime(runtime: LifecycleRuntimeRecord, state: LifecycleState) -> None:
        validate_lifecycle_transition(runtime.state, state)
        runtime.state = state
        runtime.updated_at = utc_now()

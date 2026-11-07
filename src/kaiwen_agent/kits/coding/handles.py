from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.events import AgentEvent, EventScope
from kaiwen_agent.kits.coding.providers import RoleUsageSummary
from kaiwen_agent.kits.coding.simple_config import (
    CodingExecutionBinding,
    CodingSimpleAPIError,
)
from kaiwen_agent.runtime import RuntimeIdentity, RuntimeRunRecord, RuntimeRunStatus
from kaiwen_agent.tasks import TaskStatus
from kaiwen_agent.workflow import WorkflowOutcome

_WAIT_RETURN_STATES = frozenset(
    {
        RuntimeRunStatus.BLOCKED,
        RuntimeRunStatus.COMPLETED,
        RuntimeRunStatus.FAILED,
        RuntimeRunStatus.CANCELLED,
        RuntimeRunStatus.PAUSED,
    }
)


class CodingRunResult(BaseModel):
    """Immutable projection of existing durable workflow state and evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    status: RuntimeRunStatus
    objective: str | None
    mode: str
    agent_or_team_ref: str
    workspace_ref: str
    tasks_total: int = Field(default=0, ge=0)
    tasks_completed: int = Field(default=0, ge=0)
    tasks_failed: int = Field(default=0, ge=0)
    quality_status: str | None = None
    evidence_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    usage: tuple[RoleUsageSummary, ...] = ()
    blockers: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    next_action: str | None = None


class CodingRunHandle:
    """Owner-scoped reference to existing durable state; never a second run store."""

    def __init__(
        self,
        *,
        run_id: str,
        objective: str | None,
        mode: str,
        agent_or_team_ref: str,
        workspace_ref: str,
        identity: RuntimeIdentity,
        execution: CodingExecutionBinding,
        task: asyncio.Task[WorkflowOutcome] | None = None,
    ) -> None:
        self.id = run_id
        self.objective = objective
        self.mode = mode
        self.agent_or_team_ref = agent_or_team_ref
        self.workspace_ref = workspace_ref
        self._identity = identity
        self._execution = execution
        self._task = task

    async def status(self) -> RuntimeRunRecord:
        record = await self._execution.runs.get(run_id=self.id, identity=self._identity)
        if record is None:
            raise KeyError("Coding run was not found in the trusted owner scope")
        return record

    async def wait(
        self,
        *,
        timeout: float | None = None,
        poll_interval: float = 0.1,
    ) -> CodingRunResult:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be greater than zero")
        if self._task is not None:
            try:
                outcome = await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)
            except TimeoutError as error:
                raise TimeoutError("Coding run did not reach a return state in time") from error
            return self._from_outcome(outcome)

        async def poll() -> CodingRunResult:
            while True:
                record = await self.status()
                if record.status in _WAIT_RETURN_STATES:
                    return self._from_record(record)
                await asyncio.sleep(poll_interval)

        try:
            return await asyncio.wait_for(poll(), timeout=timeout)
        except TimeoutError as error:
            raise TimeoutError("Coding run did not reach a return state in time") from error

    async def pause(self, *, safe_boundary: str = "host_requested") -> RuntimeRunRecord:
        control = self._execution.control
        if control is None:
            raise CodingSimpleAPIError(
                "pause_unsupported",
                "Configure scheduler-backed cooperative run control before pausing.",
            )
        await control.pause(run_id=self.id, identity=self._identity, safe_boundary=safe_boundary)
        return await self.status()

    async def cancel(self) -> RuntimeRunRecord:
        control = self._execution.control
        if control is None:
            raise CodingSimpleAPIError(
                "cancel_unsupported",
                "Configure scheduler-backed cooperative run control before cancelling.",
            )
        await control.cancel(run_id=self.id, identity=self._identity)
        return await self.status()

    async def events(
        self,
        *,
        after_id: str | None = None,
        limit: int = 100,
        follow: bool = False,
        poll_interval: float = 0.25,
    ) -> AsyncIterator[AgentEvent]:
        store = self._execution.event_store
        if store is None:
            raise CodingSimpleAPIError(
                "event_store_missing",
                "Configure an owner-scoped event store to read coding run events.",
            )
        if self._identity.tenant_id is None or self._identity.user_id is None:
            raise CodingSimpleAPIError(
                "workspace_not_authorized",
                "Run events require trusted tenant and user identity.",
            )
        cursor = after_id
        scope = EventScope(
            tenant_id=self._identity.tenant_id,
            user_id=self._identity.user_id,
            session_id=self._identity.session_id,
        )
        while True:
            events = await store.list_events_scoped(scope=scope, after_id=cursor, limit=limit)
            for event in events:
                cursor = event.id
                if _event_belongs_to_run(event.payload, self.id):
                    yield event
            if not follow:
                return
            if (await self.status()).status in _WAIT_RETURN_STATES and not events:
                return
            await asyncio.sleep(poll_interval)

    def _from_outcome(self, outcome: WorkflowOutcome) -> CodingRunResult:
        completed = sum(task.status == TaskStatus.COMPLETED for task in outcome.tasks)
        failed = sum(
            task.status in {TaskStatus.FAILED, TaskStatus.CANCELLED} for task in outcome.tasks
        )
        blockers: tuple[str, ...] = ()
        if outcome.quality_result is not None:
            blockers = outcome.quality_result.blockers
        if outcome.reason and outcome.status.value == "blocked":
            blockers = tuple(dict.fromkeys((*blockers, outcome.reason)))
        errors = (outcome.reason,) if outcome.reason and outcome.status.value == "failed" else ()
        return CodingRunResult(
            run_id=self.id,
            status=outcome.run.status,
            objective=self.objective,
            mode=self.mode,
            agent_or_team_ref=self.agent_or_team_ref,
            workspace_ref=self.workspace_ref,
            tasks_total=len(outcome.tasks),
            tasks_completed=completed,
            tasks_failed=failed,
            quality_status=(
                outcome.quality_result.status.value if outcome.quality_result is not None else None
            ),
            blockers=blockers,
            errors=errors,
            next_action=("Review blockers or approvals." if blockers else None),
        )

    def _from_record(self, record: RuntimeRunRecord) -> CodingRunResult:
        blockers = (record.blocked_reason,) if record.blocked_reason else ()
        limitations = (
            ("Detailed task/evidence projection requires the original workflow outcome.",)
            if record.status not in {RuntimeRunStatus.CREATED, RuntimeRunStatus.RUNNING}
            else ()
        )
        return CodingRunResult(
            run_id=record.id,
            status=record.status,
            objective=self.objective,
            mode=self.mode,
            agent_or_team_ref=self.agent_or_team_ref,
            workspace_ref=self.workspace_ref,
            blockers=blockers,
            limitations=limitations,
            next_action=("Reconnect a host workflow resumer." if limitations else None),
        )


def _event_belongs_to_run(payload: Any, run_id: str) -> bool:
    if isinstance(payload, dict):
        if payload.get("runtime_run_id") == run_id or payload.get("run_id") == run_id:
            return True
        return any(_event_belongs_to_run(value, run_id) for value in payload.values())
    if isinstance(payload, (list, tuple)):
        return any(_event_belongs_to_run(value, run_id) for value in payload)
    return False

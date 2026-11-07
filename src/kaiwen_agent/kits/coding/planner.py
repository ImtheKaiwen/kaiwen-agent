from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kaiwen_agent.workflow import WorkflowPlan


class StructuredPlanningBackend(Protocol):
    async def propose_plan(
        self,
        *,
        objective: str,
        context: dict[str, Any],
        correction: str | None,
    ) -> Any: ...


class PlanningConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_allowlist: frozenset[str] = Field(min_length=1)
    resource_allowlist: frozenset[str] = Field(default_factory=frozenset)
    capability_allowlist: frozenset[str] = Field(default_factory=frozenset)
    max_tasks: int = Field(default=20, ge=1, le=1_000)


class CodingPlanningError(ValueError):
    pass


class ObjectivePlanner:
    """Bounded model-to-WorkflowPlan adapter; it never executes plan content."""

    def __init__(
        self,
        backend: StructuredPlanningBackend,
        *,
        constraints: PlanningConstraints,
        max_corrections: int = 1,
    ) -> None:
        if max_corrections < 0 or max_corrections > 2:
            raise ValueError("max_corrections must be between zero and two")
        self.backend = backend
        self.constraints = constraints
        self.max_corrections = max_corrections

    async def plan(self, objective: str, *, context: dict[str, Any]) -> WorkflowPlan:
        if not objective.strip():
            raise CodingPlanningError("Objective must not be empty")
        correction: str | None = None
        for attempt in range(self.max_corrections + 1):
            candidate = await self.backend.propose_plan(
                objective=objective,
                context=dict(context),
                correction=correction,
            )
            try:
                plan = WorkflowPlan.model_validate(candidate)
                self._validate(plan, objective=objective)
            except (ValidationError, CodingPlanningError) as error:
                correction = _bounded_error(error)
                if attempt < self.max_corrections:
                    continue
                raise CodingPlanningError(
                    f"Planner did not produce a valid bounded plan: {correction}"
                ) from error
            return plan
        raise AssertionError("unreachable")

    def _validate(self, plan: WorkflowPlan, *, objective: str) -> None:
        if plan.objective.strip() != objective.strip():
            raise CodingPlanningError("Plan objective must exactly match the trusted objective")
        if len(plan.tasks) > self.constraints.max_tasks:
            raise CodingPlanningError("Plan exceeds the configured task limit")
        for task in plan.tasks:
            if task.worker_id not in self.constraints.worker_allowlist:
                raise CodingPlanningError(f"Worker is not allowlisted: {task.worker_id}")
            if not set(task.resource_keys) <= self.constraints.resource_allowlist:
                raise CodingPlanningError(f"Task requests unavailable resources: {task.id}")
            if not task.capabilities <= self.constraints.capability_allowlist:
                raise CodingPlanningError(f"Task requests unavailable capabilities: {task.id}")


def _bounded_error(error: Exception) -> str:
    return str(error).replace("\n", " ")[:1_000]

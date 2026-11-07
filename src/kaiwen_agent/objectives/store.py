from __future__ import annotations

import asyncio
from typing import Protocol

from kaiwen_agent.objectives.models import Objective, ObjectiveStatus


class ObjectiveStore(Protocol):
    async def save_objective(self, objective: Objective) -> None: ...

    async def get_objective(self, objective_id: str) -> Objective | None: ...

    async def list_objectives(
        self,
        *,
        tenant_id: str,
        user_id: str,
        status: ObjectiveStatus | None = None,
        limit: int = 100,
    ) -> list[Objective]: ...


class InMemoryObjectiveStore:
    def __init__(self) -> None:
        self._objectives: dict[str, Objective] = {}
        self._lock = asyncio.Lock()

    async def save_objective(self, objective: Objective) -> None:
        async with self._lock:
            self._objectives[objective.id] = objective.model_copy(deep=True)

    async def get_objective(self, objective_id: str) -> Objective | None:
        async with self._lock:
            objective = self._objectives.get(objective_id)
            return objective.model_copy(deep=True) if objective else None

    async def list_objectives(
        self,
        *,
        tenant_id: str,
        user_id: str,
        status: ObjectiveStatus | None = None,
        limit: int = 100,
    ) -> list[Objective]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        async with self._lock:
            items = [
                objective
                for objective in self._objectives.values()
                if objective.tenant_id == tenant_id
                and objective.user_id == user_id
                and (status is None or objective.status == status)
            ]
            items.sort(key=lambda item: (item.priority, item.updated_at), reverse=True)
            return [item.model_copy(deep=True) for item in items[:limit]]

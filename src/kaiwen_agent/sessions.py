from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.types import AgentRun, utc_now


class SessionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SessionStore(Protocol):
    async def save_session(self, session: SessionRecord) -> None: ...

    async def get_session(self, session_id: str) -> SessionRecord | None: ...


class RunStore(Protocol):
    async def save(self, run: AgentRun) -> None: ...

    async def get(self, run_id: str) -> AgentRun | None: ...


class InMemoryRunStore:
    def __init__(self) -> None:
        self._runs: dict[str, AgentRun] = {}

    async def save(self, run: AgentRun) -> None:
        self._runs[run.id] = run.model_copy(deep=True)

    async def get(self, run_id: str) -> AgentRun | None:
        run = self._runs.get(run_id)
        return run.model_copy(deep=True) if run else None

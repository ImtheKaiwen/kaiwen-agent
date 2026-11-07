from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.events import AgentEvent
from kaiwen_agent.types import utc_now


class ReplayFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=0)
    event: AgentEvent


class ReplayTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_id: str = Field(min_length=1)
    frames: tuple[ReplayFrame, ...]
    created_at: datetime = Field(default_factory=utc_now)


class ReplayResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_id: str
    simulation: bool
    delivered_frames: int = Field(ge=0)

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.context import AgentContext


class RunMode(StrEnum):
    CHAT = "chat"
    ACTION = "action"
    WORKFLOW = "workflow"


class AutonomyLevel(StrEnum):
    MANUAL = "manual"
    SUPERVISED = "supervised"
    AUTONOMOUS = "autonomous"


class RuntimeIdentity(BaseModel):
    """Trusted host identity for one runtime invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1, max_length=200)
    tenant_id: str | None = Field(default=None, min_length=1, max_length=200)
    user_id: str | None = Field(default=None, min_length=1, max_length=200)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=200)
    client_id: str | None = Field(default=None, min_length=1, max_length=200)
    trace_id: str | None = Field(default=None, min_length=1, max_length=200)
    environment: str = Field(default="development", min_length=1, max_length=100)
    permissions: frozenset[str] = Field(default_factory=frozenset)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_agent_context(
        self,
        *,
        agent_id: str,
        services: Mapping[str, Any],
        mode: RunMode,
        autonomy: AutonomyLevel,
    ) -> AgentContext:
        return AgentContext(
            session_id=self.session_id,
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            agent_id=agent_id,
            conversation_id=self.conversation_id,
            client_id=self.client_id,
            trace_id=self.trace_id,
            environment=self.environment,
            permissions=self.permissions,
            services=services,
            metadata={
                **self.metadata,
                "runtime_mode": mode.value,
                "autonomy_level": autonomy.value,
            },
        )

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AgentContext:
    session_id: str
    tenant_id: str | None = None
    user_id: str | None = None
    agent_id: str | None = None
    conversation_id: str | None = None
    client_id: str | None = None
    trace_id: str | None = None
    environment: str = "development"
    permissions: frozenset[str] = field(default_factory=frozenset)
    services: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

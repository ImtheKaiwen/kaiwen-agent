from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from kaiwen_agent.types import utc_now


class AuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"audit_{uuid4().hex}")
    trace_id: str
    session_id: str
    action: str
    outcome: Literal["succeeded", "failed", "denied"]
    details: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utc_now)


class AuditStore(Protocol):
    async def append_audit(self, record: AuditRecord) -> None: ...

    async def list_audit(self, *, trace_id: str | None = None) -> list[AuditRecord]: ...


class InMemoryAuditStore:
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    async def append_audit(self, record: AuditRecord) -> None:
        self.records.append(record.model_copy(deep=True))

    async def list_audit(self, *, trace_id: str | None = None) -> list[AuditRecord]:
        records = self.records
        if trace_id is not None:
            records = [record for record in records if record.trace_id == trace_id]
        return [record.model_copy(deep=True) for record in records]


class SecretRedactor:
    _secret_keys = re.compile(
        r"(^|_)(api_?key|authorization|cookie|password|secret|token)($|_)", re.IGNORECASE
    )
    _secret_values = (
        re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+=*", re.IGNORECASE),
    )

    def redact(self, value: Any, *, key: str | None = None) -> Any:
        if key is not None and self._secret_keys.search(key):
            return "[REDACTED]"
        if isinstance(value, SecretStr):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {
                item_key: self.redact(item_value, key=str(item_key))
                for item_key, item_value in value.items()
            }
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact(item) for item in value)
        if isinstance(value, str):
            redacted = value
            for pattern in self._secret_values:
                redacted = pattern.sub("[REDACTED]", redacted)
            return redacted
        return value


class RedactingAuditStore:
    def __init__(self, store: AuditStore, redactor: SecretRedactor | None = None) -> None:
        self.store = store
        self.redactor = redactor or SecretRedactor()

    async def append_audit(self, record: AuditRecord) -> None:
        clean = record.model_copy(update={"details": self.redactor.redact(record.details)})
        await self.store.append_audit(clean)

    async def list_audit(self, *, trace_id: str | None = None) -> list[AuditRecord]:
        return await self.store.list_audit(trace_id=trace_id)

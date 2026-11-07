from __future__ import annotations

import json
from typing import Any

from kaiwen_agent.audit import SecretRedactor
from kaiwen_agent.dead_letter.models import (
    DeadLetterContext,
    DeadLetterKind,
    DeadLetterRecord,
    DeadLetterStatus,
)
from kaiwen_agent.dead_letter.store import DeadLetterStore, InMemoryDeadLetterStore
from kaiwen_agent.types import utc_now


class DeadLetterManager:
    def __init__(
        self,
        *,
        store: DeadLetterStore | None = None,
        redactor: SecretRedactor | None = None,
        max_payload_bytes: int = 256 * 1024,
    ) -> None:
        if max_payload_bytes < 1:
            raise ValueError("max_payload_bytes must be positive")
        self.store = store or InMemoryDeadLetterStore()
        self.redactor = redactor or SecretRedactor()
        self.max_payload_bytes = max_payload_bytes

    async def enqueue(
        self,
        context: DeadLetterContext,
        *,
        kind: DeadLetterKind,
        reference_id: str,
        reason_code: str,
        reason: str,
        payload: dict[str, Any] | None = None,
        attempts: int = 0,
    ) -> DeadLetterRecord:
        clean = self.redactor.redact(payload or {})
        if not isinstance(clean, dict):  # pragma: no cover - input shape guarantees this
            raise TypeError("Redacted dead-letter payload must remain a mapping")
        try:
            encoded = json.dumps(clean, separators=(",", ":")).encode()
        except (TypeError, ValueError) as error:
            raise ValueError("Dead-letter payload must be JSON serializable") from error
        if len(encoded) > self.max_payload_bytes:
            raise ValueError("Dead-letter payload exceeds the configured size limit")
        clean_reason = self.redactor.redact(reason)
        if not isinstance(clean_reason, str):  # pragma: no cover - string input guarantees this
            raise TypeError("Redacted dead-letter reason must remain a string")
        record = DeadLetterRecord(
            tenant_id=context.tenant_id,
            owner_user_id=context.user_id,
            trace_id=context.trace_id,
            kind=kind,
            reference_id=reference_id,
            reason_code=reason_code,
            reason=clean_reason,
            payload=clean,
            attempts=attempts,
        )
        await self.store.save(record)
        return record

    async def get(self, context: DeadLetterContext, record_id: str) -> DeadLetterRecord:
        record = await self.store.get(record_id)
        if record is None or not _visible(record, context):
            raise KeyError("Dead-letter record not found")
        return record

    async def list(
        self,
        context: DeadLetterContext,
        *,
        status: DeadLetterStatus | None = None,
        limit: int = 100,
    ) -> list[DeadLetterRecord]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        return await self.store.list(
            tenant_id=context.tenant_id,
            owner_user_id=context.user_id,
            status=status,
            limit=limit,
        )

    async def mark_requeued(self, context: DeadLetterContext, record_id: str) -> DeadLetterRecord:
        record = await self.get(context, record_id)
        if record.status != DeadLetterStatus.PENDING:
            raise ValueError("Only pending dead-letter records can be requeued")
        updated = record.model_copy(
            update={
                "status": DeadLetterStatus.REQUEUED,
                "requeue_count": record.requeue_count + 1,
                "updated_at": utc_now(),
            }
        )
        await self.store.save(updated)
        return updated

    async def resolve(self, context: DeadLetterContext, record_id: str) -> DeadLetterRecord:
        record = await self.get(context, record_id)
        updated = record.model_copy(
            update={"status": DeadLetterStatus.RESOLVED, "updated_at": utc_now()}
        )
        await self.store.save(updated)
        return updated


def _visible(record: DeadLetterRecord, context: DeadLetterContext) -> bool:
    return record.tenant_id == context.tenant_id and (
        context.user_id is None or record.owner_user_id == context.user_id
    )

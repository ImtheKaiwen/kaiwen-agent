from __future__ import annotations

from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.artifacts import ArtifactContext, ArtifactCreate, ArtifactManager, ArtifactType
from kaiwen_agent.events import AgentEvent, EventSink, EventType
from kaiwen_agent.runtime import RuntimeIdentity, RuntimeVersionPins

CodingEventKind = Literal[
    "coding.discovery.started",
    "coding.discovery.completed",
    "coding.plan.created",
    "coding.plan.revised",
    "coding.task.assigned",
    "coding.file.changed",
    "coding.command.started",
    "coding.command.completed",
    "coding.verification.completed",
    "coding.context.compacted",
    "coding.checkpoint.saved",
    "coding.recovery.blocked",
    "coding.recovery.resumed",
    "coding.run.completed",
    "coding.run.blocked",
    "coding.run.failed",
]

_CANONICAL_EVENT: dict[CodingEventKind, EventType] = {
    "coding.discovery.started": "task.started",
    "coding.discovery.completed": "task.completed",
    "coding.plan.created": "task.created",
    "coding.plan.revised": "task.progress",
    "coding.task.assigned": "task.queued",
    "coding.file.changed": "tool.completed",
    "coding.command.started": "tool.started",
    "coding.command.completed": "tool.completed",
    "coding.verification.completed": "task.completed",
    "coding.context.compacted": "run.status",
    "coding.checkpoint.saved": "run.status",
    "coding.recovery.blocked": "run.status",
    "coding.recovery.resumed": "run.status",
    "coding.run.completed": "run.completed",
    "coding.run.blocked": "run.status",
    "coding.run.failed": "run.failed",
}


class CodingRunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    kit_version: int = Field(ge=1)
    prompt_checksums: dict[str, str]
    version_pins: RuntimeVersionPins
    plan_checksum: str = Field(min_length=64, max_length=64)
    workspace_id: str
    project_fingerprint: str = Field(min_length=64, max_length=64)
    role_refs: tuple[str, ...] = ()


async def persist_run_manifest(
    manifest: CodingRunManifest,
    *,
    artifacts: ArtifactManager,
    context: ArtifactContext,
) -> str:
    content = manifest.model_dump_json().encode()
    metadata = await artifacts.create(
        context,
        ArtifactCreate(
            type=ArtifactType.ANALYSIS,
            mime_type="application/json",
            content=content,
            labels={
                "artifact_kind": "coding_run_manifest",
                "plan_checksum": manifest.plan_checksum,
                "project_fingerprint": manifest.project_fingerprint,
            },
        ),
    )
    return metadata.id


class CodingEventEmitter:
    """Maps kit observations into existing canonical AgentEvent types."""

    def __init__(self, sink: EventSink) -> None:
        self.sink = sink

    async def emit(
        self,
        kind: CodingEventKind,
        *,
        identity: RuntimeIdentity,
        payload: dict[str, Any],
    ) -> None:
        await self.sink.append(
            AgentEvent(
                type=_CANONICAL_EVENT[kind],
                trace_id=identity.trace_id
                or f"trace_{sha256(identity.session_id.encode()).hexdigest()[:16]}",
                session_id=identity.session_id,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                payload={"coding_event": kind, **payload},
            )
        )

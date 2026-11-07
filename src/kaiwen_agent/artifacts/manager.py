from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from uuid import uuid4

from kaiwen_agent.artifacts.models import (
    Artifact,
    ArtifactContext,
    ArtifactCreate,
    ArtifactMetadata,
)
from kaiwen_agent.artifacts.store import ArtifactStore, InMemoryArtifactStore, is_expired
from kaiwen_agent.events import AgentEvent, EventSink
from kaiwen_agent.types import utc_now


class ArtifactNotFoundError(LookupError):
    pass


class ArtifactIntegrityError(RuntimeError):
    pass


class ArtifactManager:
    def __init__(
        self,
        *,
        store: ArtifactStore | None = None,
        events: EventSink | None = None,
        max_artifact_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        if max_artifact_bytes < 1:
            raise ValueError("max_artifact_bytes must be positive")
        self.store = store or InMemoryArtifactStore()
        self.events = events
        self.max_artifact_bytes = max_artifact_bytes

    async def create(
        self,
        context: ArtifactContext,
        request: ArtifactCreate,
        *,
        now: datetime | None = None,
    ) -> ArtifactMetadata:
        observed_at = now or utc_now()
        if len(request.content) > self.max_artifact_bytes:
            raise ValueError("Artifact exceeds the configured size limit")
        if request.expires_at is not None:
            if request.expires_at.tzinfo is None:
                raise ValueError("expires_at must be timezone-aware")
            if request.expires_at <= observed_at:
                raise ValueError("expires_at must be in the future")
        artifact_id = f"artifact_{uuid4().hex}"
        metadata = ArtifactMetadata(
            id=artifact_id,
            tenant_id=context.tenant_id,
            owner_user_id=context.user_id,
            type=request.type,
            mime_type=request.mime_type,
            storage_ref=f"artifact://{artifact_id}",
            size_bytes=len(request.content),
            checksum_sha256=sha256(request.content).hexdigest(),
            run_id=context.run_id,
            job_id=context.job_id,
            labels=request.labels,
            created_at=observed_at,
            expires_at=request.expires_at,
        )
        stored = await self.store.put(Artifact(metadata=metadata, content=request.content))
        metadata = stored.metadata
        if (
            metadata.id != artifact_id
            or metadata.tenant_id != context.tenant_id
            or metadata.owner_user_id != context.user_id
            or metadata.size_bytes != len(request.content)
            or metadata.checksum_sha256 != sha256(request.content).hexdigest()
        ):
            await self.store.delete(artifact_id)
            raise ValueError("Artifact store changed protected metadata")
        if self.events is not None:
            try:
                await self.events.append(
                    AgentEvent(
                        type="artifact.created",
                        trace_id=context.trace_id,
                        session_id=context.session_id,
                        tenant_id=context.tenant_id,
                        user_id=context.user_id,
                        payload={
                            "artifact_id": metadata.id,
                            "tenant_id": metadata.tenant_id,
                            "owner_user_id": metadata.owner_user_id,
                            "type": metadata.type.value,
                            "mime_type": metadata.mime_type,
                            "size_bytes": metadata.size_bytes,
                            "run_id": metadata.run_id,
                            "job_id": metadata.job_id,
                        },
                    )
                )
            except Exception:
                await self.store.delete(artifact_id)
                raise
        return metadata

    async def read(
        self,
        context: ArtifactContext,
        artifact_id: str,
        *,
        now: datetime | None = None,
    ) -> Artifact:
        artifact = await self.store.get(artifact_id)
        if artifact is None or not _owned_by(artifact.metadata, context):
            raise ArtifactNotFoundError("Artifact not found")
        if is_expired(artifact.metadata, now or utc_now()):
            await self.store.delete(artifact_id)
            raise ArtifactNotFoundError("Artifact not found")
        if (
            artifact.metadata.size_bytes != len(artifact.content)
            or artifact.metadata.checksum_sha256 != sha256(artifact.content).hexdigest()
        ):
            raise ArtifactIntegrityError("Artifact content failed integrity validation")
        return artifact

    async def delete(self, context: ArtifactContext, artifact_id: str) -> None:
        await self.read(context, artifact_id)
        await self.store.delete(artifact_id)

    async def list(
        self, context: ArtifactContext, *, limit: int = 100, now: datetime | None = None
    ) -> list[ArtifactMetadata]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        observed_at = now or utc_now()
        records = await self.store.list_metadata(
            tenant_id=context.tenant_id,
            owner_user_id=context.user_id,
            limit=limit,
        )
        visible: list[ArtifactMetadata] = []
        for metadata in records:
            if is_expired(metadata, observed_at):
                await self.store.delete(metadata.id)
            else:
                visible.append(metadata)
        return visible


def _owned_by(metadata: ArtifactMetadata, context: ArtifactContext) -> bool:
    return metadata.tenant_id == context.tenant_id and metadata.owner_user_id == context.user_id

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Protocol

from kaiwen_agent.artifacts.models import Artifact, ArtifactMetadata


class ArtifactStore(Protocol):
    async def put(self, artifact: Artifact) -> Artifact: ...

    async def get(self, artifact_id: str) -> Artifact | None: ...

    async def delete(self, artifact_id: str) -> None: ...

    async def list_metadata(
        self, *, tenant_id: str, owner_user_id: str, limit: int
    ) -> list[ArtifactMetadata]: ...


class InMemoryArtifactStore:
    def __init__(self) -> None:
        self._artifacts: dict[str, Artifact] = {}
        self._lock = asyncio.Lock()

    async def put(self, artifact: Artifact) -> Artifact:
        async with self._lock:
            if artifact.metadata.id in self._artifacts:
                raise ValueError(f"Artifact already exists: {artifact.metadata.id}")
            self._artifacts[artifact.metadata.id] = artifact.model_copy(deep=True)
            return artifact.model_copy(deep=True)

    async def get(self, artifact_id: str) -> Artifact | None:
        async with self._lock:
            artifact = self._artifacts.get(artifact_id)
            return artifact.model_copy(deep=True) if artifact is not None else None

    async def delete(self, artifact_id: str) -> None:
        async with self._lock:
            self._artifacts.pop(artifact_id, None)

    async def list_metadata(
        self, *, tenant_id: str, owner_user_id: str, limit: int
    ) -> list[ArtifactMetadata]:
        async with self._lock:
            records = [
                artifact.metadata
                for artifact in self._artifacts.values()
                if artifact.metadata.tenant_id == tenant_id
                and artifact.metadata.owner_user_id == owner_user_id
            ]
            records.sort(key=lambda item: item.created_at, reverse=True)
            return [item.model_copy(deep=True) for item in records[:limit]]


def is_expired(metadata: ArtifactMetadata, now: datetime) -> bool:
    return metadata.expires_at is not None and metadata.expires_at <= now

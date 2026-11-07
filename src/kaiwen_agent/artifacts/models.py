from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.types import utc_now


class ArtifactType(StrEnum):
    REPORT = "report"
    FILE = "file"
    CODE = "code"
    TRANSCRIPT = "transcript"
    ANALYSIS = "analysis"
    DATASET = "dataset"
    IMAGE = "image"
    DOCUMENT = "document"
    JOB_RESULT = "job_result"


class ArtifactContext(BaseModel):
    """Trusted ownership context supplied by the host, never by model arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    run_id: str | None = None
    job_id: str | None = None


class ArtifactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: ArtifactType
    mime_type: str = Field(min_length=1)
    content: bytes
    expires_at: datetime | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class ArtifactMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    owner_user_id: str = Field(min_length=1)
    type: ArtifactType
    mime_type: str = Field(min_length=1)
    storage_ref: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    checksum_sha256: str = Field(min_length=64, max_length=64)
    run_id: str | None = None
    job_id: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metadata: ArtifactMetadata
    content: bytes

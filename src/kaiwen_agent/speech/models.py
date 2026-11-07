from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SpeechRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    UNKNOWN = "unknown"


class AudioFormat(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mime_type: str = Field(min_length=1, max_length=100)
    sample_rate_hz: int = Field(ge=8_000, le=192_000)
    channels: int = Field(default=1, ge=1, le=8)


class SpeechContext(BaseModel):
    """Trusted identity and explicit permission for ephemeral audio processing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    audio_processing_allowed: bool = False


class SpeechRecognitionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    audio_format: AudioFormat
    language: str | None = Field(default=None, min_length=2, max_length=16)


class SpeechSynthesisConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    audio_format: AudioFormat
    voice: str | None = Field(default=None, min_length=1, max_length=100)
    language: str | None = Field(default=None, min_length=2, max_length=16)


class TranscriptSegment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    role: SpeechRole = SpeechRole.UNKNOWN
    speaker_id: str | None = Field(default=None, max_length=200)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    language: str | None = Field(default=None, min_length=2, max_length=16)
    confidence: float | None = Field(default=None, ge=0, le=1)
    is_final: bool = True

    @model_validator(mode="after")
    def validate_timing(self) -> TranscriptSegment:
        if self.start_ms is not None and self.end_ms is not None and self.end_ms < self.start_ms:
            raise ValueError("Transcript segment end_ms cannot precede start_ms")
        return self


class AudioArtifactRef(BaseModel):
    """Metadata-only reference; raw audio is never embedded in this contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    consent_recorded: bool
    retention_until: datetime | None = None


class SpeechPipelineResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    transcript: tuple[TranscriptSegment, ...]
    response_text: str
    audio: bytes
    audio_format: AudioFormat

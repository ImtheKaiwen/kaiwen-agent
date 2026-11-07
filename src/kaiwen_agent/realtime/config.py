from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RealtimeChannel(StrEnum):
    WEB_VOICE = "web_voice"
    MOBILE_VOICE = "mobile_voice"
    PHONE = "phone"


class RealtimeMode(StrEnum):
    INTERACTIVE = "interactive"
    OBJECTIVE_DRIVEN = "objective_driven"


class TurnDetectionMode(StrEnum):
    SERVER_VAD = "server_vad"
    SEMANTIC_VAD = "semantic_vad"
    MANUAL = "manual_push_to_talk"


class RealtimeSessionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    mode: RealtimeMode = RealtimeMode.INTERACTIVE
    channel: RealtimeChannel = RealtimeChannel.WEB_VOICE
    language: str = Field(default="en", min_length=2, max_length=16)
    turn_detection: TurnDetectionMode = TurnDetectionMode.SERVER_VAD
    interruption: bool = True
    transcription: bool = True
    max_session_seconds: int = Field(default=3600, gt=0, le=86_400)
    task_delegation: bool = True
    require_objective: bool = False

    @model_validator(mode="after")
    def validate_objective_mode(self) -> RealtimeSessionConfig:
        if self.require_objective and self.mode != RealtimeMode.OBJECTIVE_DRIVEN:
            raise ValueError("require_objective needs objective_driven mode")
        return self


class RealtimeLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_global_sessions: int = Field(default=500, ge=1)
    max_sessions_per_tenant: int = Field(default=20, ge=1)
    max_sessions_per_user: int = Field(default=3, ge=1)

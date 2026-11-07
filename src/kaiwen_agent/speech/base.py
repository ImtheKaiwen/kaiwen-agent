from __future__ import annotations

from typing import Protocol

from kaiwen_agent.speech.models import (
    SpeechContext,
    SpeechRecognitionConfig,
    SpeechSynthesisConfig,
    TranscriptSegment,
)


class SpeechRecognizer(Protocol):
    async def transcribe(
        self,
        audio: bytes,
        *,
        context: SpeechContext,
        config: SpeechRecognitionConfig,
    ) -> tuple[TranscriptSegment, ...]: ...


class SpeechSynthesizer(Protocol):
    async def synthesize(
        self,
        text: str,
        *,
        context: SpeechContext,
        config: SpeechSynthesisConfig,
    ) -> bytes: ...


class SpeechTextHandler(Protocol):
    async def respond(self, text: str, *, context: SpeechContext) -> str: ...

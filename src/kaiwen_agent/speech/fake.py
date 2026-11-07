from __future__ import annotations

from kaiwen_agent.speech.models import (
    SpeechContext,
    SpeechRecognitionConfig,
    SpeechRole,
    SpeechSynthesisConfig,
    TranscriptSegment,
)


class FakeSpeechRecognizer:
    def __init__(self, segments: tuple[TranscriptSegment, ...]) -> None:
        self.segments = segments
        self.calls: list[tuple[bytes, SpeechContext, SpeechRecognitionConfig]] = []

    async def transcribe(
        self,
        audio: bytes,
        *,
        context: SpeechContext,
        config: SpeechRecognitionConfig,
    ) -> tuple[TranscriptSegment, ...]:
        self.calls.append((audio, context, config))
        return self.segments


class FakeSpeechSynthesizer:
    def __init__(self, *, prefix: bytes = b"fake-audio:") -> None:
        self.prefix = prefix
        self.calls: list[tuple[str, SpeechContext, SpeechSynthesisConfig]] = []

    async def synthesize(
        self,
        text: str,
        *,
        context: SpeechContext,
        config: SpeechSynthesisConfig,
    ) -> bytes:
        self.calls.append((text, context, config))
        return self.prefix + text.encode()


def user_segment(text: str, *, language: str | None = None) -> TranscriptSegment:
    return TranscriptSegment(
        text=text,
        role=SpeechRole.USER,
        language=language,
        is_final=True,
    )

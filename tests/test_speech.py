from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from kaiwen_agent.speech import (
    AudioFormat,
    FakeSpeechRecognizer,
    FakeSpeechSynthesizer,
    SpeechContext,
    SpeechPermissionError,
    SpeechPipeline,
    SpeechRecognitionConfig,
    SpeechRole,
    SpeechSynthesisConfig,
    TranscriptSegment,
    user_segment,
)


def _context(*, allowed: bool) -> SpeechContext:
    return SpeechContext(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        conversation_id="conversation-1",
        trace_id="trace-1",
        audio_processing_allowed=allowed,
    )


def _format() -> AudioFormat:
    return AudioFormat(mime_type="audio/pcm", sample_rate_hz=24_000)


class EchoTextHandler:
    def __init__(self) -> None:
        self.calls = []

    async def respond(self, text: str, *, context: SpeechContext) -> str:
        self.calls.append((text, context))
        return f"Response: {text}"


def test_speech_pipeline_keeps_identity_and_audio_ephemeral() -> None:
    async def scenario() -> None:
        recognizer = FakeSpeechRecognizer(
            (
                TranscriptSegment(text="partial", role=SpeechRole.USER, is_final=False),
                user_segment("Hello", language="en"),
                user_segment("world", language="en"),
            )
        )
        synthesizer = FakeSpeechSynthesizer()
        handler = EchoTextHandler()
        pipeline = SpeechPipeline(
            recognizer=recognizer,
            text_handler=handler,
            synthesizer=synthesizer,
        )
        context = _context(allowed=True)
        result = await pipeline.process(
            b"raw-ephemeral-audio",
            context=context,
            recognition=SpeechRecognitionConfig(audio_format=_format(), language="en"),
            synthesis=SpeechSynthesisConfig(audio_format=_format(), language="en"),
        )
        assert result.response_text == "Response: Hello world"
        assert result.audio == b"fake-audio:Response: Hello world"
        assert handler.calls == [("Hello world", context)]
        assert recognizer.calls[0][1] == context
        assert synthesizer.calls[0][1] == context
        assert "raw-ephemeral-audio" not in repr(result)

    asyncio.run(scenario())


def test_speech_pipeline_requires_explicit_audio_permission() -> None:
    async def scenario() -> None:
        pipeline = SpeechPipeline(
            recognizer=FakeSpeechRecognizer((user_segment("Hello"),)),
            text_handler=EchoTextHandler(),
            synthesizer=FakeSpeechSynthesizer(),
        )
        with pytest.raises(SpeechPermissionError):
            await pipeline.process(
                b"audio",
                context=_context(allowed=False),
                recognition=SpeechRecognitionConfig(audio_format=_format()),
                synthesis=SpeechSynthesisConfig(audio_format=_format()),
            )

    asyncio.run(scenario())


def test_transcript_timing_is_ordered() -> None:
    with pytest.raises(ValidationError, match="end_ms"):
        TranscriptSegment(text="invalid", start_ms=20, end_ms=10)

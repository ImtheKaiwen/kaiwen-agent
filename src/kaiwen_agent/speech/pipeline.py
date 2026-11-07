from __future__ import annotations

from kaiwen_agent.speech.base import SpeechRecognizer, SpeechSynthesizer, SpeechTextHandler
from kaiwen_agent.speech.models import (
    SpeechContext,
    SpeechPipelineResult,
    SpeechRecognitionConfig,
    SpeechSynthesisConfig,
)


class SpeechPermissionError(PermissionError):
    pass


class SpeechPipeline:
    """Ephemeral STT -> injected text runtime -> TTS composition."""

    def __init__(
        self,
        *,
        recognizer: SpeechRecognizer,
        text_handler: SpeechTextHandler,
        synthesizer: SpeechSynthesizer,
    ) -> None:
        self.recognizer = recognizer
        self.text_handler = text_handler
        self.synthesizer = synthesizer

    async def process(
        self,
        audio: bytes,
        *,
        context: SpeechContext,
        recognition: SpeechRecognitionConfig,
        synthesis: SpeechSynthesisConfig,
    ) -> SpeechPipelineResult:
        if not context.audio_processing_allowed:
            raise SpeechPermissionError("Audio processing is not allowed for this context")
        if not audio:
            raise ValueError("audio must not be empty")
        segments = await self.recognizer.transcribe(
            audio,
            context=context,
            config=recognition,
        )
        final_text = " ".join(
            segment.text.strip()
            for segment in segments
            if segment.is_final and segment.text.strip()
        )
        if not final_text:
            raise ValueError("Speech recognizer returned no final transcript")
        response_text = await self.text_handler.respond(final_text, context=context)
        response_audio = await self.synthesizer.synthesize(
            response_text,
            context=context,
            config=synthesis,
        )
        return SpeechPipelineResult(
            transcript=segments,
            response_text=response_text,
            audio=response_audio,
            audio_format=synthesis.audio_format,
        )

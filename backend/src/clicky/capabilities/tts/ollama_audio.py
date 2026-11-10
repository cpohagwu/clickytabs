"""OllamaAudioTTS: `lfm3-audio:2b` through Ollama's OpenAI-compatible POST /v1/audio/speech.

Requested as raw PCM (24 kHz, 16-bit, mono), which Ollama streams while it generates, so playback starts on the first
chunk. Measured on the reference laptop (CPU-pinned `clicky-lfm3-audio-cpu`, warm): first audio ~0.45 s, real-time
factor ~0.95. Same model as STT, so one runner serves both.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from clicky.capabilities.base import AudioChunk, Health, ProviderSpec
from clicky.capabilities.ollama_api import OllamaError
from clicky.capabilities.options import OllamaAudioTTSOptions
from clicky.capabilities.speech_base import PCM_MIME, raise_for_status
from clicky.speech.audio import PcmAligner

if TYPE_CHECKING:
    from clicky.capabilities.context import ProviderContext

SPEECH_TIMEOUT_S = 120.0


class OllamaAudioTTS:
    def __init__(self, spec: ProviderSpec, *, model: str | None, options: OllamaAudioTTSOptions,
                 context: ProviderContext) -> None:
        self.spec = spec
        self.model = model or ""
        self.options = options
        self._ctx = context

    async def health(self) -> Health:
        try:
            if not await self._ctx.ollama.has_model(self.model):
                return Health(False, f"{self.model} is not pulled", self._ctx.pull_hint(self.model))
        except OllamaError as exc:
            return Health(False, str(exc), "start Ollama (ollama serve) or check [ollama] host")
        return Health(True)

    async def aclose(self) -> None:
        return None

    async def warm_up(self) -> None:
        """Load the model (or refresh its keep-alive). The /v1/audio endpoints keep the keep-alive set here."""
        await self._ctx.ollama.post("/api/generate", {"model": self.model, "keep_alive": self.options.keep_alive})

    async def synthesize(self, text: str, *, voice: str | None = None) -> AsyncIterator[AudioChunk]:
        body: dict[str, object] = {"model": self.model, "input": text, "response_format": "pcm"}
        if voice or self.options.voice:
            body["voice"] = voice or self.options.voice
        if self.options.speed != 1.0:
            body["speed"] = self.options.speed
        aligner = PcmAligner()
        first = True
        async with self._ctx.http.stream("POST", f"{self._ctx.ollama.host}/v1/audio/speech", json=body,
                                         timeout=SPEECH_TIMEOUT_S) as response:
            if not response.is_success:
                await response.aread()
                raise_for_status(response, "Ollama speech")
            async for raw in response.aiter_bytes():
                if pcm := aligner.push(raw):
                    yield AudioChunk(pcm, mime=PCM_MIME if first else None,
                                     sample_rate=self.options.sample_rate if first else None)
                    first = False

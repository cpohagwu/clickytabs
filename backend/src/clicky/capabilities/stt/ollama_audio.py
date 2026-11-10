"""OllamaAudioSTT: `lfm3-audio:2b` through Ollama's OpenAI-compatible POST /v1/audio/transcriptions.

The browser's webm/opus recording is decoded to 16 kHz mono WAV with PyAV (speech/audio.py, `local` extra); silent
recordings never reach the model and silence around the speech is trimmed (speech/endpointing.py). On cpu/gpu-4gb
the CPU-pinned derived model `clicky-lfm3-audio-cpu` is used so the brain keeps the GPU. Measured on the reference
laptop: ~0.85 s for a 2.6 s question, warm.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from clicky.capabilities.base import Health, ProviderSpec
from clicky.capabilities.ollama_api import OllamaError
from clicky.capabilities.options import OllamaAudioSTTOptions
from clicky.capabilities.speech_base import BatchSTTStream, raise_for_status
from clicky.speech.audio import STT_RATE, pcm_to_wav

if TYPE_CHECKING:
    from clicky.capabilities.context import ProviderContext

TRANSCRIBE_TIMEOUT_S = 60.0


class OllamaAudioSTT:
    def __init__(self, spec: ProviderSpec, *, model: str | None, options: OllamaAudioSTTOptions,
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

    async def open_stream(self, *, mime: str, language: str | None = "en") -> BatchSTTStream:
        return BatchSTTStream(self._transcribe, language=self.options.language or language)

    async def _transcribe(self, pcm16k: bytes) -> str:
        form: dict[str, str] = {"model": self.model, "response_format": "json"}
        if self.options.language:
            form["language"] = self.options.language
        if self.options.prompt:
            form["prompt"] = self.options.prompt
        if self.options.temperature is not None:
            form["temperature"] = str(self.options.temperature)
        response = await self._ctx.http.post(
            f"{self._ctx.ollama.host}/v1/audio/transcriptions", data=form,
            files={"file": ("speech.wav", pcm_to_wav(pcm16k, STT_RATE), "audio/wav")}, timeout=TRANSCRIBE_TIMEOUT_S)
        raise_for_status(response, "Ollama transcription")
        return str(response.json().get("text", ""))

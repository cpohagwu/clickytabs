"""OpenAITTS: OpenAI speech (`gpt-4o-mini-tts`) over plain httpx (no SDK).

Requested as `pcm` (24 kHz, 16-bit signed little-endian, no header), which the API streams with chunked transfer
encoding, so playback starts on the first chunk. `instructions` steers delivery; `marin` and `cedar` are OpenAI's
recommended voices. The API key comes from settings, never os.environ.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from clicky.capabilities.base import AudioChunk, Health, ProviderSpec
from clicky.capabilities.options import OpenAITTSOptions
from clicky.capabilities.speech_base import PCM_MIME, raise_for_status
from clicky.speech.audio import PcmAligner

if TYPE_CHECKING:
    from clicky.capabilities.context import ProviderContext

PCM_RATE = 24_000
SPEECH_TIMEOUT_S = 60.0


# The built-in voices of gpt-4o-mini-tts (OpenAI text-to-speech guide); marin and cedar are the recommended ones.
OPENAI_VOICES = ("marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage",
                 "shimmer", "verse")


class OpenAITTS:
    def __init__(self, spec: ProviderSpec, *, model: str | None, options: OpenAITTSOptions,
                 context: ProviderContext) -> None:
        self.spec = spec
        self.model = model or "gpt-4o-mini-tts"
        self.options = options
        self._ctx = context

    async def health(self) -> Health:
        return Health(True)

    async def aclose(self) -> None:
        return None

    async def voices(self) -> list[tuple[str, str]]:
        return [(name, name.capitalize()) for name in OPENAI_VOICES]

    async def synthesize(self, text: str, *, voice: str | None = None) -> AsyncIterator[AudioChunk]:
        key = self._ctx.settings.secret_value("openai_api_key") or ""
        body: dict[str, object] = {"model": self.model, "input": text, "voice": voice or self.options.voice,
                                   "response_format": "pcm"}
        if self.options.instructions:
            body["instructions"] = self.options.instructions
        if self.options.speed != 1.0:
            body["speed"] = self.options.speed
        aligner = PcmAligner()
        first = True
        async with self._ctx.http.stream("POST", f"{self.options.base_url.rstrip('/')}/audio/speech", json=body,
                                         headers={"Authorization": f"Bearer {key}"},
                                         timeout=SPEECH_TIMEOUT_S) as response:
            if not response.is_success:
                await response.aread()
                raise_for_status(response, "OpenAI speech")
            async for raw in response.aiter_bytes():
                if pcm := aligner.push(raw):
                    yield AudioChunk(pcm, mime=PCM_MIME if first else None, sample_rate=PCM_RATE if first else None)
                    first = False

"""ElevenLabsTTS: `eleven_flash_v2_5` (~100 ms server-side) over plain httpx (no SDK).

With `with_timestamps` (default) it calls POST /v1/text-to-speech/{voice_id}/stream/with-timestamps, a stream of JSON
objects each holding `audio_base64` and an optional `alignment` {characters, character_start_times_seconds,
character_end_times_seconds}; the alignment gives exact per-character times, which drive the directive schedule.
Audio is requested as raw PCM (`pcm_24000` by default) so the extension can stream it. The API key comes from
settings, never os.environ.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING, Any

from clicky.capabilities.base import AudioChunk, Health, ProviderSpec
from clicky.capabilities.options import ElevenLabsOptions
from clicky.capabilities.speech_base import PCM_MIME, raise_for_status
from clicky.speech.audio import PcmAligner, pcm_duration_ms

if TYPE_CHECKING:
    from clicky.capabilities.context import ProviderContext

SPEECH_TIMEOUT_S = 60.0
VOICES_TTL_S = 600.0          # the voice list is cached: listing it costs no characters, but it is still a request


class JsonStreamDecoder:
    """Splits a byte stream of concatenated / newline-delimited JSON objects into objects."""

    def __init__(self) -> None:
        self._buffer = ""
        self._decoder = json.JSONDecoder()

    def push(self, text: str) -> Iterator[dict[str, Any]]:
        self._buffer += text
        while True:
            stripped = self._buffer.lstrip()
            if not stripped:
                self._buffer = ""
                return
            try:
                obj, end = self._decoder.raw_decode(stripped)
            except json.JSONDecodeError:
                self._buffer = stripped
                return                            # incomplete object: wait for more bytes
            self._buffer = stripped[end:]
            if isinstance(obj, dict):
                yield obj


def alignment_tuples(alignment: dict[str, Any] | None, offset_ms: float) -> tuple[tuple[str, float, float], ...] | None:
    """(char, start_ms, end_ms) from an ElevenLabs alignment object, shifted by `offset_ms`."""
    if not alignment:
        return None
    chars = alignment.get("characters") or []
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    if not chars or len(chars) != len(starts) or len(starts) != len(ends):
        return None
    return tuple((c, s * 1000 + offset_ms, e * 1000 + offset_ms) for c, s, e in zip(chars, starts, ends, strict=True))


class ElevenLabsTTS:
    def __init__(self, spec: ProviderSpec, *, model: str | None, options: ElevenLabsOptions,
                 context: ProviderContext) -> None:
        self.spec = spec
        self.model = model or "eleven_flash_v2_5"
        self.options = options
        self._ctx = context
        self.sample_rate = int(options.output_format.split("_")[1])
        self._voices: tuple[float, list[tuple[str, str]]] | None = None

    async def health(self) -> Health:
        return Health(True)

    async def aclose(self) -> None:
        return None

    async def voices(self) -> list[tuple[str, str]]:
        """The account's voices (GET /v1/voices), for the options page. Free accounts can use the premade ones."""
        now = time.monotonic()
        if self._voices is not None and now - self._voices[0] < VOICES_TTL_S:
            return self._voices[1]
        key = self._ctx.settings.secret_value("elevenlabs_api_key") or ""
        response = await self._ctx.http.get(f"{self.options.base_url.rstrip('/')}/v1/voices",
                                            headers={"xi-api-key": key}, timeout=10.0)
        response.raise_for_status()
        listed = [(str(v["voice_id"]), str(v.get("name") or v["voice_id"]))
                  for v in response.json().get("voices", []) if isinstance(v, dict) and v.get("voice_id")]
        self._voices = (now, listed)
        return listed

    async def synthesize(self, text: str, *, voice: str | None = None) -> AsyncIterator[AudioChunk]:
        key = self._ctx.settings.secret_value("elevenlabs_api_key") or ""
        voice_id = voice or self.options.voice_id
        suffix = "stream/with-timestamps" if self.options.with_timestamps else "stream"
        url = f"{self.options.base_url.rstrip('/')}/v1/text-to-speech/{voice_id}/{suffix}"
        aligner = PcmAligner()
        first = True
        audio_ms = 0.0
        last_end_ms = 0.0
        async with self._ctx.http.stream("POST", url, params={"output_format": self.options.output_format},
                                         json={"text": text, "model_id": self.model},
                                         headers={"xi-api-key": key}, timeout=SPEECH_TIMEOUT_S) as response:
            if not response.is_success:
                await response.aread()
                raise_for_status(response, "ElevenLabs speech")
            if not self.options.with_timestamps:
                async for raw in response.aiter_bytes():
                    if pcm := aligner.push(raw):
                        yield AudioChunk(pcm, mime=PCM_MIME if first else None,
                                         sample_rate=self.sample_rate if first else None)
                        first = False
                return
            decoder = JsonStreamDecoder()
            async for text_chunk in response.aiter_text():
                for obj in decoder.push(text_chunk):
                    pcm = aligner.push(base64.b64decode(obj.get("audio_base64") or ""))
                    raw_alignment = obj.get("alignment")
                    starts = (raw_alignment or {}).get("character_start_times_seconds") or []
                    # Per-chunk alignment restarts near 0; absolute alignment continues: shift only the former.
                    relative = bool(starts) and starts[0] * 1000 + 50 < last_end_ms
                    alignment = alignment_tuples(raw_alignment, audio_ms if relative else 0.0)
                    if alignment:
                        last_end_ms = alignment[-1][2]
                    if pcm or alignment:
                        yield AudioChunk(pcm, mime=PCM_MIME if first else None,
                                         sample_rate=self.sample_rate if first else None, alignment=alignment)
                        first = False
                    audio_ms += pcm_duration_ms(len(pcm), self.sample_rate)

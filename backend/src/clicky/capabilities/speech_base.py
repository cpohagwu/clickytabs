"""Shared plumbing for the STT/TTS adapters.

- `BatchSTTStream`: for engines that transcribe a whole recording (Ollama audio, faster-whisper, Parakeet, OpenAI).
  It buffers the webm chunks, decodes once at the end, skips the model entirely when nothing was said, and trims the
  silence around the speech before transcribing.
- Local Hugging Face models are only ever *found*, never downloaded during a turn: a missing model makes the
  provider unhealthy with a `clicky models pull` hint, and the chain falls back.
- Every TTS adapter yields PCM16 mono (`audio/pcm` + sample rate) so the extension can stream it gaplessly.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx

from clicky.capabilities.base import STTEvent, Transcript
from clicky.speech.audio import STT_RATE, decode_to_pcm, pcm_duration_ms
from clicky.speech.endpointing import speech_segments, trim_to_speech

PCM_MIME = "audio/pcm"


class SpeechProviderError(RuntimeError):
    pass


def raise_for_status(response: httpx.Response, what: str) -> None:
    if response.is_success:
        return
    try:
        detail = response.json()
        message = detail.get("error", detail) if isinstance(detail, dict) else detail
        if isinstance(message, dict):
            message = message.get("message") or message.get("detail") or message
    except ValueError:
        message = response.text[:300]
    raise SpeechProviderError(f"{what} failed ({response.status_code}): {message}")


class BatchSTTStream:
    """Buffers the recording; `finish()` decodes, endpoints and transcribes it with `transcribe(pcm16k)`."""

    def __init__(self, transcribe: Callable[[bytes], Awaitable[str]], *, language: str | None) -> None:
        self._transcribe = transcribe
        self._language = language
        self._chunks: list[bytes] = []

    async def feed(self, chunk: bytes) -> None:
        self._chunks.append(chunk)

    async def events(self) -> AsyncIterator[STTEvent]:
        return
        yield  # pragma: no cover - makes this an (empty) async generator

    async def finish(self) -> Transcript:
        data = b"".join(self._chunks)
        if not data:
            return Transcript("", self._language, 0.0)
        pcm = await asyncio.to_thread(decode_to_pcm, data)
        duration = pcm_duration_ms(len(pcm), STT_RATE)
        spans = await asyncio.to_thread(speech_segments, pcm)
        if not spans:
            return Transcript("", self._language, duration)       # nothing said: no model call
        text = await self._transcribe(trim_to_speech(pcm, spans))
        return Transcript(" ".join(text.split()), self._language, duration)


def hf_snapshot(repo_id: str) -> str | None:
    """Local path of an already-downloaded Hugging Face snapshot, or None (never downloads)."""
    try:
        from huggingface_hub import snapshot_download
        from huggingface_hub.errors import LocalEntryNotFoundError
    except ImportError:
        return None
    try:
        return snapshot_download(repo_id=repo_id, local_files_only=True)
    except (LocalEntryNotFoundError, OSError, ValueError):
        return None

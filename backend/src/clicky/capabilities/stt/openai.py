"""OpenAISTT: OpenAI transcription (`gpt-4o-mini-transcribe`) over plain httpx (no SDK).

The browser's webm/opus recording is uploaded as is (the endpoint accepts webm). When PyAV is installed, a silent
recording is caught locally first: Whisper-family models tend to invent words for silence, and it saves a call.
The API key comes from settings (CLICKY_HOME/.env), never os.environ. `base_url` allows OpenAI-compatible servers.
"""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from clicky.capabilities.base import Health, ProviderSpec, STTEvent, Transcript
from clicky.capabilities.options import OpenAISTTOptions
from clicky.capabilities.speech_base import raise_for_status

if TYPE_CHECKING:
    from clicky.capabilities.context import ProviderContext

TRANSCRIBE_TIMEOUT_S = 60.0


class _OpenAIStream:
    def __init__(self, provider: OpenAISTT, mime: str, language: str | None) -> None:
        self._provider = provider
        self._mime = mime.split(";")[0] or "audio/webm"
        self._language = language
        self._chunks: list[bytes] = []

    async def feed(self, chunk: bytes) -> None:
        self._chunks.append(chunk)

    async def events(self) -> AsyncIterator[STTEvent]:
        return
        yield  # pragma: no cover

    async def finish(self) -> Transcript:
        data = b"".join(self._chunks)
        if not data or not await asyncio.to_thread(_has_speech, data):
            return Transcript("", self._language)
        text = await self._provider.transcribe_file(data, self._mime, self._language)
        return Transcript(" ".join(text.split()), self._language)


def _has_speech(data: bytes) -> bool:
    if importlib.util.find_spec("av") is None:
        return True                                   # cannot tell without PyAV: let the API decide
    from clicky.speech.audio import decode_to_pcm
    from clicky.speech.endpointing import speech_segments

    try:
        return bool(speech_segments(decode_to_pcm(data)))
    except Exception:                                 # undecodable here: let the API decide
        return True


class OpenAISTT:
    def __init__(self, spec: ProviderSpec, *, model: str | None, options: OpenAISTTOptions,
                 context: ProviderContext) -> None:
        self.spec = spec
        self.model = model or "gpt-4o-mini-transcribe"
        self.options = options
        self._ctx = context

    async def health(self) -> Health:
        return Health(True)               # the key is checked by the registry; a live probe would cost money

    async def aclose(self) -> None:
        return None

    async def open_stream(self, *, mime: str, language: str | None = "en") -> _OpenAIStream:
        return _OpenAIStream(self, mime, self.options.language or language)

    async def transcribe_file(self, data: bytes, mime: str, language: str | None) -> str:
        key = self._ctx.settings.secret_value("openai_api_key") or ""
        form = {"model": self.model, "response_format": "json"}
        if language:
            form["language"] = language
        if self.options.prompt:
            form["prompt"] = self.options.prompt
        extension = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/wav": "wav", "audio/mp4": "mp4"}.get(mime, "webm")
        response = await self._ctx.http.post(
            f"{self.options.base_url.rstrip('/')}/audio/transcriptions", data=form,
            files={"file": (f"speech.{extension}", data, mime)}, headers={"Authorization": f"Bearer {key}"},
            timeout=TRANSCRIBE_TIMEOUT_S)
        raise_for_status(response, "OpenAI transcription")
        return str(response.json().get("text", ""))

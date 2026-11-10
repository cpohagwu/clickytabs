"""DeepgramSTT: Deepgram live streaming (`nova-3`) over a plain WebSocket (`websockets`, no SDK).

wss://api.deepgram.com/v1/listen with `Authorization: Token <key>`. The browser's webm/opus chunks are forwarded as
they arrive (containerised audio: no encoding parameters), so transcription runs while the user speaks:
interim `Results` become partial transcripts, `SpeechStarted` / `UtteranceEnd` are endpointing events, and on key
release `{"type": "Finalize"}` flushes the last words (answered by a Results with `from_finalize`) before
`{"type": "CloseStream"}`. The API key comes from settings, never os.environ.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from clicky.capabilities.base import Health, Partial, ProviderSpec, SpeechStarted, STTEvent, Transcript, UtteranceEnd
from clicky.capabilities.options import DeepgramSTTOptions

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection

    from clicky.capabilities.context import ProviderContext

log = logging.getLogger(__name__)
FINALIZE_TIMEOUT_S = 3.0
CONNECT_TIMEOUT_S = 5.0


class DeepgramStream:
    def __init__(self, ws: ClientConnection, language: str | None) -> None:
        self._ws = ws
        self._language = language
        self._finals: list[str] = []
        self._events: asyncio.Queue[STTEvent | None] = asyncio.Queue()
        self._finalized = asyncio.Event()
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            async for message in self._ws:
                if isinstance(message, bytes):
                    continue
                self.handle(json.loads(message))
        except Exception as exc:          # connection dropped: finish() reports what arrived
            log.debug("deepgram stream closed: %s", exc)
        finally:
            self._finalized.set()
            self._events.put_nowait(None)

    def handle(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "Results":
            alternatives = (message.get("channel") or {}).get("alternatives") or [{}]
            transcript = str(alternatives[0].get("transcript") or "").strip()
            if message.get("is_final"):
                if transcript:
                    self._finals.append(transcript)
                if self._finals:
                    self._events.put_nowait(Partial(" ".join(self._finals)))
            elif transcript:
                self._events.put_nowait(Partial(" ".join([*self._finals, transcript])))
            if message.get("from_finalize"):
                self._finalized.set()
        elif kind == "SpeechStarted":
            self._events.put_nowait(SpeechStarted(at_ms=float(message.get("timestamp") or 0) * 1000))
        elif kind == "UtteranceEnd":
            self._events.put_nowait(UtteranceEnd())
        elif kind == "Error":
            log.warning("deepgram: %s", message.get("description") or message)

    async def feed(self, chunk: bytes) -> None:
        await self._ws.send(chunk)

    async def events(self) -> AsyncIterator[STTEvent]:
        while (event := await self._events.get()) is not None:
            yield event

    async def finish(self) -> Transcript:
        try:
            await self._ws.send(json.dumps({"type": "Finalize"}))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._finalized.wait(), FINALIZE_TIMEOUT_S)
            await self._ws.send(json.dumps({"type": "CloseStream"}))
        finally:
            await self.aclose()
        return Transcript(" ".join(self._finals), self._language)

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            await self._ws.close()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(self._reader, FINALIZE_TIMEOUT_S)


class DeepgramSTT:
    def __init__(self, spec: ProviderSpec, *, model: str | None, options: DeepgramSTTOptions,
                 context: ProviderContext) -> None:
        self.spec = spec
        self.model = model or "nova-3"
        self.options = options
        self._ctx = context

    async def health(self) -> Health:
        return Health(True)               # the key is checked by the registry; a live probe would cost money

    async def aclose(self) -> None:
        return None

    def url(self, language: str | None) -> str:
        opts = self.options
        params = {
            "model": self.model, "language": opts.language or language or "en",
            "smart_format": str(opts.smart_format).lower(), "interim_results": str(opts.interim_results).lower(),
            "endpointing": str(opts.endpointing_ms), "vad_events": str(opts.vad_events).lower(),
        }
        if opts.interim_results:
            params["utterance_end_ms"] = str(opts.utterance_end_ms)
        return f"{opts.base_url.rstrip('/')}/v1/listen?{urlencode(params)}"

    async def open_stream(self, *, mime: str, language: str | None = "en") -> DeepgramStream:
        from websockets.asyncio.client import connect

        key = self._ctx.settings.secret_value("deepgram_api_key") or ""
        ws = await asyncio.wait_for(
            connect(self.url(language), additional_headers={"Authorization": f"Token {key}"}, max_size=2**22),
            CONNECT_TIMEOUT_S)
        return DeepgramStream(ws, self.options.language or language)

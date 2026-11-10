"""Voice turns: push-to-talk audio in, spoken answer out.

    turn.start{mode: voice, page?}           -> turn.state(listening)
    audio.chunk* (webm/opus, 100 ms slices)  -> STT stream (fed live; Deepgram transcribes while you speak)
    audio.end (key released)                 -> turn.state(transcribing) -> transcript.final
                                             -> turn.state(thinking) -> brain stream (+ page context)
    assistant.text.delta* (captions)         -> SentenceSegmenter (eager first chunk) -> Speaker -> TTS
    speech.segment / speech.audio* / speech.segment.end per sentence, streamed while the brain still writes
    turn.done {metrics: stt_ms, brain_first_token_ms, first_audio_ms, total_ms - all from key release}

STT falls back by replaying the buffered recording to the next healthy member; TTS falls back only before a
sentence's first audio, like the brain before its first token. Cancelling the turn (barge-in) cancels everything.

The brain sees the conversation so far (brain/history.py). Its directives (<point/>, ...) are parsed out of the
reply (never spoken), travel with the sentence they were written in (speech.segment.directives, `at_char` = where
in that sentence), and the client fires them on the audio clock. The exchange is recorded when the turn ends; a
barged-in reply is kept as far as it got.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from clicky.brain.directives import DirectiveAt, ReplyEvent, SpeechText
from clicky.brain.history import Conversation
from clicky.capabilities.base import Partial, STTEngine, STTStream, Transcript
from clicky.capabilities.fallback import NoUsableProvider, describe_error, stream_chat, synthesize_speech
from clicky.capabilities.kinds import Capability
from clicky.capabilities.registry import Capabilities, CapabilityChain, ChainMember
from clicky.protocol.events import (
    AssistantTextDelta,
    Directive,
    ErrorEvent,
    SpeechAudio,
    SpeechSegment,
    SpeechSegmentEnd,
    TimedDirective,
    TranscriptFinal,
    TranscriptPartial,
    TurnStart,
    TurnState,
    VisualDirectiveEvent,
)
from clicky.session.brain_turn import BrainTurn, SceneStarter, TaskStarter
from clicky.session.text_turn import DONE_REASONS
from clicky.session.turn import TurnContext
from clicky.speech.audio import pcm_duration_ms
from clicky.speech.segmenter import SentenceSegmenter, SpeechPiece

log = logging.getLogger(__name__)

AUDIO_TIMEOUT_S = 75.0           # push-to-talk holds at most 60 s; a silent client must not pin the turn forever
NO_SPEECH_MESSAGE = "I didn't catch that. Hold the key while you speak."

AudioItem = tuple[bytes, str] | None     # (webm bytes, mime) or None at audio.end


class SpeechToText:
    """Feeds the recording to the first STT member that opens; on failure, replays it to the next ones."""

    def __init__(self, chain: CapabilityChain, on_partial: Callable[[str], None], language: str = "en") -> None:
        self._chain = chain
        self._on_partial = on_partial
        self._language = language
        self._chunks: list[bytes] = []
        self._mime: str | None = None
        self._stream: STTStream | None = None
        self._pump: asyncio.Task[None] | None = None
        self._tried: list[ChainMember] = []
        self._failures: list[tuple[str, str]] = []

    async def _open(self, member: ChainMember) -> STTStream:
        self._tried.append(member)
        if member.provider is None:
            raise RuntimeError(member.health.reason or "unavailable")
        return await cast(STTEngine, member.provider).open_stream(mime=self._mime or "audio/webm",
                                                                language=self._language)

    def _fail(self, member: ChainMember, exc: BaseException) -> None:
        reason = describe_error(exc)
        log.warning("%s failed (%s); trying the next STT provider", member.label, reason)
        self._failures.append((member.label, reason))

    async def _events(self, stream: STTStream) -> None:
        last = ""
        with contextlib.suppress(Exception):
            async for event in stream.events():
                if isinstance(event, Partial) and event.text and event.text != last:
                    last = event.text
                    self._on_partial(event.text)

    async def feed(self, chunk: bytes, mime: str) -> None:
        self._chunks.append(chunk)
        if self._mime is None:
            self._mime = mime
            for member in self._chain.candidates():
                try:
                    self._stream = await self._open(member)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._fail(member, exc)
                    continue
                self._pump = asyncio.create_task(self._events(self._stream))
                break
        if self._stream is not None:
            try:
                await self._stream.feed(chunk)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._fail(self._tried[-1], exc)
                await self._drop()

    async def _drop(self) -> None:
        stream, self._stream = self._stream, None
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None
        closer = getattr(stream, "aclose", None)
        if closer is not None:
            with contextlib.suppress(Exception):
                await closer()

    async def finish(self) -> Transcript:
        if not self._chunks:
            return Transcript("", self._language, 0.0)
        if self._stream is not None:
            try:
                return await self._stream.finish()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._fail(self._tried[-1], exc)
            finally:
                await self._drop()
        for member in self._chain.candidates():
            if member in self._tried:
                continue
            try:
                stream = await self._open(member)
                for chunk in self._chunks:
                    await stream.feed(chunk)
                return await stream.finish()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._fail(member, exc)
        raise NoUsableProvider(Capability.STT, self._failures, self._chain.active.health.hint)

    async def aclose(self) -> None:
        await self._drop()


@dataclass
class _Utterance:
    text: str
    directives: list[TimedDirective]
    started: bool = False          # its speech.segment header (which carries the directives) has gone out


class Speaker:
    """Speaks sentences in order, one TTS request at a time, streaming each one's audio to the client.

    A sentence's directives travel in its speech.segment header, so the client fires them on the audio clock. When
    there is no voice (TTS failed or produced nothing) they go out at once as visual.directive instead.
    """

    def __init__(self, ctx: TurnContext, chain: CapabilityChain, on_first_audio: Callable[[], None]) -> None:
        self._ctx = ctx
        self._chain = chain
        self._on_first_audio = on_first_audio
        self._queue: asyncio.Queue[_Utterance | None] = asyncio.Queue()
        self._last: _Utterance | None = None
        self._seq = 0
        self._spoke = False
        self.failed = False
        self._task = asyncio.create_task(self._run(), name=f"speaker-{ctx.turn_id}")

    def say(self, piece: SpeechPiece | str) -> None:
        text, marks = (piece, ()) if isinstance(piece, str) else (piece.text, piece.marks)
        item = _Utterance(text, [TimedDirective(at_char=at, directive=cast(Directive, d)) for at, d in marks])
        self._last = item
        self._queue.put_nowait(item)

    def trailing(self, directives: list[Directive]) -> None:
        """Directives written after the last words: at the end of the last sentence if it hasn't started, else now."""
        last = self._last
        if last is not None and not last.started and not self.failed:
            last.directives.extend(TimedDirective(at_char=len(last.text), directive=d) for d in directives)
            return
        for directive in directives:
            self._ctx.emit(VisualDirectiveEvent(turn_id=self._ctx.turn_id, directive=directive))

    async def close(self) -> None:
        """Wait until everything queued has been synthesised."""
        self._queue.put_nowait(None)
        await self._task

    async def cancel(self) -> None:
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task

    def _fire_now(self, item: _Utterance) -> None:
        for timed in item.directives:
            self._ctx.emit(VisualDirectiveEvent(turn_id=self._ctx.turn_id, directive=timed.directive))
        item.directives = []

    async def _run(self) -> None:
        while (item := await self._queue.get()) is not None:
            if self.failed:
                self._fire_now(item)           # captions carry on; the voice stays quiet after a TTS failure
                continue
            try:
                await self._speak(item)
            except asyncio.CancelledError:
                raise
            except NoUsableProvider as exc:
                self.failed = True
                message = f"{exc} - {exc.hint}" if exc.hint else str(exc)
                self._ctx.emit(ErrorEvent(code="tts_unavailable", message=message, turn_id=self._ctx.turn_id,
                                          retryable=True))
            except Exception as exc:
                self.failed = True
                self._ctx.emit(ErrorEvent(code="tts_failed", message=describe_error(exc), turn_id=self._ctx.turn_id,
                                          retryable=True))
            if not item.started:
                self._fire_now(item)

    async def _speak(self, item: _Utterance) -> None:
        ctx, seq, text = self._ctx, self._seq, item.text
        self._seq += 1
        index = 0
        audio_bytes = 0
        mime: str | None = None
        rate: int | None = None
        alignment: list[tuple[str, float, float]] = []
        async with contextlib.aclosing(synthesize_speech(self._chain, text)) as chunks:
            async for chunk in chunks:
                if mime is None:
                    mime, rate = chunk.mime or "audio/pcm", chunk.sample_rate
                    item.started = True
                    ctx.emit(SpeechSegment(turn_id=ctx.turn_id, seq=seq, text=text, mime=mime, sample_rate=rate,
                                           directives=list(item.directives)))
                    if not self._spoke:
                        self._spoke = True
                        self._on_first_audio()
                if chunk.data:
                    ctx.emit(SpeechAudio(turn_id=ctx.turn_id, seq=seq, index=index,
                                         data_b64=base64.b64encode(chunk.data).decode()))
                    index += 1
                    audio_bytes += len(chunk.data)
                if chunk.alignment:
                    alignment.extend(chunk.alignment)
        if mime is None:
            return
        duration = pcm_duration_ms(audio_bytes, rate) if mime.startswith("audio/pcm") and rate else 0.0
        char_times = [start for _char, start, _end in alignment] if len(alignment) == len(text) else None
        ctx.emit(SpeechSegmentEnd(turn_id=ctx.turn_id, seq=seq, duration_ms=duration, char_times_ms=char_times))


async def _next_audio(inbox: asyncio.Queue[AudioItem], timeout_s: float) -> AudioItem:
    try:
        return await asyncio.wait_for(inbox.get(), timeout_s)
    except TimeoutError:
        raise AudioTimeout from None


class AudioTimeout(Exception):
    pass


async def run_voice_turn(event: TurnStart, ctx: TurnContext, capabilities: Capabilities,
                         inbox: asyncio.Queue[AudioItem], conversation: Conversation, *,
                         audio_timeout_s: float = AUDIO_TIMEOUT_S, on_scene: SceneStarter | None = None,
                         on_task: TaskStarter | None = None) -> None:
    """Never raises (except CancelledError): every failure becomes an `error` event plus turn.done(error)."""
    loop = asyncio.get_running_loop()
    released = loop.time()

    def since_release() -> float:
        return round((loop.time() - released) * 1000, 1)

    stt = SpeechToText(capabilities.get(Capability.STT),
                       lambda text: ctx.emit(TranscriptPartial(turn_id=ctx.turn_id, text=text)))
    speaker: Speaker | None = None
    brain: BrainTurn | None = None

    async def stop_speaking() -> None:
        if speaker is not None:
            await speaker.cancel()

    try:
        ctx.emit(TurnState(turn_id=ctx.turn_id, state="listening"))
        while (item := await _next_audio(inbox, audio_timeout_s)) is not None:
            await stt.feed(*item)
        released = loop.time()
        ctx.emit(TurnState(turn_id=ctx.turn_id, state="transcribing"))
        transcript = await stt.finish()
        ctx.metrics["stt_ms"] = since_release()
        heard = transcript.text.strip()
        if not heard:
            ctx.fail("no_speech", NO_SPEECH_MESSAGE, retryable=True)
            return
        ctx.emit(TranscriptFinal(turn_id=ctx.turn_id, text=heard))
        ctx.emit(TurnState(turn_id=ctx.turn_id, state="thinking"))
        await conversation.ready()
        conversation.touch()

        def first_audio() -> None:
            ctx.metrics["first_audio_ms"] = since_release()
            ctx.emit(TurnState(turn_id=ctx.turn_id, state="speaking"))

        voice = Speaker(ctx, capabilities.get(Capability.TTS), first_audio)
        speaker = voice
        turn = brain = BrainTurn(conversation, capabilities, heard, event.page, on_scene, on_task)
        segmenter = SentenceSegmenter()

        def handle(reply_event: ReplyEvent) -> None:
            if isinstance(reply_event, SpeechText):
                ctx.emit(AssistantTextDelta(turn_id=ctx.turn_id, delta=reply_event.text))
                for piece in segmenter.push(reply_event.text):
                    voice.say(piece)
            elif isinstance(reply_event, DirectiveAt):
                segmenter.mark(reply_event.directive)     # fires as the words after it are spoken

        def on_delta(delta: str) -> None:
            ctx.metrics.setdefault("brain_first_token_ms", since_release())
            for reply_event in turn.feed(delta):
                handle(reply_event)

        outcome = await stream_chat(capabilities.get(Capability.BRAIN), turn.request_for, on_delta)
        for reply_event in turn.close():
            handle(reply_event)
        for piece in segmenter.finish():
            if piece.text:
                voice.say(piece)
            else:
                voice.trailing([cast(Directive, item) for _at, item in piece.marks])
        await voice.close()
        ctx.metrics["total_ms"] = since_release()
    except asyncio.CancelledError:
        await stop_speaking()                 # barge-in / turn.cancel: silence at once
        if brain is not None:
            brain.record(None, None)          # what was said so far stays in the conversation
        raise
    except AudioTimeout:
        ctx.fail("audio_timeout", "the recording stopped arriving", retryable=True)
        return
    except NoUsableProvider as exc:
        await stop_speaking()
        message = f"{exc} - {exc.hint}" if exc.hint else str(exc)
        ctx.fail("provider_unavailable", message, retryable=True)
        return
    except Exception as exc:
        await stop_speaking()
        if brain is not None:
            brain.record(None, None)
        log.warning("voice turn %s failed: %s", ctx.turn_id, describe_error(exc))
        ctx.fail("provider_error", describe_error(exc), retryable=True)
        return
    finally:
        await stt.aclose()
    brain.record(outcome.finish, outcome.provider)
    finish = outcome.finish
    if finish.stop_reason == "error":
        ctx.emit(ErrorEvent(code="provider_error", message=finish.error or "the provider reported an error",
                            turn_id=ctx.turn_id, retryable=True))
    ctx.finish(DONE_REASONS[finish.stop_reason], detail=finish.refusal_category)

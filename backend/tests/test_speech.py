"""Speech: audio decoding, endpointing, segmenting, the STT/TTS adapters (against mocks), and voice turns."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import math
from array import array
from collections.abc import AsyncIterator
from typing import Any

import av
import httpx
import pytest
from fastapi.testclient import TestClient

from clicky.capabilities.context import ProviderContext
from clicky.capabilities.kinds import Capability
from clicky.capabilities.registry import resolve_member
from clicky.capabilities.stt.deepgram import DeepgramSTT
from clicky.capabilities.stt.ollama_audio import OllamaAudioSTT
from clicky.capabilities.stt.openai import OpenAISTT
from clicky.capabilities.tts.elevenlabs import ElevenLabsTTS, JsonStreamDecoder
from clicky.capabilities.tts.ollama_audio import OllamaAudioTTS
from clicky.capabilities.tts.openai import OpenAITTS
from clicky.config import ProviderChoice, Settings
from clicky.speech.audio import PcmAligner, decode_to_pcm, pcm_to_wav, wav_to_pcm
from clicky.speech.endpointing import speech_segments, trim_to_speech
from clicky.speech.segmenter import SentenceSegmenter, clean_for_speech

from .conftest import ORIGIN, hello

OLLAMA = "http://ollama.test:11434"


# ------------------------------------------------------------------------------------------------- fixtures


def tone(ms: int, rate: int = 16_000, hz: float = 300.0, amp: int = 9000) -> bytes:
    samples = array("h", (int(amp * math.sin(2 * math.pi * hz * i / rate)) for i in range(rate * ms // 1000)))
    return samples.tobytes()


def silence(ms: int, rate: int = 16_000) -> bytes:
    return b"\x00\x00" * (rate * ms // 1000)


def webm_opus(pcm16k: bytes) -> bytes:
    """What MediaRecorder produces: webm/opus (48 kHz), here encoded with PyAV from 16 kHz PCM."""
    pcm48 = decode_to_pcm(pcm_to_wav(pcm16k, 16_000), 48_000)
    buffer = io.BytesIO()
    with av.open(buffer, "w", format="webm") as out:
        stream = out.add_stream("libopus", rate=48_000)
        stream.layout = "mono"
        pts = 0
        for start in range(0, len(pcm48), 960 * 2):
            piece = pcm48[start:start + 960 * 2]
            frame = av.AudioFrame(format="s16", layout="mono", samples=len(piece) // 2)
            frame.planes[0].update(piece)
            frame.sample_rate, frame.pts = 48_000, pts
            pts += len(piece) // 2
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    return buffer.getvalue()


def context(handler: Any, **settings: Any) -> ProviderContext:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ProviderContext.create(Settings(profile="local", ollama={"host": OLLAMA}, **settings), http)


def provider(cls: Any, capability: Capability, name: str, model: str, ctx: ProviderContext, **options: Any) -> Any:
    member = resolve_member(capability, ProviderChoice(provider=name, model=model, options=options), "test")
    return cls(member.spec, model=model, options=member.options, context=ctx)


async def collect(chunks: AsyncIterator[Any]) -> list[Any]:
    return [chunk async for chunk in chunks]


class Body(httpx.AsyncByteStream):
    """A streamed response body, delivered in the given (possibly odd-sized) pieces."""

    def __init__(self, *pieces: bytes) -> None:
        self.pieces = pieces

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for piece in self.pieces:
            yield piece


# ------------------------------------------------------------------------------------------------- audio + VAD


def test_webm_opus_decodes_to_16k_pcm_even_when_cut_short() -> None:
    webm = webm_opus(tone(1000))
    pcm = decode_to_pcm(webm)
    assert abs(len(pcm) / 2 - 16_000) < 800                       # ~1 s of 16 kHz samples
    truncated = decode_to_pcm(webm[: len(webm) * 2 // 3])         # MediaRecorder stopped mid-cluster
    assert 0 < len(truncated) < len(pcm)
    assert wav_to_pcm(pcm_to_wav(pcm, 16_000)) == (pcm, 16_000)


def test_endpointing_finds_speech_and_ignores_silence_and_clicks() -> None:
    assert speech_segments(silence(2000)) == []
    assert len(speech_segments(tone(800))) == 1                    # speech from start to end, no room to learn from
    assert speech_segments(silence(500) + tone(60) + silence(500)) == []        # a key click, not speech
    pcm = silence(600) + tone(900) + silence(200) + tone(400) + silence(700)
    spans = speech_segments(pcm)
    assert len(spans) == 1 and 540 <= spans[0][0] <= 630 and 2070 <= spans[0][1] <= 2130   # the gap is bridged
    trimmed = trim_to_speech(pcm, spans)
    assert len(trimmed) < len(pcm) and abs(len(trimmed) / 32 - (spans[0][1] - spans[0][0] + 400)) < 40


def test_pcm_aligner_never_splits_a_sample() -> None:
    aligner = PcmAligner()
    assert aligner.push(b"\x01\x02\x03") == b"\x01\x02"
    assert aligner.push(b"\x04") == b"\x03\x04"
    assert aligner.push(b"") == b""


# ------------------------------------------------------------------------------------------------- segmenter


def segments(*deltas: str) -> list[str]:
    seg = SentenceSegmenter()
    out = [piece for delta in deltas for piece in seg.feed(delta)]
    return [*out, *seg.flush()]


def test_segmenter_speaks_an_early_first_clause_then_whole_sentences() -> None:
    assert segments("The main heading on this page, ", "as you can see, says Popover API. ",
                    "It explains how popovers work. Want ", "more?") == [
        "The main heading on this page,", "as you can see, says Popover API.", "It explains how popovers work.",
        "Want more?"]
    assert segments("Sure. ", "Here it is.") == ["Sure.", "Here it is."]   # short sentences go out as they are


def test_segmenter_respects_abbreviations_numbers_and_length() -> None:
    assert segments("Dr. Smith paid 3.5 dollars for it, e.g. here. Then left.") == [
        "Dr. Smith paid 3.5 dollars for it,", "e.g. here.", "Then left."]
    long = "word " * 80
    pieces = segments(long)
    assert len(pieces) >= 2 and all(len(p) <= 220 for p in pieces)


def test_markdown_is_not_read_aloud() -> None:
    assert clean_for_speech("**Bold** and `code`, see [the docs](https://x.test).\n- item") == (
        "Bold and code, see the docs. item")


# ------------------------------------------------------------------------------------------------- adapters


async def test_ollama_stt_posts_trimmed_wav_and_skips_silence() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"text": " What is   this? "})

    stt = provider(OllamaAudioSTT, Capability.STT, "ollama_audio", "clicky-lfm3-audio-cpu", context(handler))
    stream = await stt.open_stream(mime="audio/webm;codecs=opus")
    webm = webm_opus(silence(400) + tone(800) + silence(400))
    for start in range(0, len(webm), 700):                        # MediaRecorder-sized slices
        await stream.feed(webm[start:start + 700])
    transcript = await stream.finish()
    assert transcript.text == "What is this?" and len(seen) == 1
    body = seen[0].read()
    assert seen[0].url.path == "/v1/audio/transcriptions" and b"clicky-lfm3-audio-cpu" in body and b"RIFF" in body

    silent = await stt.open_stream(mime="audio/webm")
    await silent.feed(webm_opus(silence(1500)))
    assert (await silent.finish()).text == "" and len(seen) == 1     # silence never reaches the model


async def test_ollama_tts_streams_aligned_pcm() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == {"model": "lfm3", "input": "Hello.", "response_format": "pcm"}
        return httpx.Response(200, stream=Body(b"\x01\x02\x03", b"\x04\x05", b"\x06"))  # odd-sized HTTP chunks

    tts = provider(OllamaAudioTTS, Capability.TTS, "ollama_audio", "lfm3", context(handler))
    chunks = await collect(tts.synthesize("Hello."))
    assert [c.data for c in chunks] == [b"\x01\x02", b"\x03\x04", b"\x05\x06"]
    assert chunks[0].mime == "audio/pcm" and chunks[0].sample_rate == 24_000 and chunks[1].mime is None


async def test_openai_speech_round_trip_uses_pcm_and_the_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-test"
        if request.url.path.endswith("/audio/transcriptions"):
            assert b'name="model"' in request.read() and b"gpt-4o-mini-transcribe" in request.read()
            return httpx.Response(200, json={"text": "hi there"})
        body = json.loads(request.content)
        assert body["response_format"] == "pcm" and body["voice"] == "marin"
        return httpx.Response(200, stream=Body(b"\x00\x01" * 10))

    ctx = context(handler, openai_api_key="sk-test")
    stt = provider(OpenAISTT, Capability.STT, "openai", "gpt-4o-mini-transcribe", ctx)
    stream = await stt.open_stream(mime="audio/webm;codecs=opus")
    await stream.feed(webm_opus(tone(700)))
    assert (await stream.finish()).text == "hi there"
    tts = provider(OpenAITTS, Capability.TTS, "openai", "gpt-4o-mini-tts", ctx)
    chunks = await collect(tts.synthesize("Hi."))
    assert sum(len(c.data) for c in chunks) == 20 and chunks[0].sample_rate == 24_000


async def test_elevenlabs_alignment_becomes_absolute_character_times() -> None:
    pcm = b"\x00\x00" * 2400                                       # 100 ms at 24 kHz per object

    def obj(chars: str, start_s: float) -> str:
        step = 0.1 / len(chars)                                    # the characters span the chunk's 100 ms
        return json.dumps({"audio_base64": base64.b64encode(pcm).decode(), "alignment": {
            "characters": list(chars), "character_start_times_seconds": [start_s + i * step for i in range(len(chars))],
            "character_end_times_seconds": [start_s + (i + 1) * step for i in range(len(chars))]}})

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/text-to-speech/v1d/stream/with-timestamps"
        assert request.url.params["output_format"] == "pcm_24000" and request.headers["xi-api-key"] == "el-test"
        stream = obj("Hi", 0.0) + "\n" + obj(" you", 0.0)          # second chunk restarts at 0: per-chunk times
        return httpx.Response(200, stream=Body(stream[:30].encode(), stream[30:].encode()))

    tts = provider(ElevenLabsTTS, Capability.TTS, "elevenlabs", "eleven_flash_v2_5",
                   context(handler, elevenlabs_api_key="el-test"), voice_id="v1d")
    chunks = await collect(tts.synthesize("Hi you"))
    starts = [round(a[1]) for c in chunks for a in (c.alignment or ())]
    assert starts == [0, 50, 100, 125, 150, 175] and chunks[0].sample_rate == 24_000


def test_json_stream_decoder_handles_split_and_concatenated_objects() -> None:
    decoder = JsonStreamDecoder()
    assert list(decoder.push('{"a": 1}{"b"')) == [{"a": 1}]
    assert list(decoder.push(': 2}\n  {"c": [1, ')) == [{"b": 2}]
    assert list(decoder.push("2]}\n")) == [{"c": [1, 2]}]


async def test_deepgram_streams_partials_and_finalizes(unused_tcp_port: int) -> None:
    from websockets.asyncio.server import serve

    received: list[Any] = []

    async def fake_deepgram(ws: Any) -> None:
        assert ws.request.headers["Authorization"] == "Token dg-test"
        assert "model=nova-3" in ws.request.path and "vad_events=true" in ws.request.path
        async for message in ws:
            received.append(message)
            if isinstance(message, bytes) and len([m for m in received if isinstance(m, bytes)]) == 1:
                await ws.send(json.dumps({"type": "SpeechStarted", "timestamp": 0.4}))
                await ws.send(json.dumps({"type": "Results", "is_final": False,
                                          "channel": {"alternatives": [{"transcript": "what is"}]}}))
                await ws.send(json.dumps({"type": "Results", "is_final": True,
                                          "channel": {"alternatives": [{"transcript": "What is this"}]}}))
            elif message == json.dumps({"type": "Finalize"}):
                await ws.send(json.dumps({"type": "Results", "is_final": True, "from_finalize": True,
                                          "channel": {"alternatives": [{"transcript": "page?"}]}}))
            elif message == json.dumps({"type": "CloseStream"}):
                await ws.close()

    async with serve(fake_deepgram, "127.0.0.1", unused_tcp_port):
        stt = provider(DeepgramSTT, Capability.STT, "deepgram", "nova-3", context(lambda r: httpx.Response(500),
                       deepgram_api_key="dg-test"), base_url=f"ws://127.0.0.1:{unused_tcp_port}")
        stream = await stt.open_stream(mime="audio/webm")
        partials: list[str] = []

        async def pump() -> None:
            async for event in stream.events():
                if hasattr(event, "text"):
                    partials.append(event.text)

        pumping = asyncio.create_task(pump())
        await stream.feed(b"\x1a\x45\xdf\xa3 webm bytes")
        await asyncio.sleep(0.2)
        transcript = await stream.finish()
        await asyncio.wait_for(pumping, 2)
    assert transcript.text == "What is this page?"
    assert partials[:2] == ["what is", "What is this"]
    assert received[-2:] == [json.dumps({"type": "Finalize"}), json.dumps({"type": "CloseStream"})]


# ------------------------------------------------------------------------------------------------- voice turns


def _voice_events(ws: Any, turn_id: str) -> list[dict[str, Any]]:
    events = []
    while True:
        event = ws.receive_json()
        events.append(event)
        if event["type"] == "turn.done" and event["turn_id"] == turn_id:
            return events


def test_voice_turn_streams_transcript_text_and_speech(client: TestClient) -> None:
    with client.websocket_connect("/v1/ws", headers={"origin": ORIGIN}) as ws:
        ws.send_json(hello())
        ws.receive_json()
        ws.send_json({"type": "turn.start", "turn_id": "v1", "mode": "voice"})
        for seq in range(3):
            ws.send_json({"type": "audio.chunk", "turn_id": "v1", "seq": seq,
                          "data_b64": base64.b64encode(b"opus bytes").decode()})
        ws.send_json({"type": "audio.end", "turn_id": "v1"})
        events = _voice_events(ws, "v1")

    states = [e["state"] for e in events if e["type"] == "turn.state"]
    assert states == ["listening", "transcribing", "thinking", "speaking"]
    assert {"type": "transcript.final", "turn_id": "v1", "text": "hello from the fake microphone"} in events
    text = "".join(e["delta"] for e in events if e["type"] == "assistant.text.delta")
    assert text.startswith("You said: hello from the fake microphone.")
    headers = [e for e in events if e["type"] == "speech.segment"]
    ends = [e for e in events if e["type"] == "speech.segment.end"]
    assert [h["seq"] for h in headers] == list(range(len(headers))) and len(headers) >= 2
    assert all(h["mime"] == "audio/pcm" and h["sample_rate"] == 24_000 for h in headers)
    assert " ".join(h["text"] for h in headers) == text.strip()
    audio = [e for e in events if e["type"] == "speech.audio" and e["seq"] == 0]
    assert len(audio) >= 1 and ends[0]["duration_ms"] > 0
    # Every audio chunk of a segment comes after its header and before its end.
    order = [(e["type"], e.get("seq")) for e in events if e["type"].startswith("speech.")]
    assert order.index(("speech.segment", 1)) > order.index(("speech.segment.end", 0))
    done = events[-1]
    assert done["reason"] == "end" and {"stt_ms", "brain_first_token_ms", "first_audio_ms", "total_ms"} <= set(
        done["metrics"])


def test_barge_in_cancels_a_speaking_voice_turn(client: TestClient) -> None:
    with client.websocket_connect("/v1/ws", headers={"origin": ORIGIN}) as ws:
        ws.send_json(hello())
        ws.receive_json()
        ws.send_json({"type": "turn.start", "turn_id": "a", "mode": "text", "text": "[slow] count"})
        assert ws.receive_json()["type"] == "turn.state"
        ws.send_json({"type": "turn.start", "turn_id": "b", "mode": "voice"})   # push-to-talk during the answer
        ws.send_json({"type": "audio.chunk", "turn_id": "b", "seq": 0, "data_b64": base64.b64encode(b"x").decode()})
        ws.send_json({"type": "audio.chunk", "turn_id": "a", "seq": 0, "data_b64": base64.b64encode(b"x").decode()})
        ws.send_json({"type": "audio.end", "turn_id": "b"})
        events = _voice_events(ws, "b")
    done = {e["turn_id"]: e["reason"] for e in events if e["type"] == "turn.done"}
    assert done == {"a": "cancelled", "b": "end"}
    assert not any(e["type"] == "error" for e in events)            # audio for the old turn is ignored quietly


async def test_a_silent_client_cannot_pin_a_voice_turn() -> None:
    from clicky.brain.history import Conversation
    from clicky.protocol.events import TurnStart
    from clicky.session.turn import TurnContext
    from clicky.session.voice_turn import run_voice_turn

    emitted: list[Any] = []
    ctx = TurnContext(turn_id="t", emit=emitted.append)
    caps = type("Caps", (), {"get": lambda self, cap: type("Chain", (), {"candidates": lambda s: []})()})()
    await run_voice_turn(TurnStart(turn_id="t", mode="voice"), ctx, caps, asyncio.Queue(),
                         Conversation("conversation-1"), audio_timeout_s=0.05)
    assert [getattr(e, "code", None) for e in emitted if e.type == "error"] == ["audio_timeout"]
    assert emitted[-1].type == "turn.done" and emitted[-1].reason == "error"


@pytest.fixture
def unused_tcp_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ------------------------------------------------------------------------------------------------- bench demotion


async def test_bench_demotes_a_slow_speech_provider_only_when_a_healthy_one_remains(tmp_path, monkeypatch) -> None:
    from clicky.capabilities import registry
    from clicky.capabilities.base import Health
    from clicky.capabilities.registry import ChainMember, ResolvedChain, build_chain

    slow = resolve_member(Capability.TTS, ProviderChoice(provider="ollama_audio", model="lfm3"), "test")
    fast = resolve_member(Capability.TTS, ProviderChoice(provider="fake"), "test")
    healthy = {"ollama_audio:lfm3": True, "fake": True}

    async def check(member: Any, _ctx: Any) -> ChainMember:
        return ChainMember(member, object(), Health(healthy[member.label]))   # type: ignore[arg-type]

    monkeypatch.setattr(registry, "check_member", check)
    ctx = type("Ctx", (), {"settings": type("S", (), {"data_dir": tmp_path})()})()
    chain = ResolvedChain(Capability.TTS, (slow, fast), "test")
    assert (await build_chain(chain, ctx)).active.label == "ollama_audio:lfm3"          # no bench yet

    (tmp_path / "bench.json").write_text(json.dumps({"results": [
        {"capability": "tts", "provider": "ollama_audio:lfm3", "ok": False, "detail": "RTF 1.4"},
        {"capability": "tts", "provider": "fake", "ok": True, "detail": "fine"}]}))
    assert (await build_chain(chain, ctx)).active.label == "fake"                         # demoted
    healthy["fake"] = False
    assert (await build_chain(chain, ctx)).active.label == "ollama_audio:lfm3"          # never left voiceless
    (tmp_path / "bench.json").write_text("not json")
    assert registry.demoted_speech_providers(tmp_path) == set()


# ------------------------------------------------------------------------------------------------ Kokoro


def test_kokoro_renders_sentence_by_sentence_and_leads_on_cpu_pinned_tiers() -> None:
    from clicky.capabilities.kinds import Capability as Cap
    from clicky.capabilities.kinds import HardwareTier, Profile
    from clicky.capabilities.profiles import presets_for
    from clicky.capabilities.tts.kokoro import split_pieces

    assert split_pieces("Sure. The heading says Popover API. Okay!") == [
        ("Sure.", True), ("The heading says Popover API.", True), ("Okay!", True)]
    long = ("The button below the heading opens the menu, which lists every option, and the arrow shows where the "
            "popover appears on screen when you press it.")
    pieces = split_pieces(long)
    assert len(pieces) > 1 and pieces[-1][1] and not pieces[0][1]
    assert " ".join(piece for piece, _ in pieces) == long
    assert split_pieces("A, b, c.") == [("A, b, c.", True)]                 # short: never cut at commas
    assert split_pieces("   ") == []

    small = presets_for(Profile.LOCAL, HardwareTier.GPU_4GB)[Cap.TTS]
    big = presets_for(Profile.LOCAL, HardwareTier.GPU_8GB)[Cap.TTS]
    assert small.provider == "kokoro" and small.fallbacks[0].provider == "ollama_audio"
    assert big.provider == "ollama_audio" and big.fallbacks[0].provider == "kokoro"

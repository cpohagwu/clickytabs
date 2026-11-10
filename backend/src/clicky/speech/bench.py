"""`clicky doctor --bench`: measure every healthy speech provider *while the brain is decoding*.

In a voice turn the brain is still writing while TTS speaks the first sentences, so each TTS is measured while a
background brain answer keeps the GPU/CPU busy (first audio, real-time factor). STT finishes before the brain starts,
so it is measured idle, on what the first TTS produced (time, words recovered).

Service-level objective (streamed playback stays gapless when synthesis outpaces it):
- TTS: first audio <= 800 ms and real-time factor <= 0.9
- STT: <= 1500 ms for the ~4 s test sentence, and the key words recovered

Results go to CLICKY_HOME/.clicky/bench.json. At startup the registry demotes a provider that missed the SLO in favour
of the next healthy member that met it (never leaving a capability without a provider).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from clicky.capabilities.base import ChatMessage, ChatRequest, STTEngine, TextPart, TTSEngine
from clicky.capabilities.fallback import stream_chat
from clicky.capabilities.kinds import Capability
from clicky.capabilities.registry import BENCH_FILE, Capabilities, ChainMember
from clicky.speech.audio import pcm_duration_ms, pcm_to_wav, resample_pcm

SENTENCE = "Sure. The main heading on this page says Popover API, and the button below it opens the menu."
KEY_WORDS = ("heading", "popover", "button", "menu")
TTS_FIRST_AUDIO_MS = 800.0
TTS_MAX_RTF = 0.9
STT_MAX_MS = 1500.0


@dataclass
class BenchResult:
    capability: str
    provider: str                         # chain label, e.g. "ollama_audio:clicky-lfm3-audio-cpu"
    ok: bool
    detail: str
    first_audio_ms: float | None = None
    rtf: float | None = None
    stt_ms: float | None = None


def bench_path(data_dir: Path) -> Path:
    return data_dir / BENCH_FILE


async def _keep_brain_busy(capabilities: Capabilities, stop: asyncio.Event) -> None:
    request = ChatRequest(system="You are a counter.", purpose="brain", max_tokens=2048,
                          messages=(ChatMessage("user", (TextPart("Count from 1 to 400, one number per line."),)),))
    while not stop.is_set():
        with contextlib.suppress(Exception):
            await stream_chat(capabilities.get(Capability.BRAIN), request, lambda _delta: None)


async def _bench_tts(member: ChainMember) -> tuple[BenchResult, bytes, int]:
    started = time.perf_counter()
    first: float | None = None
    pcm = b""
    rate = 24_000
    async for chunk in cast(TTSEngine, member.provider).synthesize(SENTENCE):
        if first is None:
            first = (time.perf_counter() - started) * 1000
            rate = chunk.sample_rate or rate
        pcm += chunk.data
    total = (time.perf_counter() - started) * 1000
    audio_ms = pcm_duration_ms(len(pcm), rate)
    rtf = total / audio_ms if audio_ms else float("inf")
    ok = first is not None and first <= TTS_FIRST_AUDIO_MS and rtf <= TTS_MAX_RTF
    detail = f"first audio {first or 0:.0f} ms, RTF {rtf:.2f} ({audio_ms / 1000:.1f} s of audio)"
    return BenchResult("tts", member.label, ok, detail, first_audio_ms=first, rtf=round(rtf, 3)), pcm, rate


async def _bench_stt(member: ChainMember, wav16k: bytes) -> BenchResult:
    stream = await cast(STTEngine, member.provider).open_stream(mime="audio/wav")
    await stream.feed(wav16k)
    started = time.perf_counter()
    transcript = await stream.finish()
    elapsed = (time.perf_counter() - started) * 1000
    heard = transcript.text.lower()
    recovered = sum(word in heard for word in KEY_WORDS)
    ok = elapsed <= STT_MAX_MS and recovered >= len(KEY_WORDS) - 1
    detail = f"{elapsed:.0f} ms, {recovered}/{len(KEY_WORDS)} key words: {transcript.text!r}"
    return BenchResult("stt", member.label, ok, detail, stt_ms=round(elapsed, 1))


async def run_bench(capabilities: Capabilities, *, under_load: bool = True) -> list[BenchResult]:
    stop = asyncio.Event()
    load = asyncio.create_task(_keep_brain_busy(capabilities, stop)) if under_load else None
    results: list[BenchResult] = []
    sample: tuple[bytes, int] | None = None
    try:
        if load is not None:
            await asyncio.sleep(2.0)                 # let the brain get going
        for member in capabilities.get(Capability.TTS).members:
            if not member.usable:
                continue
            try:
                # Every member is measured warm (startup only warms the active one): loading is not latency.
                if (warm_up := getattr(member.provider, "warm_up", None)) is not None:
                    await warm_up()
                result, pcm, rate = await _bench_tts(member)
                sample = sample or (pcm, rate)
            except Exception as exc:
                result = BenchResult("tts", member.label, False, f"failed: {exc}")
            results.append(result)
        stop.set()                                   # STT never overlaps the brain in a turn
        if load is not None:
            load.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await load
            load = None
        if sample is not None:
            wav = pcm_to_wav(resample_pcm(*sample, 16_000), 16_000)
            for member in capabilities.get(Capability.STT).members:
                if not member.usable:
                    continue
                try:
                    results.append(await _bench_stt(member, wav))
                except Exception as exc:
                    results.append(BenchResult("stt", member.label, False, f"failed: {exc}"))
    finally:
        stop.set()
        if load is not None:
            load.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await load
    return results


def save_results(data_dir: Path, results: list[BenchResult], profile: str, under_load: bool) -> Path:
    path = bench_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "profile": profile,
        "under_brain_load": under_load,
        "slo": {"tts_first_audio_ms": TTS_FIRST_AUDIO_MS, "tts_max_rtf": TTS_MAX_RTF, "stt_max_ms": STT_MAX_MS},
        "results": [asdict(result) for result in results],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path

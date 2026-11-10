"""Live speech round trip on the local Ollama (uv run pytest -m live): lfm3 speaks a sentence, then transcribes it.

Skipped when Ollama or the CPU-pinned lfm3 model is absent. Exercises the real /v1/audio/speech (streamed PCM) and
/v1/audio/transcriptions endpoints, PyAV resampling, and the endpointer on real speech.
"""

from __future__ import annotations

import time

import httpx
import pytest

from clicky.capabilities.context import ProviderContext
from clicky.capabilities.kinds import Capability
from clicky.capabilities.registry import resolve_member
from clicky.capabilities.stt.ollama_audio import OllamaAudioSTT
from clicky.capabilities.tts.ollama_audio import OllamaAudioTTS
from clicky.config import ProviderChoice, Settings
from clicky.speech.audio import pcm_duration_ms, pcm_to_wav, resample_pcm
from clicky.speech.endpointing import speech_segments

MODEL = "clicky-lfm3-audio-cpu"
pytestmark = pytest.mark.live


async def test_lfm3_speaks_and_hears_itself() -> None:
    settings = Settings(profile="local")
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=3.0), trust_env=False) as http:
        try:
            tags = (await http.get(f"{settings.ollama.host}/api/tags")).json()
        except httpx.HTTPError:
            pytest.skip("Ollama is not reachable")
        if not any(m["name"].startswith(MODEL) for m in tags.get("models", [])):
            pytest.skip(f"{MODEL} is not pulled")
        ctx = ProviderContext.create(settings, http)
        tts_member = resolve_member(Capability.TTS, ProviderChoice(provider="ollama_audio", model=MODEL), "live")
        tts = OllamaAudioTTS(tts_member.spec, model=MODEL, options=tts_member.options, context=ctx)
        assert (await tts.health()).ok

        started = time.perf_counter()
        first_audio_ms: float | None = None
        pcm = b""
        async for chunk in tts.synthesize("Hello from ClickyTabs. The main heading is Popover API."):
            if first_audio_ms is None:
                first_audio_ms = (time.perf_counter() - started) * 1000
                assert chunk.mime == "audio/pcm" and chunk.sample_rate == 24_000
            pcm += chunk.data
        synth_ms = (time.perf_counter() - started) * 1000
        audio_ms = pcm_duration_ms(len(pcm), 24_000)
        assert audio_ms > 1500 and first_audio_ms is not None

        pcm16k = resample_pcm(pcm, 24_000, 16_000)
        assert speech_segments(pcm16k), "the endpointer must find real speech"
        stt_member = resolve_member(Capability.STT, ProviderChoice(provider="ollama_audio", model=MODEL), "live")
        stt = OllamaAudioSTT(stt_member.spec, model=MODEL, options=stt_member.options, context=ctx)
        stream = await stt.open_stream(mime="audio/wav")
        await stream.feed(pcm_to_wav(pcm16k, 16_000))
        started = time.perf_counter()
        transcript = await stream.finish()
        stt_ms = (time.perf_counter() - started) * 1000

    heard = transcript.text.lower()
    print(f"\nTTS first audio {first_audio_ms:.0f} ms, RTF {synth_ms / audio_ms:.2f}; STT {stt_ms:.0f} ms: {heard!r}")
    assert "hello" in heard and "popover" in heard

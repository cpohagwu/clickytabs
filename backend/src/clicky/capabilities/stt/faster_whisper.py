"""FasterWhisperSTT: CTranslate2 Whisper via `faster-whisper` (`local-hf` extra).

Default `Systran/faster-whisper-small.en` on CPU (int8, 4 threads); bigger tiers use distil-large-v3.5 or
large-v3-turbo on the GPU. Models are loaded from the local Hugging Face cache only (`clicky models pull` downloads
them); endpointing comes from the shared batch stream. faster_whisper/numpy are imported lazily, the model is loaded
once per process and inference runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any

from clicky.capabilities.base import Health, ProviderSpec
from clicky.capabilities.models import FASTER_WHISPER_REPOS
from clicky.capabilities.options import FasterWhisperOptions
from clicky.capabilities.speech_base import BatchSTTStream, hf_snapshot

if TYPE_CHECKING:
    from clicky.capabilities.context import ProviderContext

_MODELS: dict[tuple[str, str, str, int], Any] = {}
_LOCK = threading.Lock()


class FasterWhisperSTT:
    def __init__(self, spec: ProviderSpec, *, model: str | None, options: FasterWhisperOptions,
                 context: ProviderContext | None = None) -> None:
        self.spec = spec
        self.model = model or "Systran/faster-whisper-small.en"
        self.options = options
        self._ctx = context
        self.repo = FASTER_WHISPER_REPOS.get(self.model, self.model)

    async def health(self) -> Health:
        if await asyncio.to_thread(hf_snapshot, self.repo) is None:
            return Health(False, f"{self.repo} is not downloaded", "run: clicky models pull --profile local")
        return Health(True)

    async def aclose(self) -> None:
        return None

    async def open_stream(self, *, mime: str, language: str | None = "en") -> BatchSTTStream:
        return BatchSTTStream(self._transcribe, language=self.options.language or language)

    def _load(self) -> Any:
        opts = self.options
        key = (self.repo, opts.device, opts.compute_type, opts.num_threads)
        with _LOCK:
            if key not in _MODELS:
                from faster_whisper import WhisperModel  # `local-hf` extra

                path = hf_snapshot(self.repo)
                if path is None:
                    raise RuntimeError(f"{self.repo} is not downloaded (run: clicky models pull --profile local)")
                _MODELS[key] = WhisperModel(path, device=opts.device, compute_type=opts.compute_type,
                                            cpu_threads=opts.num_threads)
            return _MODELS[key]

    def _run(self, pcm16k: bytes) -> str:
        import numpy as np  # a faster-whisper dependency

        audio = np.frombuffer(pcm16k, dtype="<i2").astype(np.float32) / 32768.0
        segments, _info = self._load().transcribe(audio, language=self.options.language,
                                                  beam_size=self.options.beam_size, vad_filter=False)
        return "".join(segment.text for segment in segments)

    async def _transcribe(self, pcm16k: bytes) -> str:
        return await asyncio.to_thread(self._run, pcm16k)

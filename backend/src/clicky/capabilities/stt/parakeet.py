"""ParakeetSTT: NVIDIA Parakeet-TDT-0.6B-v2 (English, CC-BY-4.0) via `onnx-asr` on CPU (`local-hf` extra).

Model `nemo-parakeet-tdt-0.6b-v2` -> Hugging Face `istupakov/parakeet-tdt-0.6b-v2-onnx` (int8 by default), loaded from
the local cache only (`clicky models pull` downloads it). onnx_asr/numpy are imported lazily; the model is loaded once
per process and inference runs in a worker thread. Endpointing comes from the shared batch stream.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any

from clicky.capabilities.base import Health, ProviderSpec
from clicky.capabilities.models import PARAKEET_REPOS
from clicky.capabilities.options import ParakeetOptions
from clicky.capabilities.speech_base import BatchSTTStream, hf_snapshot
from clicky.speech.audio import STT_RATE

if TYPE_CHECKING:
    from clicky.capabilities.context import ProviderContext

_MODELS: dict[tuple[str, str | None, int], Any] = {}
_LOCK = threading.Lock()


class ParakeetSTT:
    def __init__(self, spec: ProviderSpec, *, model: str | None, options: ParakeetOptions,
                 context: ProviderContext | None = None) -> None:
        self.spec = spec
        self.model = model or "nemo-parakeet-tdt-0.6b-v2"
        self.options = options
        self._ctx = context
        self.repo = PARAKEET_REPOS.get(self.model, self.model)

    async def health(self) -> Health:
        if await asyncio.to_thread(hf_snapshot, self.repo) is None:
            return Health(False, f"{self.repo} is not downloaded", "run: clicky models pull --profile local")
        return Health(True)

    async def aclose(self) -> None:
        return None

    async def open_stream(self, *, mime: str, language: str | None = "en") -> BatchSTTStream:
        return BatchSTTStream(self._transcribe, language="en")    # English-only model

    def _load(self) -> Any:
        key = (self.model, self.options.quantization, self.options.num_threads)
        with _LOCK:
            if key not in _MODELS:
                import onnx_asr  # `local-hf` extra

                path = hf_snapshot(self.repo)
                if path is None:
                    raise RuntimeError(f"{self.repo} is not downloaded (run: clicky models pull --profile local)")
                _MODELS[key] = onnx_asr.load_model(self.model, path, quantization=self.options.quantization)
            return _MODELS[key]

    def _run(self, pcm16k: bytes) -> str:
        import numpy as np  # an onnx-asr dependency

        audio = np.frombuffer(pcm16k, dtype="<i2").astype(np.float32) / 32768.0
        return str(self._load().recognize(audio, sample_rate=STT_RATE))

    async def _transcribe(self, pcm16k: bytes) -> str:
        return await asyncio.to_thread(self._run, pcm16k)

"""KokoroTTS: Kokoro-82M (Apache-2.0) via `kokoro-onnx` 0.6.x on CPU (`kokoro` extra).

Model files kokoro-v1.0.onnx + voices-v1.0.bin come from the kokoro-onnx GitHub release (`clicky models pull`,
sha256-pinned) and live in CLICKY_HOME/.clicky/models/kokoro. kokoro_onnx/numpy/onnxruntime are imported lazily; the
model is loaded once per process (and by `warm_up`, so the first sentence doesn't pay for it).

Kokoro renders a whole phrase at once, so the first audio waits for the whole phrase: the text is cut into sentences
(and long sentences at clause marks), each rendered in a worker thread and streamed as soon as it is done, with a
short pause between sentences.
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from clicky.capabilities.base import AudioChunk, Health, ProviderSpec
from clicky.capabilities.models import KOKORO_FILES
from clicky.capabilities.options import KokoroOptions
from clicky.capabilities.speech_base import PCM_MIME

if TYPE_CHECKING:
    from clicky.capabilities.context import ProviderContext

SLICE_BYTES = 24_000 * 2 // 5          # 200 ms at 24 kHz
SENTENCE_PAUSE_S = 0.18
LONG_PIECE_CHARS = 120                 # longer sentences are also cut at clause marks
MIN_PIECE_CHARS = 12                   # shorter pieces join their neighbour (Kokoro's prosody needs context)
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
_CLAUSE_END = re.compile(r"(?<=[,;:—])\s+")
_MODELS: dict[tuple[str, int | None], Any] = {}
_LOCK = threading.Lock()


def split_pieces(text: str) -> list[tuple[str, bool]]:
    """(piece, ends_sentence) in order: sentences, long ones cut at clause marks, tiny pieces merged forward."""
    pieces: list[tuple[str, bool]] = []
    for sentence in _SENTENCE_END.split(text.strip()):
        sentence = sentence.strip()
        if not sentence:
            continue
        parts = _CLAUSE_END.split(sentence) if len(sentence) > LONG_PIECE_CHARS else [sentence]
        merged: list[str] = []
        for part in parts:
            if merged and len(merged[-1]) < MIN_PIECE_CHARS:
                merged[-1] = f"{merged[-1]} {part}"
            else:
                merged.append(part)
        if len(merged) > 1 and len(merged[-1]) < MIN_PIECE_CHARS:
            tail = merged.pop()
            merged[-1] = f"{merged[-1]} {tail}"
        pieces.extend((part, index == len(merged) - 1) for index, part in enumerate(merged))
    # A tiny opening sentence ("Sure.") is fine on its own: it is the fastest possible first audio.
    return pieces


class KokoroTTS:
    def __init__(self, spec: ProviderSpec, *, model: str | None, options: KokoroOptions,
                 context: ProviderContext) -> None:
        self.spec = spec
        self.model = model or "kokoro-v1.0"
        self.options = options
        self._ctx = context
        self.files = [context.settings.data_dir / "models" / "kokoro" / name for name in KOKORO_FILES]

    async def health(self) -> Health:
        missing = [path.name for path in self.files if not path.is_file()]
        if missing:
            return Health(False, f"missing {', '.join(missing)}",
                          "run: clicky models pull --profile local --only kokoro")
        return Health(True)

    async def aclose(self) -> None:
        return None

    async def warm_up(self) -> None:
        """Load the model and render one word (first inference allocates; espeak loads its data)."""
        await asyncio.to_thread(self._run, "Ready.", self.options.voice)

    def _load(self) -> Any:
        model_path, voices_path = (str(path) for path in self.files)
        key = (model_path, self.options.threads)
        with _LOCK:
            if key not in _MODELS:
                from kokoro_onnx import Kokoro  # `kokoro` extra

                if self.options.threads is None:
                    _MODELS[key] = Kokoro(model_path, voices_path)
                else:
                    import onnxruntime as ort

                    session_options = ort.SessionOptions()
                    session_options.intra_op_num_threads = self.options.threads
                    session = ort.InferenceSession(model_path, session_options,
                                                   providers=["CPUExecutionProvider"])
                    _MODELS[key] = Kokoro.from_session(session, voices_path)
            return _MODELS[key]

    def _run(self, text: str, voice: str) -> tuple[bytes, int]:
        import numpy as np  # a kokoro-onnx dependency

        samples, rate = self._load().create(text, voice=voice, speed=self.options.speed, lang=self.options.lang)
        pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767).astype("<i2").tobytes()
        return pcm, int(rate)

    async def voices(self) -> list[tuple[str, str]]:
        """(id, name) of every voice in voices-v1.0.bin (the options page lists them)."""
        def names() -> list[str]:
            import numpy as np  # a kokoro-onnx dependency

            with np.load(str(self.files[1])) as archive:
                return sorted(archive.files)

        return [(name, name) for name in await asyncio.to_thread(names)]

    async def synthesize(self, text: str, *, voice: str | None = None) -> AsyncIterator[AudioChunk]:
        chosen = voice or self.options.voice
        first = True
        for piece, ends_sentence in split_pieces(text) or [(text, True)]:
            pcm, rate = await asyncio.to_thread(self._run, piece, chosen)
            if ends_sentence:
                pcm += b"\x00\x00" * int(rate * SENTENCE_PAUSE_S)
            for start in range(0, len(pcm), SLICE_BYTES):
                yield AudioChunk(pcm[start:start + SLICE_BYTES], mime=PCM_MIME if first else None,
                                 sample_rate=rate if first else None)
                first = False

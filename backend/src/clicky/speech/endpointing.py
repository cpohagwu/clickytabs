"""Endpointing for batch STT providers: where the speech is in a push-to-talk recording.

Push-to-talk already marks the end of the utterance (key release), so endpointing here does two cheap jobs: skip STT
entirely when nothing was said (an accidental tap answers in milliseconds instead of a model call), and trim the
leading and trailing silence the model would otherwise have to process. A frame-energy detector with an adaptive
noise floor is enough for that and needs no model or numpy. Deepgram reports endpointing natively.
"""

from __future__ import annotations

import math
import sys
from array import array

FRAME_MS = 30
MIN_SPEECH_MS = 120          # shorter bursts (clicks, key noise) are not speech
HANGOVER_MS = 300            # gaps shorter than this stay inside one segment
ABS_FLOOR_DBFS = -50.0       # never call anything quieter than this speech
NOISE_MARGIN_DB = 12.0       # speech is this much louder than the room
MAX_THRESHOLD_DBFS = -35.0   # a recording that is speech from start to end has no quiet frames to learn the room
                             # from; its "floor" is the speech itself, so the threshold must not follow it up


def _frame_dbfs(pcm16k: bytes, rate: int) -> list[float]:
    samples = array("h")
    samples.frombytes(pcm16k[: len(pcm16k) - len(pcm16k) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    size = rate * FRAME_MS // 1000
    levels = []
    for start in range(0, len(samples) - size + 1, size):
        frame = samples[start:start + size]
        rms = math.sqrt(sum(x * x for x in frame) / size)
        levels.append(20 * math.log10(rms / 32768) if rms > 0 else -120.0)
    return levels


def speech_segments(pcm: bytes, rate: int = 16_000) -> list[tuple[float, float]]:
    """(start_ms, end_ms) spans that contain speech, in mono PCM16."""
    levels = _frame_dbfs(pcm, rate)
    if not levels:
        return []
    floor = sorted(levels)[len(levels) // 5]                  # 20th percentile = room noise
    threshold = max(ABS_FLOOR_DBFS, min(floor + NOISE_MARGIN_DB, MAX_THRESHOLD_DBFS))
    spans: list[tuple[float, float]] = []
    start: int | None = None
    last_voiced = -1
    hangover = HANGOVER_MS // FRAME_MS
    for index, level in enumerate(levels):
        if level >= threshold:
            if start is None:
                start = index
            last_voiced = index
        elif start is not None and index - last_voiced > hangover:
            spans.append((start * FRAME_MS, (last_voiced + 1) * FRAME_MS))
            start = None
    if start is not None:
        spans.append((start * FRAME_MS, (last_voiced + 1) * FRAME_MS))
    return [(a, b) for a, b in spans if b - a >= MIN_SPEECH_MS]


def trim_to_speech(pcm: bytes, spans: list[tuple[float, float]], rate: int = 16_000, pad_ms: float = 200) -> bytes:
    """The audio from just before the first speech to just after the last (unchanged when there are no spans)."""
    if not spans:
        return pcm
    start = max(0, int((spans[0][0] - pad_ms) * rate / 1000)) * 2
    end = min(len(pcm), int((spans[-1][1] + pad_ms) * rate / 1000) * 2)
    return pcm[start:end]

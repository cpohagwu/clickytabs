"""Audio plumbing: the browser's webm/opus recording -> 16 kHz mono PCM16 (PyAV, `local` extra), PCM <-> WAV.

PCM here is always signed 16-bit little-endian mono. PyAV is imported lazily so the module loads without the extra;
no numpy is needed (frames are read through their buffer interface).
"""

from __future__ import annotations

import io
import logging
import wave

log = logging.getLogger(__name__)

STT_RATE = 16_000


def decode_to_pcm(data: bytes, rate: int = STT_RATE) -> bytes:
    """Decode a (possibly truncated) audio file - webm/opus from MediaRecorder, WAV, ... - to mono PCM16 at `rate`."""
    import av  # lazy: the `local` extra

    out = bytearray()
    resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)

    def take(frames: list[av.AudioFrame]) -> None:
        for frame in frames:
            out.extend(bytes(frame.planes[0])[: frame.samples * 2])   # planes may carry alignment padding

    with av.open(io.BytesIO(data)) as container:
        try:
            for frame in container.decode(audio=0):
                take(resampler.resample(frame))
        except av.error.FFmpegError as exc:   # a recording cut mid-cluster: keep what decoded
            log.debug("audio decode stopped early: %s", exc)
        take(resampler.resample(None))
    return bytes(out)


def resample_pcm(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
    """Resample mono PCM16 (e.g. 24 kHz TTS output -> 16 kHz for STT)."""
    if from_rate == to_rate:
        return pcm
    return decode_to_pcm(pcm_to_wav(pcm, from_rate), to_rate)


def pcm_to_wav(pcm: bytes, rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


def wav_to_pcm(data: bytes) -> tuple[bytes, int]:
    """(PCM16 mono, sample rate) of a mono 16-bit WAV."""
    with wave.open(io.BytesIO(data), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError("expected mono 16-bit WAV")
        return wav.readframes(wav.getnframes()), wav.getframerate()


def pcm_duration_ms(nbytes: int, rate: int) -> float:
    return nbytes / 2 / rate * 1000


class PcmAligner:
    """Re-chunks a PCM16 byte stream so no chunk splits a sample (HTTP chunks can end on an odd byte)."""

    def __init__(self) -> None:
        self._carry = b""

    def push(self, data: bytes) -> bytes:
        data = self._carry + data
        cut = len(data) - (len(data) % 2)
        self._carry = data[cut:]
        return data[:cut]

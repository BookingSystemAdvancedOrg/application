"""The frame model (FROZEN shape; owner: builder 'listen' for any bug fix).

Inside the gateway every audio chunk is a `Frame`: PCM16 little-endian
mono at the canonical rate (AudioConfig.sample_rate, 16 kHz), 20 ms
(640 bytes), stamped with its start on the call clock. Telephony wire
formats (8 kHz mu-law, 16 kHz PCM) are converted only at the adapter edge
(`to_canonical` / `from_canonical`). Pure Python on purpose (audioop is
gone since Python 3.13): mu-law is a 256 entry table, the rest is
`array`. Resampling lives in resample.py (numpy).
"""

from __future__ import annotations

import math
import sys
from array import array
from dataclasses import dataclass
from typing import Iterator, Literal

FRAME_MS = 20
CANONICAL_RATE = 16000
SAMPLE_BYTES = 2


@dataclass(frozen=True, slots=True)
class AudioFormat:
    encoding: Literal["mulaw", "pcm16"]
    sample_rate: int
    frame_ms: int = FRAME_MS

    @property
    def frame_bytes(self) -> int:
        per_sample = 1 if self.encoding == "mulaw" else SAMPLE_BYTES
        return self.sample_rate * self.frame_ms // 1000 * per_sample


TELEPHONY_MULAW_8K = AudioFormat("mulaw", 8000)
PCM16_8K = AudioFormat("pcm16", 8000)
PCM16_16K = AudioFormat("pcm16", 16000)


@dataclass(frozen=True, slots=True)
class Frame:
    """`pcm`: int16 LE mono at `sample_rate`; `ts_ms`: start on the call clock."""
    pcm: bytes
    ts_ms: int
    sample_rate: int = CANONICAL_RATE

    @property
    def samples(self) -> int:
        return len(self.pcm) // SAMPLE_BYTES

    @property
    def duration_ms(self) -> int:
        return self.samples * 1000 // self.sample_rate

    @property
    def end_ms(self) -> int:
        return self.ts_ms + self.duration_ms


def frame_bytes(sample_rate: int = CANONICAL_RATE, frame_ms: int = FRAME_MS) -> int:
    return sample_rate * frame_ms // 1000 * SAMPLE_BYTES


def silence(ts_ms: int, sample_rate: int = CANONICAL_RATE, frame_ms: int = FRAME_MS) -> Frame:
    return Frame(bytes(frame_bytes(sample_rate, frame_ms)), ts_ms, sample_rate)


def frames_from_pcm(pcm: bytes, start_ms: int = 0, sample_rate: int = CANONICAL_RATE,
                    frame_ms: int = FRAME_MS) -> Iterator[Frame]:
    """Cuts PCM16 into whole frames; the last one is padded with silence."""
    size = frame_bytes(sample_rate, frame_ms)
    for i, offset in enumerate(range(0, len(pcm), size)):
        chunk = pcm[offset:offset + size]
        if len(chunk) < size:
            chunk = chunk + bytes(size - len(chunk))
        yield Frame(chunk, start_ms + i * frame_ms, sample_rate)


def _samples(pcm: bytes) -> array:
    a = array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        a.byteswap()
    return a


def _bytes(a: array) -> bytes:
    if sys.byteorder == "big":
        a = array("h", a)
        a.byteswap()
    return a.tobytes()


def rms_dbfs(pcm: bytes) -> float:
    """Level of PCM16 in dBFS (-120.0 for digital silence)."""
    a = _samples(pcm)
    if not a:
        return -120.0
    mean = sum(x * x for x in a) / len(a)
    return -120.0 if mean <= 0 else max(-120.0, 10 * math.log10(mean / (32768.0 ** 2)))


def gain(pcm: bytes, db: float) -> bytes:
    """PCM16 scaled by `db` (clipped)."""
    factor = 10 ** (db / 20)
    return _bytes(array("h", (max(-32768, min(32767, int(x * factor))) for x in _samples(pcm))))


# --- G.711 mu-law -----------------------------------------------------------------------------

_BIAS, _CLIP = 0x84, 32635


def _decode_byte(u: int) -> int:
    u = ~u & 0xFF
    sign, exponent, mantissa = u & 0x80, (u >> 4) & 0x07, u & 0x0F
    sample = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return -sample if sign else sample


_ULAW_TO_LINEAR = [_decode_byte(i) for i in range(256)]


def _encode_sample(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    sample = min(_CLIP, -sample if sign else sample) + _BIAS
    exponent, mask = 7, 0x4000
    while exponent > 0 and not sample & mask:
        exponent, mask = exponent - 1, mask >> 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def mulaw_decode(data: bytes) -> bytes:
    """G.711 mu-law bytes -> PCM16 LE (same rate)."""
    return _bytes(array("h", (_ULAW_TO_LINEAR[b] for b in data)))


def mulaw_encode(pcm: bytes) -> bytes:
    """PCM16 LE -> G.711 mu-law bytes (same rate)."""
    return bytes(_encode_sample(x) for x in _samples(pcm))


# --- the adapter edge -------------------------------------------------------------------------

def to_canonical(payload: bytes, fmt: AudioFormat, ts_ms: int, resampler=None) -> Frame:
    """One wire frame -> a canonical Frame. `resampler` (resample.Resampler,
    stateful per direction and call) is required when fmt.sample_rate
    differs from CANONICAL_RATE."""
    pcm = mulaw_decode(payload) if fmt.encoding == "mulaw" else payload
    if fmt.sample_rate != CANONICAL_RATE:
        if resampler is None:
            raise ValueError("resampler required")
        pcm = resampler.process(pcm)
    return Frame(pcm, ts_ms, CANONICAL_RATE)


def from_canonical(frame: Frame, fmt: AudioFormat, resampler=None) -> bytes:
    """A canonical Frame -> one wire frame payload."""
    pcm = frame.pcm
    if fmt.sample_rate != frame.sample_rate:
        if resampler is None:
            raise ValueError("resampler required")
        pcm = resampler.process(pcm)
    return mulaw_encode(pcm) if fmt.encoding == "mulaw" else pcm

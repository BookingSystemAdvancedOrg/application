"""WAV helpers for LocalAdapter and the harness. Owner: builder 'listen'.

`read_wav` parses the RIFF itself because the stdlib `wave` module reads
only integer PCM: 8-bit mu-law WAVs (format 7, what telephony recorders
write) are decoded here too. Stdlib only (resampling needs numpy, but only
when the file's rate differs from the target)."""

from __future__ import annotations

import struct
import wave
from array import array
from pathlib import Path

from voice_gateway.audio import frames as _frames


def _chunks(data: bytes):
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    pos = 12
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        yield cid, body
        pos += 8 + size + (size & 1)


def _mono_int16(raw: bytes, tag: int, channels: int, bits: int) -> bytes:
    if tag == 7:                                           # G.711 mu-law
        samples = array("h")
        samples.frombytes(_frames.mulaw_decode(raw))
    elif tag in (1, 0xFFFE) and bits == 16:
        samples = array("h")
        samples.frombytes(raw[: len(raw) // 2 * 2])
    elif tag in (1, 0xFFFE) and bits == 8:                 # unsigned 8-bit
        samples = array("h", ((b - 128) << 8 for b in raw))
    elif tag in (1, 0xFFFE) and bits == 24:
        samples = array("h", (int.from_bytes(raw[i + 1:i + 3], "little", signed=True)
                              for i in range(0, len(raw) - 2, 3)))
    elif tag in (1, 0xFFFE) and bits == 32:
        samples = array("h", (int.from_bytes(raw[i + 2:i + 4], "little", signed=True)
                              for i in range(0, len(raw) - 3, 4)))
    elif tag == 3 and bits == 32:                          # IEEE float
        floats = array("f")
        floats.frombytes(raw[: len(raw) // 4 * 4])
        samples = array("h", (max(-32768, min(32767, int(v * 32768))) for v in floats))
    else:
        raise ValueError(f"unsupported WAV format tag {tag} with {bits} bits")
    if channels > 1:
        n = len(samples) // channels
        samples = array("h", (sum(samples[i * channels:(i + 1) * channels]) // channels for i in range(n)))
    return samples.tobytes()


def read_wav(path: str | Path, target_rate: int = 16000) -> bytes:
    """Mono PCM16 at target_rate from a WAV file (8-bit mu-law WAVs,
    stereo and other rates are converted)."""
    data = Path(path).read_bytes()
    fmt = raw = None
    for cid, body in _chunks(data):
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            raw = body
    if fmt is None or raw is None:
        raise ValueError("WAV without fmt or data chunk")
    tag, channels, rate, _, _, bits = fmt
    pcm = _mono_int16(raw, tag, max(1, channels), bits)
    if rate != target_rate:
        from voice_gateway.audio.resample import resample
        pcm = resample(pcm, rate, target_rate)
    return pcm


def write_wav(path: str | Path, pcm: bytes, sample_rate: int = 16000, channels: int = 1) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)


def interleave(left: bytes, right: bytes) -> bytes:
    """Two mono PCM16 tracks -> one stereo PCM16 (shorter one padded)."""
    n = max(len(left), len(right)) // 2 * 2
    a, b = array("h"), array("h")
    a.frombytes(left.ljust(n, b"\0")[:n])
    b.frombytes(right.ljust(n, b"\0")[:n])
    out = array("h", bytes(2 * len(a) * 2))
    out[0::2], out[1::2] = a, b
    return out.tobytes()

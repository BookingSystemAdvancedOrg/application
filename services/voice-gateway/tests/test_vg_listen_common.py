"""Shared helpers for the listen builder's tests (no tests here)."""

import asyncio
import math
import os
import random
import struct
from array import array
from pathlib import Path

from voice_gateway.audio import frames as fr
from voice_gateway.clock import FakeClock

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "listen"


def tone(freq=220.0, ms=1000, amp=0.3, rate=16000, harmonics=6, start=0.0):
    """A voiced, speech-like synthetic signal (harmonic stack, syllable
    modulation) as PCM16 bytes."""
    n = rate * ms // 1000
    out = array("h")
    for i in range(n):
        t = start + i / rate
        env = 0.55 + 0.45 * math.sin(2 * math.pi * 4.0 * t)
        v = sum(math.sin(2 * math.pi * freq * k * t) / k for k in range(1, harmonics + 1))
        out.append(int(32767 * amp * env * v / 2.2))
    return out.tobytes()


def noise(ms=1000, dbfs=-60.0, rate=16000, seed=1):
    rnd = random.Random(seed)
    sigma = 32768 * 10 ** (dbfs / 20)
    return array("h", (int(max(-32768, min(32767, rnd.gauss(0, sigma)))) for _ in range(rate * ms // 1000))).tobytes()


def silence_pcm(ms, rate=16000):
    return bytes(rate * ms // 1000 * 2)


def run(coro_fn):
    """Runs an async test body on a FakeClock driven in the background."""
    async def main():
        clock = FakeClock()
        driver = asyncio.create_task(clock.drive())
        try:
            return await asyncio.wait_for(coro_fn(clock), 20)
        finally:
            driver.cancel()
    return asyncio.run(main())


def pcm_frames(pcm, start_ms=0):
    return list(fr.frames_from_pcm(pcm, start_ms))


def silero_model_path():
    for cand in (os.environ.get("VG_SILERO_MODEL"), "/opt/models/silero_vad.onnx"):
        if cand and Path(cand).exists():
            return cand
    return None


def samples(pcm):
    a = array("h")
    a.frombytes(pcm)
    return a

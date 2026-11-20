"""Sample rate conversion between the wire (8 kHz) and the canonical rate
(16 kHz). Owner: builder 'listen'.

Requirements:
  * stateful per stream (filter history carried across 20 ms frames, so
    frame borders never click); one Resampler per direction per call;
  * 8000 -> 16000 and 16000 -> 8000 exactly (integer factor 2): a
    polyphase FIR (windowed sinc, about 32 taps per phase, cutoff 0.45 of
    the lower rate) in numpy; any other pair may use a generic path;
  * output length = input length * dst / src for every frame (a 20 ms
    frame in gives a 20 ms frame out, after the first frame's latency is
    absorbed as leading samples of history);
  * under 50 us per frame on one Fargate vCPU.
numpy is imported lazily inside the class so `import voice_gateway`
works without numpy (root CI has none, DESIGN.md 8).
"""

from __future__ import annotations


class Resampler:
    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self.src_rate, self.dst_rate = src_rate, dst_rate

    def process(self, pcm: bytes) -> bytes:
        """PCM16 LE at src_rate -> PCM16 LE at dst_rate (same duration)."""
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError


def resample(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """One-shot conversion of a whole clip (TTS output, test audio)."""
    if src_rate == dst_rate:
        return pcm
    return Resampler(src_rate, dst_rate).process(pcm)

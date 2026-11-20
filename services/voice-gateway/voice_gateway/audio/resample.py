"""Sample rate conversion between the wire (8 kHz) and the canonical rate
(16 kHz). Owner: builder 'listen'.

Requirements:
  * stateful per stream (filter history carried across 20 ms frames, so
    frame borders never click); one Resampler per direction per call;
  * 8000 -> 16000 and 16000 -> 8000 exactly (integer factor 2): a
    polyphase FIR (windowed sinc, 63 taps, 32 per phase, cutoff 0.45 of
    the lower rate) in numpy; any other pair uses a generic linear
    interpolation path (lower quality, not used on the call path);
  * output length = input length * dst / src for every frame (a 20 ms
    frame in gives a 20 ms frame out; the filter delay of about 2 ms
    shows as leading samples of history in the very first frame);
  * under 50 us per frame on one Fargate vCPU.
numpy is imported lazily inside the class so `import voice_gateway`
works without numpy (root CI has none, DESIGN.md 8).
"""

from __future__ import annotations

_TAPS_PER_PHASE = 32
_CUTOFF = 0.45          # of the lower rate


def _design(rate_hi: int, rate_lo: int, taps: int):
    """Windowed-sinc low-pass at _CUTOFF of `rate_lo`, designed at `rate_hi`."""
    import numpy as np
    fc = _CUTOFF * rate_lo / rate_hi            # cycles per sample at rate_hi
    n = np.arange(taps) - (taps - 1) / 2.0
    h = 2 * fc * np.sinc(2 * fc * n) * np.blackman(taps + 2)[1:-1]
    return h / h.sum()


class Resampler:
    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self.src_rate, self.dst_rate = src_rate, dst_rate
        self._np = None
        self._kind = ("up2" if dst_rate == 2 * src_rate else "down2" if src_rate == 2 * dst_rate
                      else "same" if src_rate == dst_rate else "generic")
        self._hist = None
        self._pos = 0               # down2: running input sample index (decimation phase)
        self._frac = 0.0            # generic: fractional read position
        self._last = 0.0            # generic: last input sample of the previous chunk

    def _lazy(self):
        if self._np is None:
            import numpy as np
            self._np = np
            if self._kind == "up2":
                h = _design(self.dst_rate, self.src_rate, 2 * _TAPS_PER_PHASE - 1) * 2.0    # 63 taps, delay 31
                p1 = np.concatenate((h[1::2], [0.0]))                                       # pad to 32 taps
                self._phases = (h[0::2][::-1].copy(), p1[::-1].copy())                      # correlate form
                self._hist = np.zeros(_TAPS_PER_PHASE - 1)
            elif self._kind == "down2":
                h = _design(self.src_rate, self.dst_rate, 2 * _TAPS_PER_PHASE - 1)
                self._h = h[::-1].copy()
                self._hist = np.zeros(len(h) - 1)
        return self._np

    def process(self, pcm: bytes) -> bytes:
        """PCM16 LE at src_rate -> PCM16 LE at dst_rate (same duration)."""
        if self._kind == "same" or not pcm:
            return pcm
        np = self._lazy()
        x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float64)
        if self._kind == "up2":
            ext = np.concatenate((self._hist, x))
            self._hist = ext[len(ext) - (_TAPS_PER_PHASE - 1):]
            y = np.empty(2 * len(x))
            y[0::2] = np.correlate(ext, self._phases[0], "valid")
            y[1::2] = np.correlate(ext, self._phases[1], "valid")
        elif self._kind == "down2":
            ext = np.concatenate((self._hist, x))
            self._hist = ext[len(ext) - len(self._hist):]
            full = np.correlate(ext, self._h, "valid")
            start = (-self._pos) % 2
            y = full[start::2]
            self._pos += len(x)
        else:
            y = self._generic(x)
        return np.clip(np.rint(y), -32768, 32767).astype("<i2").tobytes()

    def _generic(self, x):
        np = self._np
        step = self.src_rate / self.dst_rate
        ext = np.concatenate(([self._last], x))
        n_out = int((len(x) - self._frac) / step) + (1 if self._frac < len(x) else 0)
        pos = self._frac + step * np.arange(max(0, n_out))
        pos = pos[pos < len(x)]
        y = np.interp(pos + 1, np.arange(len(ext)), ext)
        self._frac = float(self._frac + step * len(pos) - len(x))
        self._last = float(x[-1])
        return y

    def reset(self) -> None:
        self._pos, self._frac, self._last = 0, 0.0, 0.0
        if self._hist is not None:
            self._hist = self._hist * 0.0


def resample(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """One-shot conversion of a whole clip (TTS output, test audio)."""
    if src_rate == dst_rate:
        return pcm
    return Resampler(src_rate, dst_rate).process(pcm)

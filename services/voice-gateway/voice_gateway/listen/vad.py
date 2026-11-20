"""Voice activity detection. Owner: builder 'listen'.

VadModel: per-frame speech probability (0..1) for a canonical frame.
  EnergyVad   adaptive noise floor (slow up, fast down) + margin, no deps;
  SileroVad   DEFAULT. silero-vad ONNX (MIT) via onnxruntime (arm64
              py3.13 wheels exist), CPU, 512-sample windows at 16 kHz
              (buffers 20 ms frames), stateful; the model file is fetched
              in the Dockerfile from a pinned release with a sha256 check;
  WebRtcVad   optional, x86 only (webrtcvad-wheels has no arm64 py3.13
              wheel, checked 2026-11-20; Fargate runs ARM), 16 kHz, 20 ms,
              aggressiveness 0..3, mapped to 0.0 / 1.0, smoothed over 3 frames.
make_vad(cfg) picks one; a missing optional library falls back to
EnergyVad with a warning (log.event "vad_fallback").

VadSegmenter: the pure state machine over probabilities:
  silent --(voiced for start_confirm_ms)--> speaking  => VadStart
  speaking --(unvoiced for pause_confirm_ms)--> paused => VadPause
  paused --(voiced for resume_confirm_ms)--> speaking  => VadResume
  While `playing` (assistant audio on the line) the threshold is
  speech_prob_during_playback. A caller turn's VadStart resets when the
  TurnDetector ends the turn (`end_turn()`), so the next speech is a new
  VadStart. Onset and silence timestamps are the first frame of the run,
  not the confirm time.
"""

from __future__ import annotations

import os
from typing import Protocol

from voice_gateway import log
from voice_gateway.audio.frames import Frame, rms_dbfs
from voice_gateway.config import VadConfig
from voice_gateway.events import VadPause, VadResume, VadStart


class VadModel(Protocol):
    def prob(self, frame: Frame) -> float: ...

    def reset(self) -> None: ...


class EnergyVad:
    """Adaptive noise floor: falls fast, rises slowly, and only on frames that
    are not clearly speech, so a long monologue does not lift the floor."""

    def __init__(self, cfg: VadConfig) -> None:
        self.cfg = cfg
        self.reset()

    def reset(self) -> None:
        self.floor = self.cfg.energy_floor_dbfs - 10.0
        self._frames = 0

    def threshold_db(self) -> float:
        return max(self.cfg.energy_floor_dbfs, self.floor + self.cfg.energy_margin_db)

    def prob(self, frame: Frame) -> float:
        level = rms_dbfs(frame.pcm)
        thr = self.threshold_db()
        self._frames += 1
        if level < self.floor:
            self.floor += (level - self.floor) * 0.5
        elif level < thr:
            self.floor += (level - self.floor) * (0.2 if self._frames < 25 else 0.05)
        else:
            self.floor += (level - self.floor) * 0.0002
        x = (level - thr) / max(1.0, self.cfg.energy_margin_db)
        return min(1.0, max(0.0, 0.5 + 0.5 * x))


class WebRtcVad:
    """x86 only (no arm64 py3.13 wheel). 16 kHz, 20 ms frames, smoothed over 3 frames."""

    def __init__(self, cfg: VadConfig) -> None:
        import webrtcvad
        self.cfg = cfg
        self._vad = webrtcvad.Vad(cfg.webrtc_aggressiveness)
        self._hist: list[float] = []

    def prob(self, frame: Frame) -> float:
        raw = 1.0 if self._vad.is_speech(frame.pcm, frame.sample_rate) else 0.0
        self._hist = (self._hist + [raw])[-3:]
        return sum(self._hist) / len(self._hist)

    def reset(self) -> None:
        self._hist = []


_SESSIONS: dict[str, object] = {}
_WINDOW, _CONTEXT = 512, 64


def silero_model_path(cfg: VadConfig) -> str:
    for cand in (os.environ.get("VG_SILERO_MODEL"), cfg.silero_model_path):
        if cand and os.path.exists(cand):
            return cand
    raise FileNotFoundError("silero_vad.onnx not found (VG_SILERO_MODEL or VadConfig.silero_model_path)")


class SileroVad:
    """silero-vad v5 ONNX, 16 kHz. 20 ms frames (320 samples) are buffered into
    512-sample windows; between windows the last probability carries over."""

    def __init__(self, cfg: VadConfig) -> None:
        import numpy as np
        import onnxruntime as ort
        self.cfg, self._np = cfg, np
        path = silero_model_path(cfg)
        if path not in _SESSIONS:
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = opts.inter_op_num_threads = 1
            _SESSIONS[path] = ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])
        self._sess = _SESSIONS[path]
        if {i.name for i in self._sess.get_inputs()} != {"input", "state", "sr"}:
            raise ValueError("unsupported silero model (v5 with input, state, sr expected)")
        self._sr = np.array(16000, dtype=np.int64)
        self.reset()

    def reset(self) -> None:
        np = self._np
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, _CONTEXT), dtype=np.float32)
        self._buf = np.zeros(0, dtype=np.float32)
        self._last = 0.0

    def prob(self, frame: Frame) -> float:
        np = self._np
        x = np.frombuffer(frame.pcm[: len(frame.pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
        self._buf = np.concatenate((self._buf, x))
        while len(self._buf) >= _WINDOW:
            window, self._buf = self._buf[:_WINDOW], self._buf[_WINDOW:]
            inp = np.concatenate((self._context, window[None, :]), axis=1)
            out, self._state = self._sess.run(None, {"input": inp, "state": self._state, "sr": self._sr})
            self._context = inp[:, -_CONTEXT:]
            self._last = float(out[0][0])
        return self._last


def make_vad(cfg: VadConfig) -> VadModel:
    """The configured model; a missing optional library or model file falls
    back to EnergyVad with a warning (log.event "vad_fallback")."""
    try:
        if cfg.model == "silero":
            return SileroVad(cfg)
        if cfg.model == "webrtc":
            return WebRtcVad(cfg)
    except Exception as exc:                      # ImportError, FileNotFoundError, a bad model
        log.event("vad_fallback", level="warning", wanted=cfg.model, error=type(exc).__name__)
    return EnergyVad(cfg)


class VadSegmenter:
    """silent -> speaking -> paused -> speaking ...; onset and silence times
    are the first frame of the run, not the confirm time."""

    def __init__(self, cfg: VadConfig, model: VadModel) -> None:
        self.cfg, self.model = cfg, model
        self.last_prob = 0.0
        self._state = "silent"
        self._clear()

    def _clear(self) -> None:
        self._voiced_ms = 0
        self._run_start: int | None = None      # first frame of the current voiced run
        self._unvoiced_ms = 0
        self._silence_start: int | None = None
        self._speech_ms = 0

    def on_frame(self, frame: Frame, *, playing: bool) -> list[VadStart | VadPause | VadResume]:
        p = self.model.prob(frame)
        self.last_prob = p
        voiced = p >= (self.cfg.speech_prob_during_playback if playing else self.cfg.speech_prob)
        dur, out = frame.duration_ms, []
        if self._state == "silent":
            if voiced:
                if self._run_start is None:
                    self._run_start = frame.ts_ms
                self._voiced_ms += dur
                if self._voiced_ms >= self.cfg.start_confirm_ms:
                    self._state = "speaking"
                    self._speech_ms = self._voiced_ms
                    self._unvoiced_ms, self._silence_start = 0, None
                    out.append(VadStart(frame.end_ms, self._run_start, p, playing))
            else:
                self._voiced_ms, self._run_start = 0, None
        elif self._state == "speaking":
            if voiced:
                self._speech_ms += dur
                self._unvoiced_ms, self._silence_start = 0, None
            else:
                if self._silence_start is None:
                    self._silence_start = frame.ts_ms
                self._unvoiced_ms += dur
                if self._unvoiced_ms >= self.cfg.pause_confirm_ms:
                    self._state = "paused"
                    self._voiced_ms, self._run_start = 0, None
                    out.append(VadPause(frame.end_ms, self._silence_start, self._speech_ms))
        else:                                                   # paused
            if voiced:
                if self._run_start is None:
                    self._run_start = frame.ts_ms
                self._voiced_ms += dur
                if self._voiced_ms >= self.cfg.resume_confirm_ms:
                    self._state = "speaking"
                    self._speech_ms += self._voiced_ms
                    out.append(VadResume(frame.end_ms, max(0, self._run_start - (self._silence_start or 0))))
                    self._unvoiced_ms, self._silence_start = 0, None
            else:
                self._voiced_ms, self._run_start = 0, None
        return out

    def end_turn(self) -> None:
        """The TurnDetector took the turn: the next speech is a new VadStart."""
        self._state = "silent"
        self._clear()

    @property
    def speaking(self) -> bool:
        return self._state == "speaking"

    @property
    def voiced(self) -> bool:
        """Voice on the line now: speaking, or a voiced run not confirmed yet."""
        return self._state == "speaking" or self._run_start is not None

    @property
    def paused(self) -> bool:
        return self._state == "paused"

    @property
    def speech_ms(self) -> int:
        """Voiced ms since the current turn's VadStart (0 when none)."""
        return self._speech_ms if self._state != "silent" else 0

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

from typing import Protocol

from voice_gateway.audio.frames import Frame
from voice_gateway.config import VadConfig
from voice_gateway.events import VadPause, VadResume, VadStart


class VadModel(Protocol):
    def prob(self, frame: Frame) -> float: ...

    def reset(self) -> None: ...


class EnergyVad:
    def __init__(self, cfg: VadConfig) -> None:
        self.cfg = cfg

    def prob(self, frame: Frame) -> float:
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError


class WebRtcVad:
    def __init__(self, cfg: VadConfig) -> None:
        self.cfg = cfg

    def prob(self, frame: Frame) -> float:
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError


class SileroVad:
    def __init__(self, cfg: VadConfig) -> None:
        self.cfg = cfg

    def prob(self, frame: Frame) -> float:
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError


def make_vad(cfg: VadConfig) -> VadModel:
    raise NotImplementedError


class VadSegmenter:
    def __init__(self, cfg: VadConfig, model: VadModel) -> None:
        self.cfg, self.model = cfg, model
        self.last_prob = 0.0

    def on_frame(self, frame: Frame, *, playing: bool) -> list[VadStart | VadPause | VadResume]:
        raise NotImplementedError

    def end_turn(self) -> None:
        """The TurnDetector took the turn: the next speech is a new VadStart."""
        raise NotImplementedError

    @property
    def speaking(self) -> bool:
        raise NotImplementedError

    @property
    def speech_ms(self) -> int:
        """Voiced ms since the current turn's VadStart (0 when none)."""
        raise NotImplementedError

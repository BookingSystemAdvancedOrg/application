"""VAD models and the segmenter (listen builder)."""

import pytest

from test_vg_listen_common import FIXTURES, noise, pcm_frames, silence_pcm, silero_model_path, tone
from voice_gateway.config import VadConfig
from voice_gateway.events import VadPause, VadResume, VadStart
from voice_gateway.listen.vad import EnergyVad, VadSegmenter, make_vad


class ScriptedModel:
    """Probabilities per 20 ms frame: 1 = voiced, 0 = silent."""

    def __init__(self, probs):
        self.probs, self.i = list(probs), 0

    def prob(self, frame):
        p = self.probs[min(self.i, len(self.probs) - 1)]
        self.i += 1
        return float(p)

    def reset(self):
        self.i = 0


def _events(probs, playing=lambda i: False, cfg=None):
    cfg = cfg or VadConfig()
    seg = VadSegmenter(cfg, ScriptedModel(probs))
    out = []
    for i, f in enumerate(pcm_frames(bytes(640 * len(probs)))):
        for ev in seg.on_frame(f, playing=playing(i)):
            out.append(ev)
    return seg, out


def test_segmenter_start_pause_resume_with_first_frame_timestamps():
    probs = [0] * 10 + [1] * 25 + [0] * 12 + [1] * 10 + [0] * 3
    seg, evs = _events(probs)
    start, pause, resume = evs
    assert isinstance(start, VadStart) and start.speech_start_ms == 200      # onset, not the confirm time
    assert start.at_ms == 200 + 100 and start.during_playback is False
    assert isinstance(pause, VadPause) and pause.silence_start_ms == 700      # first unvoiced frame
    assert pause.at_ms == 700 + 80 and pause.speech_ms == 500
    assert isinstance(resume, VadResume) and resume.at_ms == 940 + 60
    assert resume.pause_ms == 940 - 700
    assert len(evs) == 3 and seg.speaking is True
    assert seg.speech_ms == 500 + 200


def test_segmenter_needs_the_confirm_time_and_ignores_blips():
    _, evs = _events([0, 1, 1, 0, 1, 0, 0, 1, 0] + [0] * 10)
    assert evs == []                                                       # under 100 ms in a row
    _, evs = _events([0] * 3 + [1] * 5)
    assert len(evs) == 1 and evs[0].speech_start_ms == 60


def test_segmenter_short_gaps_are_not_pauses():
    _, evs = _events([1] * 10 + [0] * 3 + [1] * 10)                        # 60 ms gap < 80 ms
    assert len(evs) == 1 and isinstance(evs[0], VadStart)


def test_segmenter_stricter_threshold_while_the_assistant_plays():
    cfg = VadConfig()
    probs = [0.6] * 20                                                     # speech-like but not certain
    _, free = _events(probs, cfg=cfg)
    _, playing = _events(probs, playing=lambda i: True, cfg=cfg)
    assert len(free) == 1 and playing == []
    _, sure = _events([0.9] * 20, playing=lambda i: True, cfg=cfg)
    assert sure[0].during_playback is True


def test_end_turn_makes_the_next_speech_a_new_start():
    seg = VadSegmenter(VadConfig(), ScriptedModel([1] * 100))
    frames = pcm_frames(bytes(640 * 100))
    evs = [e for f in frames[:20] for e in seg.on_frame(f, playing=False)]
    assert [type(e) for e in evs] == [VadStart] and seg.speaking
    seg.end_turn()
    assert not seg.speaking and seg.speech_ms == 0
    evs = [e for f in frames[20:40] for e in seg.on_frame(f, playing=False)]
    assert [type(e) for e in evs] == [VadStart]


# --- energy VAD --------------------------------------------------------------------------------

def test_energy_vad_finds_speech_over_line_noise_and_tracks_the_floor():
    cfg = VadConfig(model="energy")
    seg = VadSegmenter(cfg, EnergyVad(cfg))
    audio = (noise(1500, -62) + tone(180, 1200, amp=0.2) + noise(800, -62, seed=2) + tone(210, 900, amp=0.2)
             + noise(1000, -62, seed=3))
    evs = [e for f in pcm_frames(audio) for e in seg.on_frame(f, playing=False)]
    kinds = [type(e).__name__ for e in evs]
    assert kinds == ["VadStart", "VadPause", "VadResume", "VadPause"]
    assert abs(evs[0].speech_start_ms - 1500) <= 60 and abs(evs[1].silence_start_ms - 2700) <= 120


def test_energy_vad_adapts_to_a_noisy_line_and_long_speech_does_not_lift_the_floor():
    cfg = VadConfig(model="energy")
    model = EnergyVad(cfg)
    for f in pcm_frames(noise(2000, -52)):                                 # a noisy line
        model.prob(f)
    assert -58 < model.floor < -48
    quiet = [model.prob(f) for f in pcm_frames(noise(500, -52, seed=9))]
    assert max(quiet) < 0.5                                                # the hum itself is not speech
    voiced = [model.prob(f) for f in pcm_frames(tone(180, 10_000, amp=0.3))]
    late = voiced[len(voiced) // 2:]
    assert sum(p >= 0.5 for p in late) / len(late) > 0.85                 # a 10 s monologue stays speech
    model.reset()
    assert model.floor == cfg.energy_floor_dbfs - 10.0


def test_make_vad_falls_back_to_energy_when_the_model_is_missing(monkeypatch):
    from voice_gateway import log
    monkeypatch.delenv("VG_SILERO_MODEL", raising=False)
    log.sink = []
    try:
        vad = make_vad(VadConfig(model="silero", silero_model_path="/nonexistent/silero.onnx"))
        events = list(log.sink)
    finally:
        log.sink = None
    assert isinstance(vad, EnergyVad) and events[0]["event"] == "vad_fallback"
    assert isinstance(make_vad(VadConfig(model="energy")), EnergyVad)


# --- silero on the line-quality Polly clips ----------------------------------------------------

def _line_clip(name):
    from voice_gateway.audio.wav import read_wav
    return read_wav(FIXTURES / f"{name}.wav", 16000)


@pytest.fixture(scope="module")
def silero_cfg():
    pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")
    path = silero_model_path()
    if not path:
        pytest.skip("silero_vad.onnx not available (set VG_SILERO_MODEL)")
    return VadConfig(model="silero", silero_model_path=path)


def _run_silero(cfg, audio):
    seg = VadSegmenter(cfg, make_vad(cfg))
    return [e for f in pcm_frames(audio) for e in seg.on_frame(f, playing=False)], seg


@pytest.mark.parametrize("name", ["sv_booking", "sv_yes", "en_booking"])
def test_silero_segments_polly_speech_on_a_noisy_line(silero_cfg, name):
    clip = _line_clip(name)
    lead, tail = 1200, 1500
    audio = noise(lead, -55) + _mix(clip, noise(len(clip) // 32, -55, seed=5)) + noise(tail, -55, seed=6)
    evs, _ = _run_silero(silero_cfg, audio)
    starts = [e for e in evs if isinstance(e, VadStart)]
    assert len(starts) == 1
    assert lead - 80 <= starts[0].speech_start_ms <= lead + 320            # a soft onset may trail the first phoneme
    pauses = [e for e in evs if isinstance(e, VadPause)]
    end = lead + len(clip) // 32
    assert pauses and abs(pauses[-1].silence_start_ms - end) < 350
    assert pauses[-1].speech_ms >= 0.6 * len(clip) // 32 - 400


def _mix(a, b):
    import numpy as np
    n = min(len(a), len(b))
    x = np.frombuffer(a[:n], "<i2").astype(np.int32) + np.frombuffer(b[:n], "<i2").astype(np.int32)
    return np.clip(x, -32768, 32767).astype("<i2").tobytes() + a[n:]


def test_silero_ignores_silence_line_noise_and_a_steady_hum(silero_cfg):
    evs, _ = _run_silero(silero_cfg, noise(3000, -50) + silence_pcm(500))
    assert evs == []
    import numpy as np
    t = np.arange(16000 * 3) / 16000
    hum = (np.sin(2 * np.pi * 50 * t) * 0.02 * 32767).astype("<i2").tobytes()
    evs, _ = _run_silero(silero_cfg, hum)
    assert evs == []


def test_silero_is_stateful_resettable_and_fast(silero_cfg):
    import time
    clip = _line_clip("sv_booking")
    model = make_vad(silero_cfg)
    frames = pcm_frames(noise(600, -60) + clip)
    t0 = time.perf_counter()
    first = [model.prob(f) for f in frames]
    per_frame_ms = (time.perf_counter() - t0) / len(frames) * 1000
    assert per_frame_ms < 5                                                # about 1 ms on arm64 per 32 ms window
    model.reset()
    second = [model.prob(f) for f in frames]
    assert first == second and max(first) > 0.9 and max(first[:25]) < 0.5

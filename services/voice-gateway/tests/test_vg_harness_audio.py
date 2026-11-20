"""Caller audio (builder 'harness'): the cache, the phone line, the speech stand-in and Polly through a fake client."""

import math
import wave

import pytest

from vg_harness import caller_audio as ca
from vg_harness.scenarios import Piece, Step
from voice_gateway.audio.frames import rms_dbfs


def tone(ms=200, rate=16000, hz=440.0, level_db=-20.0):
    amp = 32768 * 10 ** (level_db / 20)
    n = rate * ms // 1000
    from array import array
    return array("h", (int(amp * math.sin(2 * math.pi * hz * i / rate)) for i in range(n))).tobytes()


class FakePolly:
    """Polly's synthesize_speech: 8 kHz PCM of 100 ms per character."""

    def __init__(self):
        self.calls = []

    def synthesize_speech(self, **kwargs):
        self.calls.append(kwargs)
        import io
        pcm = tone(100 * len(kwargs["Text"]), rate=8000)
        return {"AudioStream": io.BytesIO(pcm)}


@pytest.fixture
def scratch_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "CACHE", tmp_path)
    return tmp_path


def test_a_cached_clip_renders_without_any_network():
    r = ca.render_detailed("Ja, det stämmer.", "sv")
    assert r.source == "cache" and r.voice == "Astrid"
    assert 800 < r.duration_ms < 3000 and abs(len(r.pcm) - r.duration_ms * 32) < 64
    assert -45 < rms_dbfs(r.pcm) < -10


def test_render_without_cache_or_permission_says_what_is_missing(scratch_cache):
    with pytest.raises(ca.CallerAudioMissing, match="not cached and network not allowed"):
        ca.render_detailed("En mening som aldrig renderats.", "sv")
    with pytest.raises(ca.CallerAudioMissing, match="no Polly voice and no fixture"):
        ca.render_detailed("سلام", "fa")


def test_render_asks_polly_once_then_reads_the_cache(scratch_cache):
    client = FakePolly()
    first = ca.render_detailed("Hej hopp", "sv", client=client)
    second = ca.render_detailed("Hej hopp", "sv", client=client)
    assert (first.source, second.source) == ("polly", "cache")
    assert len(client.calls) == 1
    call = client.calls[0]
    assert (call["VoiceId"], call["Engine"], call["OutputFormat"], call["SampleRate"]) == ("Astrid", "standard", "pcm", "8000")
    assert first.duration_ms == second.duration_ms == 800 and len(first.pcm) == 800 * 32
    assert (scratch_cache / f"sv-{ca.cache_key('Hej hopp', 'sv')}.ulaw").stat().st_size == 800 * 8   # mu-law at 8 kHz


def test_a_supplied_wav_fixture_is_used_for_a_language_without_a_voice(scratch_cache):
    path = ca.cache_path("سلام", "fa", ".wav")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(tone(500))
    r = ca.render_detailed("سلام", "fa")
    assert r.source == "cache" and r.voice is None and r.duration_ms == 500


def test_synth_fallback_for_fake_mode_only(scratch_cache):
    r = ca.render_detailed("سلام", "fa", synth_fallback=True)
    assert r.source == "synth" and r.duration_ms > 200


def test_synth_speech_is_deterministic_voiced_and_scales_with_the_text():
    a = ca.synth_speech("Jag vill boka ett bord för fyra personer.", "sv")
    assert a == ca.synth_speech("Jag vill boka ett bord för fyra personer.", "sv")
    assert abs(rms_dbfs(a) - (-24.0)) < 2.5
    longer = ca.synth_speech("Jag vill boka ett bord för fyra personer på fredag klockan sju, tack.", "sv")
    assert ca.duration_ms(longer) > ca.duration_ms(a)
    assert ca.duration_ms(ca.synth_speech("ehm", "sv")) >= 220


def test_line_keeps_the_length_and_band_limits_the_audio():
    clip = ca.render("Ja, det stämmer.", "sv")
    out = ca.line(clip)
    assert len(out) == len(clip)
    assert abs(rms_dbfs(out) - rms_dbfs(clip)) < 3
    # 5 kHz is above the 4 kHz telephone band: it must be gone
    high = tone(400, hz=5000.0, level_db=-10.0)
    assert rms_dbfs(ca.line(high)) < rms_dbfs(high) - 12


def test_line_noise_is_seeded_and_at_the_asked_level():
    silent = bytes(16000 * 2)
    noisy = ca.line(silent, noise_dbfs=-50.0, seed=3)
    assert noisy == ca.line(silent, noise_dbfs=-50.0, seed=3) and noisy != ca.line(silent, noise_dbfs=-50.0, seed=4)
    assert abs(rms_dbfs(noisy) - (-50.0)) < 2.0


def test_step_audio_labels_and_pauses_join_into_one_clip(scratch_cache):
    step = Step("sv", (Piece("Hej", 400), Piece("ehm", 1500), Piece("tack")))
    pieces = ca.step_audio("voice-A", 0, step, synth_fallback=True)
    assert [p.label for p in pieces] == ["voice-A/s0p0", "voice-A/s0p1", "voice-A/s0p2"]
    joined = ca.join_pieces(pieces)
    expected = sum(len(p.pcm) for p in pieces) + len(ca.silence_pcm(400)) + len(ca.silence_pcm(1500))
    assert len(joined) == expected


def test_warm_renders_what_is_missing_and_stops_when_complete(scratch_cache):
    from vg_harness import scenarios as sc
    client = FakePolly()
    one = [s for s in sc.ALL if s.key == "voice-F"]
    report = ca.warm(one, client_factory=lambda region: client)
    assert len(report) == 2 and all(line.startswith("rendered: sv") for line in report)
    assert ca.warm(one, client_factory=lambda region: client) == []
    assert len(client.calls) == 2


def test_resample_helpers_roundtrip_length():
    pcm = tone(100)
    assert len(ca._upsample2(ca._downsample2(pcm))) == len(pcm)

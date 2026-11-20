"""Resampler, buffers, WAV helpers (listen builder)."""

import math
import struct
import wave

import pytest

from test_vg_listen_common import pcm_frames, samples, tone
from voice_gateway.audio import frames as fr
from voice_gateway.audio.buffers import InboundJitterBuffer, PlayoutBuffer, RingBuffer
from voice_gateway.audio.frames import Frame

np = pytest.importorskip("numpy")


def _sine(freq, n, rate, amp=0.5, offset=0):
    x = np.sin(2 * np.pi * freq * (np.arange(n) + offset) / rate) * amp * 32767
    return x.astype("<i2").tobytes()


def _snr_db(ref, got):
    ref, got = np.frombuffer(ref, "<i2").astype(float), np.frombuffer(got, "<i2").astype(float)
    n = min(len(ref), len(got))
    err = ref[:n] - got[:n]
    return 10 * np.log10((ref[:n] ** 2).sum() / max(1e-9, (err ** 2).sum()))


def _best_delay_snr(ref, got, max_delay=80):
    best = -99
    ref_a = np.frombuffer(ref, "<i2").astype(float)
    got_a = np.frombuffer(got, "<i2").astype(float)
    for d in range(max_delay):
        n = min(len(ref_a), len(got_a) - d) - 200
        e = ref_a[100:n] - got_a[100 + d:n + d]
        best = max(best, 10 * np.log10((ref_a[100:n] ** 2).sum() / max(1e-9, (e ** 2).sum())))
    return best


# --- resampler -------------------------------------------------------------------------------

def test_resampler_upsamples_by_two_and_keeps_frame_lengths():
    from voice_gateway.audio.resample import Resampler
    rs = Resampler(8000, 16000)
    x = _sine(440, 8000, 8000)
    out = b"".join(rs.process(x[i:i + 320]) for i in range(0, len(x), 160 * 2))
    assert len(out) == 2 * len(x)
    for i in range(0, 4):
        assert len(Resampler(8000, 16000).process(x[:320])) == 640   # 20 ms in, 20 ms out
    ref = _sine(440, 16000, 16000)
    assert _best_delay_snr(ref, out) > 35


def test_resampler_downsamples_by_two_and_chunking_does_not_change_the_result():
    from voice_gateway.audio.resample import Resampler
    x = _sine(1000, 16000, 16000)
    one = Resampler(16000, 8000).process(x)
    rs = Resampler(16000, 8000)
    parts = [rs.process(x[i:i + 640]) for i in range(0, len(x), 640)]
    chunked = b"".join(parts)
    assert len(one) == len(x) // 2 and all(len(p) == 320 for p in parts)
    assert one == chunked                                   # no clicks at frame borders
    odd = Resampler(16000, 8000)
    pieces = b"".join(odd.process(x[i:i + 2 * 321]) for i in range(0, len(x), 2 * 321))
    assert abs(len(pieces) - len(one)) <= 2 and pieces[:2000] == one[:2000]    # decimation phase carried


def test_resampler_rejects_aliasing_and_keeps_passband():
    from voice_gateway.audio.resample import Resampler
    # 6 kHz at 16 kHz is above the 8 kHz line's Nyquist-limited band: it must be attenuated
    x = _sine(6000, 16000, 16000)
    low = Resampler(16000, 8000).process(x)
    assert np.abs(np.frombuffer(low, "<i2")[200:]).max() < 0.05 * 32767
    y = _sine(1000, 16000, 16000)
    out = Resampler(16000, 8000).process(y)
    assert np.abs(np.frombuffer(out, "<i2")[200:]).max() > 0.45 * 32767


def test_resampler_speed():
    import time
    from voice_gateway.audio.resample import Resampler
    rs, frame = Resampler(8000, 16000), _sine(300, 160, 8000)
    rs.process(frame)
    t0 = time.perf_counter()
    for _ in range(500):
        rs.process(frame)
    per_frame_us = (time.perf_counter() - t0) / 500 * 1e6
    assert per_frame_us < 500      # the 50 us target is on the Fargate vCPU; the guard here only catches regressions


def test_generic_ratio_and_one_shot():
    from voice_gateway.audio.resample import Resampler, resample
    x = _sine(300, 4410, 44100)
    out = resample(x, 44100, 16000)
    assert abs(len(out) // 2 - 1600) <= 2
    rs = Resampler(44100, 16000)
    chunked = b"".join(rs.process(x[i:i + 882 * 2]) for i in range(0, len(x), 882 * 2))
    assert abs(len(chunked) - len(out)) <= 4
    assert resample(x, 16000, 16000) is x


def test_mulaw_line_roundtrip_through_frames():
    from voice_gateway.audio.resample import Resampler
    down, up = Resampler(16000, 8000), Resampler(8000, 16000)
    pcm = tone(180, 1000)
    out = b""
    for f in pcm_frames(pcm):
        wire = fr.from_canonical(f, fr.TELEPHONY_MULAW_8K, down)
        assert len(wire) == 160
        out += fr.to_canonical(wire, fr.TELEPHONY_MULAW_8K, f.ts_ms, up).pcm
    assert len(out) == len(pcm.ljust(len(out), b"\0"))
    assert _best_delay_snr(pcm, out) > 15


# --- buffers ---------------------------------------------------------------------------------

def _f(ts, fill=0):
    return Frame(bytes([fill]) * 640, ts)


def test_ring_buffer_keeps_the_last_seconds():
    ring = RingBuffer(200)
    assert ring.end_ms is None and ring.since(0) == [] and ring.last(100) == []
    for i in range(30):
        ring.push(_f(i * 20))
    assert len(ring) == 10 and ring.start_ms == 400 and ring.end_ms == 600
    assert [f.ts_ms for f in ring.since(500)] == [500, 520, 540, 560, 580]
    got = [f.ts_ms for f in ring.since(520)]
    assert got == [520, 540, 560, 580]                  # frame 500 ends at 520: not after
    assert [f.ts_ms for f in ring.since(0)][0] == 400   # does not reach back: starts at the oldest
    assert [f.ts_ms for f in ring.last(60)] == [540, 560, 580]
    assert len(ring.last(10_000)) == 10


def test_jitter_buffer_orders_conceals_and_counts_late_frames():
    jb = InboundJitterBuffer(60)
    for ts in (0, 40, 20, 100):                          # reordered, 60 and 80 missing
        jb.push(_f(ts, 1 + ts // 20))
    assert jb.pop_due(50) == []                          # 0 is due at 60
    due = jb.pop_due(70)
    assert [f.ts_ms for f in due] == [0] and due[0].pcm[0] == 1
    due = jb.pop_due(150)
    assert [f.ts_ms for f in due] == [20, 40, 60, 80] and jb.concealed == 2
    assert due[0].pcm[0] == 2 and due[2].pcm == bytes(640) and due[3].pcm == bytes(640)
    jb.push(_f(40))                                      # too late
    assert jb.late == 1
    assert [f.ts_ms for f in jb.pop_due(200)] == [100, 120, 140]


def test_jitter_buffer_before_any_frame_is_empty():
    assert InboundJitterBuffer(60).pop_due(10_000) == []


def test_playout_buffer_releases_with_lead_and_clears():
    pb = PlayoutBuffer(60)
    for i in range(10):
        pb.push(_f(i * 20), "a" if i < 6 else "b")
    assert pb.queued_ms == 200
    out = pb.pop_due(1000)                               # idle: starts now, lead 60: 4 frames (1000..1060)
    assert len(out) == 4 and pb.queued_ms == 120
    assert pb.played_until_ms(1000) == 1000 and pb.played_until_ms(1030) == 1030
    assert pb.played_until_ms(5000) == 1080
    assert len(pb.pop_due(1020)) == 1                    # steady: one more as time advances
    dropped, ids = pb.clear()
    assert dropped == 100                                # 5 frames were still queued
    assert "b" in ids and pb.queued_ms == 0
    assert pb.pop_due(1100) == []
    pb.push(_f(0), "c")
    assert len(pb.pop_due(2000)) == 1                    # new stream after an idle gap
    assert pb.item_played_ms("c", 2010) == 10


def test_playout_buffer_clear_reports_items_with_unplayed_audio():
    pb = PlayoutBuffer(60)
    for i in range(5):
        pb.push(_f(0), "x")
    pb.pop_due(0)                                        # releases 0..60 -> 3-4 frames
    dropped, ids = pb.clear()
    assert ids == ("x",) and dropped == 20          # 4 frames released (0..60 inclusive), 1 still queued
    assert pb.played_until_ms(0) == 0                    # nothing will play after the clear


# --- wav -------------------------------------------------------------------------------------

def test_wav_roundtrip_stereo_and_rate_conversion(tmp_path):
    from voice_gateway.audio.wav import interleave, read_wav, write_wav
    pcm = tone(200, 500)
    p = tmp_path / "a.wav"
    write_wav(p, pcm)
    assert read_wav(p) == pcm
    st = interleave(pcm, pcm[:-640])
    assert len(st) == 2 * len(pcm)
    write_wav(tmp_path / "s.wav", st, 16000, 2)
    mono = read_wav(tmp_path / "s.wav")
    assert len(mono) == len(pcm) and abs(samples(mono)[1000] - samples(pcm)[1000]) <= 1
    write_wav(tmp_path / "r.wav", pcm, 16000)
    assert abs(len(read_wav(tmp_path / "r.wav", 8000)) - len(pcm) // 2) <= 2


def test_wav_reads_mulaw_8bit(tmp_path):
    from voice_gateway.audio.wav import read_wav
    pcm8 = np.frombuffer(tone(300, 500, rate=8000), "<i2").tobytes()
    ulaw = fr.mulaw_encode(pcm8)
    header = (b"RIFF" + struct.pack("<I", 4 + 26 + 8 + len(ulaw) + 12) + b"WAVE" + b"fmt " + struct.pack("<I", 18)
              + struct.pack("<HHIIHHH", 7, 1, 8000, 8000, 1, 8, 0) + b"fact" + struct.pack("<II", 4, len(ulaw))
              + b"data" + struct.pack("<I", len(ulaw)))
    p = tmp_path / "u.wav"
    p.write_bytes(header + ulaw)
    out = read_wav(p, 16000)
    assert abs(len(out) - 2 * len(pcm8)) <= 4
    assert _best_delay_snr(np.frombuffer(pcm8, "<i2").tobytes(), read_wav(p, 8000)) > 20

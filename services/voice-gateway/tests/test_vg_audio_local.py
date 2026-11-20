"""LocalAdapter and the WebSocket adapter (listen builder)."""

import asyncio
import json

import pytest

from test_vg_listen_common import pcm_frames, run, samples, tone
from voice_gateway.audio import frames as fr
from voice_gateway.audio.adapter import TelephonyAdapter
from voice_gateway.audio.local import LocalAdapter, WebSocketAdapter
from voice_gateway.audio.wav import read_wav
from voice_gateway.clock import FakeClock


async def _collect(adapter, out):
    async for f in adapter.caller_frames():
        out.append(f)


def test_local_adapter_is_a_telephony_adapter():
    assert isinstance(LocalAdapter(FakeClock()), TelephonyAdapter)


def test_caller_frames_are_continuous_on_the_clock_and_carry_the_speech():
    pytest.importorskip("numpy")

    async def body(clock):
        a = LocalAdapter(clock)
        await a.accept()
        got = []
        task = asyncio.create_task(_collect(a, got))
        speech = tone(200, 400)
        await a.caller.silence_ms(100)
        await a.caller.say(speech, label="u1")
        await a.caller.silence_ms(100)
        await a.caller.hang_up()
        await task
        return a, got, speech

    a, got, speech = run(body)
    assert [f.ts_ms for f in got] == [i * 20 for i in range(len(got))]
    assert all(f.duration_ms == 20 and f.sample_rate == 16000 for f in got)
    body_pcm = b"".join(f.pcm for f in got)
    assert body_pcm[100 * 32:100 * 32 + len(speech)] == speech
    assert a.closed and [e.kind for e in a.timeline if e.kind.startswith("caller")] == [
        "caller_audio_start", "caller_audio_end", "caller_hangup"]
    assert a.label_at(100) == "u1" and a.label_at(499) == "u1" and a.label_at(500) is None


def test_frame_ts_follow_the_clock_in_paced_mode():
    async def body(clock):
        a = LocalAdapter(clock)
        await a.accept()
        seen = []

        async def reader():
            async for f in a.caller_frames():
                seen.append((f.ts_ms, clock.now_ms()))
                if len(seen) == 10:
                    break
        await reader()
        return seen
    seen = run(body)
    assert all(now == ts + 20 for ts, now in seen)      # a frame exists when its 20 ms are over


def test_assistant_audio_is_recorded_waited_for_and_cleared():
    async def body(clock):
        a = LocalAdapter(clock)
        await a.accept()
        got = []
        reader = asyncio.create_task(_collect(a, got))
        await clock.sleep_ms(100)
        for f in pcm_frames(tone(300, 2000), 0):
            await a.play(f)                              # everything queued at t=100: line end 2100
        t = await a.caller.wait_assistant_audio(min_ms=500)
        assert 600 <= t <= 640
        dropped = await a.clear()
        assert 1400 <= dropped <= 1500
        await a.caller.wait_assistant_quiet(quiet_ms=300)
        await a.hangup("done")
        await reader
        return a

    a = run(body)
    heard = a.assistant_pcm()
    assert any(samples(heard[200 * 32:600 * 32]))
    assert not any(samples(heard[640 * 32:]))                                    # nothing after the clear
    assert any(e.kind == "clear" for e in a.timeline) and a.hangup_reason == "done"
    assert a.closed


def test_wait_assistant_audio_times_out_when_nothing_plays():
    async def body(clock):
        a = LocalAdapter(clock)
        await a.accept()
        with pytest.raises(TimeoutError):
            await a.caller.wait_assistant_audio(min_ms=100, timeout_ms=500)
    run(body)


def test_echo_noise_and_mulaw_line_simulation_and_stereo_wav(tmp_path):
    pytest.importorskip("numpy")

    async def body(clock):
        a = LocalAdapter(clock, wire_format=fr.TELEPHONY_MULAW_8K, echo_db=-20, echo_delay_ms=100,
                         noise_dbfs=-70)
        await a.accept()
        got = []
        reader = asyncio.create_task(_collect(a, got))
        for f in pcm_frames(tone(300, 400, amp=0.5), 40):
            await a.play(f)
        await clock.sleep_ms(1000)
        await a.hangup("x")
        await reader
        return a, got

    a, got = run(body)
    levels = [fr.rms_dbfs(f.pcm) for f in got]
    echo_window = [lv for f, lv in zip(got, levels) if 160 <= f.ts_ms < 520]
    assert max(echo_window) > -50 and max(echo_window) < -20       # the assistant leaks back, -20 dB down
    assert max(levels[-10:]) < -55                                   # then only the noise floor
    p = tmp_path / "call.wav"
    a.save(str(p))
    import wave
    with wave.open(str(p)) as w:
        assert w.getnchannels() == 2 and w.getframerate() == 16000
    assert len(read_wav(p)) > 0


def test_unpaced_mode_runs_without_the_clock():
    async def main():
        clock = FakeClock()
        a = LocalAdapter(clock, paced=False)
        await a.accept()
        got = []
        reader = asyncio.create_task(_collect(a, got))
        await a.caller.say(tone(200, 200))
        await a.caller.hang_up()
        await asyncio.wait_for(reader, 5)
        return clock.now_ms(), len(got)
    now, n = asyncio.run(main())
    assert now == 0 and n >= 10


def test_transfer_follows_the_script():
    async def body(clock):
        a = LocalAdapter(clock, transfer_answers=False)
        out = await a.transfer("+46700000001", 20)
        return out, a.transfers
    out, transfers = run(body)
    assert out.answered is False and out.reason == "no_answer" and transfers == [("+46700000001", 20)]


def test_hang_up_by_the_gateway_ends_the_caller_stream():
    async def body(clock):
        a = LocalAdapter(clock)
        await a.accept()
        got = []
        reader = asyncio.create_task(_collect(a, got))
        await clock.sleep_ms(100)
        await a.hangup("goodbye")
        await asyncio.wait_for(reader, 5)
        return len(got)
    assert 3 <= run(body) <= 6


def test_websocket_adapter_binds_localhost_only():
    with pytest.raises(ValueError):
        WebSocketAdapter(FakeClock(), host="0.0.0.0")


def test_websocket_adapter_roundtrip():
    pytest.importorskip("numpy")
    ws_lib = pytest.importorskip("websockets")
    import base64

    from websockets.asyncio.client import connect
    from voice_gateway.clock import RealClock

    async def main():
        clock = RealClock()
        server = WebSocketAdapter(clock, port=0)
        # port 0: let the OS choose; read it back from the started server via a free-port helper
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        server = WebSocketAdapter(clock, port=port)
        agen = server.serve()
        first = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0.2)
        async with connect(f"ws://127.0.0.1:{port}") as client:
            await client.send(json.dumps({"event": "start", "callRef": "ws-test", "dialed": "+46000000000",
                                          "caller": None, "format": "mulaw8k"}))
            call = await asyncio.wait_for(first, 5)
            info = await call.accept()
            assert info.call_ref == "ws-test" and info.caller is None and info.wire_format == fr.TELEPHONY_MULAW_8K
            speech = fr.mulaw_encode(bytes(320))
            for seq in range(15):
                await client.send(json.dumps({"event": "media", "seq": seq,
                                              "payload": base64.b64encode(speech).decode()}))
            frames = []
            async for f in call.caller_frames():
                frames.append(f)
                if len(frames) >= 8:
                    break
            assert all(f.duration_ms == 20 and f.sample_rate == 16000 for f in frames)
            await call.play(fr.Frame(bytes(640), 0))
            msg = json.loads(await asyncio.wait_for(client.recv(), 5))
            assert msg["event"] == "media" and len(base64.b64decode(msg["payload"])) == 160
            await call.hangup("bye")
            assert json.loads(await asyncio.wait_for(client.recv(), 5))["event"] == "hangup"
        await agen.aclose()
    asyncio.run(asyncio.wait_for(main(), 20))

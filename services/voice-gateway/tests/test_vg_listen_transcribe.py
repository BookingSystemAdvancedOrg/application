"""Transcribe client with fake streams, the scripted fake and the manager (listen builder)."""

import asyncio
import dataclasses

import pytest

from test_vg_listen_common import pcm_frames, run, silence_pcm, tone
from voice_gateway.audio.buffers import RingBuffer
from voice_gateway.audio.frames import Frame
from voice_gateway.config import SttConfig
from voice_gateway.events import EventBus, SttFinal, SttPartial
from voice_gateway.listen.transcribe import (FakeSttFactory, ScriptedUtterance, SttMode, SttUnavailable, Transcriber)


def _labelled(spans):
    """(start, end, label) spans -> a labeller."""
    return lambda ts: next((lab for a, b, lab in spans if a <= ts < b), None)


async def _drain(stream, n=None):
    out = []
    async for ev in stream.results():
        out.append(ev)
    return out


# --- FakeSttFactory ---------------------------------------------------------------------------

def test_fake_emits_partials_word_by_word_and_the_final_after_the_segment():
    async def main():
        f = FakeSttFactory([ScriptedUtterance("u1", "jag vill boka bord", "sv")], labeller=_labelled([(1000, 2200, "u1")]))
        s = await f.open(SttMode(language="sv"), stream_id=1, audio_origin_ms=0)
        for fr in pcm_frames(tone(200, 1200), 1000) + pcm_frames(silence_pcm(800), 2200):
            await s.feed(fr)
        await s.close()
        return await _drain(s)

    evs = asyncio.run(main())
    partials = [e for e in evs if isinstance(e, SttPartial)]
    (final,) = [e for e in evs if isinstance(e, SttFinal)]
    assert [p.text for p in partials] == ["jag", "jag vill", "jag vill boka", "jag vill boka bord"]
    assert partials[0].at_ms == 1333 and partials[1].at_ms == 1666 and partials[2].at_ms == 2000
    assert final.text == "jag vill boka bord" and final.start_ms == 1000 and final.end_ms == 2200
    assert final.at_ms == 2200 + 250 and final.confidence == 0.9 and final.language == "sv"
    assert partials[1].stable_text == "jag"
    assert all(a.at_ms <= b.at_ms for a, b in zip(evs, evs[1:]))


def test_fake_without_a_labeller_maps_energy_segments_to_the_script_in_order():
    async def main():
        f = FakeSttFactory([ScriptedUtterance("a", "hej", "sv"), ScriptedUtterance("b", "ett bord", "sv")])
        s = await f.open(SttMode(language="sv"), stream_id=1, audio_origin_ms=0)
        pcm = silence_pcm(300) + tone(200, 600) + silence_pcm(1000) + tone(210, 800) + silence_pcm(1000)
        for fr in pcm_frames(pcm):
            await s.feed(fr)
        await s.close()
        return [e for e in await _drain(s) if isinstance(e, SttFinal)], f
    finals, f = asyncio.run(main())
    assert [x.text for x in finals] == ["hej", "ett bord"] and finals[0].start_ms == 300


def test_fake_replay_on_a_new_stream_hears_the_same_segment_again():
    async def main():
        f = FakeSttFactory([ScriptedUtterance("a", "hej hej", "sv")])
        pcm = tone(200, 600) + silence_pcm(900)
        out = []
        for sid in (1, 2):
            s = await f.open(SttMode(language="sv"), stream_id=sid, audio_origin_ms=0)
            for fr in pcm_frames(pcm):
                await s.feed(fr)
            await s.close()
            out.append([e for e in await _drain(s) if isinstance(e, SttFinal)])
        return out
    first, second = asyncio.run(main())
    assert first[0].text == second[0].text == "hej hej" and second[0].stream_id == 2


def test_fake_language_modes_identify_scores_and_wrong_language_confidence():
    async def main():
        line = ScriptedUtterance("e", "hello I would like a table", "en", (("en", 0.97), ("sv", 0.02)), 0.88)
        f = FakeSttFactory([line], labeller=_labelled([(0, 1000, "e")]))
        res = {}
        for name, mode in (("identify", SttMode(identify=("sv", "en", "de"), preferred="sv")),
                           ("fixed_en", SttMode(language="en")), ("fixed_sv", SttMode(language="sv")),
                           ("identify_without_en", SttMode(identify=("sv", "de")))):
            s = await f.open(mode, stream_id=1, audio_origin_ms=0)
            for fr in pcm_frames(tone(200, 1000)):
                await s.feed(fr)
            await s.close()
            res[name] = [e for e in await _drain(s) if isinstance(e, SttFinal)][0]
        return res, f
    res, f = asyncio.run(main())
    assert res["identify"].language == "en" and res["identify"].language_scores[0] == ("en", 0.97)
    assert res["fixed_en"].confidence == 0.88 and res["fixed_en"].language_scores == ()
    assert res["fixed_sv"].confidence == 0.3 and res["fixed_sv"].language == "sv"     # sv stream hears English: poor
    assert res["identify_without_en"].language == "sv" and res["identify_without_en"].confidence == 0.4
    assert len(f.opened) == 4


# --- Transcriber with the fake -----------------------------------------------------------------

class Rig:
    def __init__(self, clock, script, spans, cfg=None, factory=None):
        self.clock = clock
        self.ring = RingBuffer(12000)
        self.bus = EventBus()
        self.bus.history = []
        self.factory = factory or FakeSttFactory(script, labeller=_labelled(spans))
        self.t = Transcriber(self.factory, self.ring, cfg or SttConfig(), self.bus, clock)

    async def feed(self, pcm, start):
        for fr in pcm_frames(pcm, start):
            self.ring.push(fr)
            await self.t.feed(fr)
            await self.clock.sleep_ms(20)

    def events(self, kind):
        return [e for e in self.bus.history if isinstance(e, kind)]


def test_transcriber_publishes_results_stamped_with_the_arrival_time():
    async def body(clock):
        r = Rig(clock, [ScriptedUtterance("u", "tisdag klockan sju", "sv")], [(0, 1500, "u")])
        await r.t.open("sv")
        await r.feed(tone(200, 1500) + silence_pcm(800), 0)
        await r.t.close()
        return r
    r = run(body)
    finals = r.events(SttFinal)
    assert [f.text for f in finals] == ["tisdag klockan sju"] and r.t.language == "sv" and not r.t.identifying
    assert 1500 + 250 <= finals[0].at_ms <= 1500 + 250 + 60              # stamped when it arrived
    assert finals[0].start_ms == 0 and finals[0].end_ms == 1500          # audio times unchanged
    assert [p.text for p in r.events(SttPartial)][-1] == "tisdag klockan sju"


def test_identify_stream_then_reopen_with_replay_re_transcribes_the_utterance_and_drops_old_results():
    async def body(clock):
        line = ScriptedUtterance("e", "hello I would like to book a table", "en", (("en", 0.96),), 0.9)
        r = Rig(clock, [line], [(0, 2000, "e")])
        await r.t.open_identify(("sv", "en", "ar"), preferred="sv")
        assert r.t.identifying
        await r.feed(tone(200, 2000), 0)
        sid = await r.t.reopen("en", replay_from_ms=0)
        assert not r.t.identifying and r.t.language == "en"
        await r.feed(silence_pcm(1500), 2000)                            # live audio goes to the new stream
        await clock.sleep_ms(1500)
        await r.t.close()
        return r, sid
    r, sid = run(body)
    modes = r.factory.opened
    assert modes[0].identify == ("sv", "en", "ar") and modes[0].preferred == "sv" and modes[1].language == "en"
    finals = r.events(SttFinal)
    new = [f for f in finals if f.stream_id == sid]
    assert new and new[0].text == "hello I would like to book a table" and new[0].confidence == 0.9
    assert new[0].language == "en" and new[0].start_ms == 0                # the replayed audio kept its call times
    assert [f.stream_id for f in finals].count(1) == 0                     # the identify stream's final never came out


def test_reopen_without_replay_and_backlog_order():
    async def body(clock):
        r = Rig(clock, [], [])
        await r.t.open("sv")
        await r.feed(tone(200, 400), 0)
        fed = []
        orig = r.factory.open

        async def spy(mode, *, stream_id, audio_origin_ms):
            s = await orig(mode, stream_id=stream_id, audio_origin_ms=audio_origin_ms)
            real_feed = s.feed

            async def feed(frame):
                fed.append((stream_id, frame.ts_ms))
                await real_feed(frame)
            s.feed = feed
            return s
        r.factory.open = spy
        await r.t.reopen("sv", replay_from_ms=200)                       # replay 200..400, live goes to the backlog
        await r.feed(tone(200, 200), 400)
        await clock.sleep_ms(800)
        await r.t.close()
        return fed
    fed = run(body)
    ts = [t for sid, t in fed if sid == 2]
    assert ts == sorted(ts) and ts[0] == 200 and ts[-1] == 580 and len(ts) == len(set(ts)) == 20   # no gap, no dupes


def test_frames_the_new_stream_would_get_twice_are_not_replayed():
    async def body(clock):
        r = Rig(clock, [], [])
        await r.t.open("sv")
        await r.feed(tone(200, 200), 0)
        r.ring.push(Frame(bytes(640), 200))                              # in the ring, not yet fed (Listener order)
        await r.t.reopen("en", replay_from_ms=0)
        await r.t.feed(Frame(bytes(640), 200))
        await clock.sleep_ms(500)
        return r
    r = run(body)
    assert r.t._fed_end == 220


def test_probe_returns_the_language_evidence_from_a_separate_stream():
    async def body(clock):
        line = ScriptedUtterance("e", "hello I would like to book a table", "en", (("en", 0.9), ("sv", 0.06)))
        r = Rig(clock, [line], [(0, 2000, "e")])
        await r.t.open("sv")
        await r.feed(tone(200, 2000) + silence_pcm(300), 0)
        live_before = r.t._live
        ev = await r.t.probe(("sv", "en"), replay_from_ms=0)
        return r, ev, live_before
    r, ev, live_before = run(body)
    assert ev.source == "transcribe_lid" and ev.code == "en" and ev.score == pytest.approx(0.9)
    assert ev.content_words == 8 and ev.speech_ms >= 1900
    assert r.t.last_probe_scores[0][0] == "en" and r.t._live is live_before
    assert r.factory.opened[-1].identify == ("sv", "en")
    assert r.events(SttFinal) == [] or all(f.stream_id == 1 for f in r.events(SttFinal))   # nothing from the probe


def test_probe_without_buffered_audio_or_candidates_is_none():
    async def body(clock):
        r = Rig(clock, [], [])
        await r.t.open("sv")
        assert await r.t.probe(("sv", "en"), 0) is None
        await r.feed(tone(200, 200), 0)
        assert await r.t.probe(("sv",), 0) is None
    run(body)


def test_a_stream_error_reopens_once_with_replay_and_a_second_one_raises():
    broken_streams = {1}
    received: dict[int, list[int]] = {}

    class Flaky(FakeSttFactory):
        async def open(self, mode, *, stream_id, audio_origin_ms):
            s = await super().open(mode, stream_id=stream_id, audio_origin_ms=audio_origin_ms)
            real = s.feed

            async def feed(frame):
                if stream_id in broken_streams:
                    raise RuntimeError("connection reset")
                received.setdefault(stream_id, []).append(frame.ts_ms)
                await real(frame)
            s.feed = feed
            return s

    async def body(clock):
        f = Flaky([], labeller=_labelled([]))
        r = Rig(clock, [], [], factory=f)
        await r.t.open("sv")
        await r.feed(tone(200, 100), 0)                 # the first stream dies: one automatic recovery
        await clock.sleep_ms(300)
        assert len(f.opened) == 2 and r.t._unavailable is None
        await r.feed(tone(200, 100), 100)
        await clock.sleep_ms(300)
        broken_streams.add(2)                           # the recovered stream dies too
        with pytest.raises(SttUnavailable):
            await r.feed(tone(200, 200), 200)           # the error at the first frame surfaces at the next one
    run(body)
    assert received[2][:5] == [0, 20, 40, 60, 80]       # the recovered stream got the audio back


def test_opening_is_retried_once_then_the_stt_is_unavailable():
    class Down:
        calls = 0

        async def open(self, mode, *, stream_id, audio_origin_ms):
            Down.calls += 1
            raise ConnectionError("no route")

    async def body(clock):
        r = Rig(clock, [], [], factory=Down())
        with pytest.raises(SttUnavailable):
            await r.t.open("sv")
    run(body)
    assert Down.calls == 2


def test_a_stalled_stream_gets_a_silence_frame_before_the_next_audio():
    async def body(clock):
        r = Rig(clock, [], [], cfg=SttConfig(idle_keepalive_ms=1000))
        await r.t.open("sv")
        sent = []
        real = r.t._live.feed

        async def spy(frame):
            sent.append(frame)
            await real(frame)
        r.t._live.feed = spy
        await r.t.feed(Frame(bytes(640), 0))
        await clock.sleep_ms(1500)
        await r.t.feed(Frame(tone(200, 20), 1500))
        return sent
    sent = run(body)
    assert len(sent) == 3 and sent[1].pcm == bytes(640) and sent[1].ts_ms == 1500


def test_a_hung_send_counts_as_a_dead_stream_and_the_frame_loop_goes_on(monkeypatch):
    """A send that never returns (a stalled HTTP/2 connection) must not freeze VAD and barge-in: it times out, the
    stream is reopened with replay and the dead one is not fed again."""
    from voice_gateway.listen import transcribe as tr
    monkeypatch.setattr(tr, "FEED_TIMEOUT_S", 0.05)
    received: dict[int, list[int]] = {}
    hung_calls = []

    class Hung(FakeSttFactory):
        async def open(self, mode, *, stream_id, audio_origin_ms):
            s = await super().open(mode, stream_id=stream_id, audio_origin_ms=audio_origin_ms)
            real = s.feed

            async def feed(frame):
                if stream_id == 1:
                    hung_calls.append(frame.ts_ms)
                    await asyncio.Event().wait()
                received.setdefault(stream_id, []).append(frame.ts_ms)
                await real(frame)
            s.feed = feed
            return s

    async def body(clock):
        f = Hung([], labeller=_labelled([]))
        r = Rig(clock, [], [], factory=f)
        await r.t.open("sv")
        await r.feed(tone(200, 200), 0)
        await clock.sleep_ms(400)
        assert len(f.opened) == 2 and r.t._unavailable is None
        await r.feed(tone(200, 100), 200)
        await clock.sleep_ms(400)
    run(body)
    assert hung_calls == [0]                             # the dead stream is not fed again
    assert received[2][:3] == [0, 20, 40] and 280 in received[2]   # replayed, then live audio


def test_a_switch_whose_stream_will_not_open_keeps_the_healthy_stream():
    class Picky(FakeSttFactory):
        async def open(self, mode, *, stream_id, audio_origin_ms):
            if mode.language == "cy":
                raise ValueError("unsupported language code")
            return await super().open(mode, stream_id=stream_id, audio_origin_ms=audio_origin_ms)

    async def body(clock):
        r = Rig(clock, [], [], factory=Picky([], labeller=_labelled([])))
        await r.t.open("sv")
        await r.feed(tone(200, 200), 0)
        live = r.t._live
        sid = await r.t.reopen("cy", replay_from_ms=0)
        await r.feed(tone(200, 100), 200)                # still fed, no SttUnavailable
        return r, live, sid
    r, live, sid = run(body)
    assert r.t._live is live and sid == 1 and r.t.language == "sv" and r.t._unavailable is None

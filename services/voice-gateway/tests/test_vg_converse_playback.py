"""SpeechQueue, OutputMixer and Speaker on a fake line and FakeClock
(builder 'converse'): order, synth-ahead, TTS failure, one source at a
time, backchannel cut, filler, barge-in flush, PlaybackView."""

import asyncio

from voice_gateway.backchannel.clips import ClipCache
from voice_gateway.clock import FakeClock
from voice_gateway.config import BackchannelConfig, GatewayConfig, SpeakConfig
from voice_gateway.events import (BackchannelRequest, CallState, EventBus, PlaybackFinished, PlaybackFlushed,
                                  PlaybackStarted, SttPartial, StateChanged, ToolFinished, ToolStarted, TurnEnd,
                                  VadPause, VadResume, VadStart)
from voice_gateway.speak.mixer import OutputMixer
from voice_gateway.speak.queue import SpeechItem, SpeechQueue
from voice_gateway.speak.speaker import Speaker
from voice_gateway.speak.tts import FakeTts

CFG = GatewayConfig()


class Line:
    """A line that plays frames back to back at the clock's pace (like LocalAdapter)."""

    def __init__(self, clock):
        self.clock, self.frames, self.line_end, self.clears, self._closed = clock, [], 0, [], False

    async def play(self, frame):
        start = max(self.line_end, self.clock.now_ms())
        self.frames.append((start, frame.pcm))
        self.line_end = start + 20

    async def clear(self):
        now = self.clock.now_ms()
        dropped = max(0, self.line_end - now)
        self.frames = [(t, p) for t, p in self.frames if t < now]
        self.line_end = now
        self.clears.append(now)
        return dropped

    @property
    def closed(self):
        return self._closed

    def heard_ms(self):
        return len(self.frames) * 20


def run(body, *, tts=None):
    async def main():
        clock = FakeClock()
        driver = asyncio.create_task(clock.drive())
        try:
            return await body(clock, tts or FakeTts())
        finally:
            driver.cancel()

    return asyncio.run(main())


async def warm_clips(tts):
    clips = ClipCache(tts, CFG.speak, CFG.backchannel)
    await clips.warm(("sv", "en"))
    tts.calls.clear()
    return clips


def intervals(history):
    """(start, end, item_id, kind) per played item from Started/Finished events."""
    starts = {e.item_id: e for e in history if isinstance(e, PlaybackStarted)}
    out = []
    for e in history:
        if isinstance(e, PlaybackFinished):
            s = starts[e.item_id]
            out.append((s.at_ms, s.at_ms + e.played_ms, e.item_id, e.kind))
    return sorted(out)


def assert_no_overlap(history):
    spans = intervals(history)
    for (a0, a1, a, _), (b0, b1, b, _) in zip(spans, spans[1:]):
        assert a1 <= b0, f"{a} [{a0},{a1}) overlaps {b} [{b0},{b1})"


async def setup(clock, tts, cfg=CFG):
    bus = EventBus()
    bus.history = []
    line = Line(clock)
    clips = await warm_clips(tts)
    queue = SpeechQueue(tts, cfg.speak, clock)
    mixer = OutputMixer(cfg, line, queue, clips, bus, clock)
    task = asyncio.create_task(mixer.run())
    return bus, line, queue, mixer, task


# --- queue -----------------------------------------------------------------------------------

def test_queue_yields_in_order_streams_the_first_and_synthesises_ahead():
    async def body(clock, tts):
        q = SpeechQueue(tts, SpeakConfig(synth_ahead=2), clock)
        texts = ["Vi har plats på fredag klockan sju.", "Passar det?", "Annars lördag.", "Säg till."]
        for i, t in enumerate(texts):
            q.enqueue(SpeechItem(f"t1-s{i}", t, "sv", turn=1))
        got, calls_at_first = [], None
        async for ra in q.ready():
            if calls_at_first is None:
                calls_at_first = len(tts.calls)
            got.append(ra)
            if ra.item.item_id == "t1-s3" and ra.last:
                break
        return got, calls_at_first, q.pending

    tts = FakeTts(clock=None)
    got, calls_at_first, pending = run(body, tts=tts)
    ids = [ra.item.item_id for ra in got]
    assert ids == sorted(ids, key=lambda x: int(x[-1]))
    first = [ra for ra in got if ra.item.item_id == "t1-s0"]
    assert len(first) > 2 and first[-1].last and first[-1].pcm == b""      # streamed in chunks
    assert all(not ra.last for ra in first[:-1])
    for i in (1, 2, 3):
        rest = [ra for ra in got if ra.item.item_id == f"t1-s{i}"]
        assert len(rest) == 1 and rest[0].last and len(rest[0].pcm) % 640 == 0
    assert calls_at_first <= 3 and pending == 0                              # 1 + synth_ahead


def test_queue_tts_failure_is_retried_then_replaced_by_the_error_sentence_once_per_turn():
    from shared.features.phone_agent import copy as pa_copy

    async def body(clock, tts):
        tts.fail["Trasig mening ett."] = 99
        tts.fail["Trasig mening två."] = 99
        tts.fail["Hiccup."] = 1
        q = SpeechQueue(tts, SpeakConfig(), clock)
        q.enqueue(SpeechItem("t1-s0", "Hiccup.", "sv", turn=1))
        q.enqueue(SpeechItem("t1-s1", "Trasig mening ett.", "sv", turn=1))
        q.enqueue(SpeechItem("t1-s2", "Trasig mening två.", "sv", turn=1))
        q.enqueue(SpeechItem("t2-s0", "Trasig mening ett.", "en", turn=2))
        out = {}
        async for ra in q.ready():
            out[ra.item.item_id] = out.get(ra.item.item_id, b"") + ra.pcm
            if ra.item.item_id == "t2-s0" and ra.last:
                break
        return out, q.failures

    tts = FakeTts()
    out, failures = run(body, tts=tts)
    assert out["t1-s0"]                                   # one retry was enough
    err_sv = len(pa_copy.ERROR["sv"]) * 55 * 32
    assert abs(len(out["t1-s1"]) - err_sv) <= 640         # the Swedish error sentence instead
    assert out["t1-s2"] == b""                            # once per turn: not twice
    assert (pa_copy.ERROR["en"], "en") in tts.calls       # its own language, never another
    assert (pa_copy.ERROR["sv"], "en") not in tts.calls
    assert failures == 3


def test_queue_no_voice_never_falls_back():
    async def body(clock, tts):
        q = SpeechQueue(tts, SpeakConfig(), clock)
        q.enqueue(SpeechItem("t1-s0", "salam", "fa", turn=1))
        q.enqueue(SpeechItem("t1-s1", "Hej igen, vad kan jag hjälpa till med?", "sv", turn=1))
        got = []
        async for ra in q.ready():
            got.append(ra)
            if ra.item.item_id == "t1-s1" and ra.last:
                break
        return got

    got = run(body)
    fa = [ra for ra in got if ra.item.language == "fa"]
    assert len(fa) == 1 and fa[0].pcm == b"" and fa[0].last


def test_queue_cancel_returns_unplayed_items_and_keeps_working():
    async def body(clock, tts):
        q = SpeechQueue(tts, SpeakConfig(), clock)
        for i in range(3):
            q.enqueue(SpeechItem(f"t1-s{i}", f"Mening nummer {i} som är lång nog.", "sv", turn=1))
        it = q.ready().__aiter__()
        first = await it.__anext__()
        dropped = q.cancel()
        q.enqueue(SpeechItem("t2-s0", "Ny mening efter avbrottet.", "sv", turn=2))
        nxt = await it.__anext__()
        return first, dropped, nxt, q.pending

    first, dropped, nxt, pending = run(body)
    assert first.item.item_id == "t1-s0"
    assert [i.item_id for i in dropped] == ["t1-s0", "t1-s1", "t1-s2"]
    assert nxt.item.item_id == "t2-s0" and pending == 1


# --- mixer -----------------------------------------------------------------------------------

def test_mixer_plays_sentences_back_to_back_in_order():
    async def body(clock, tts):
        bus, line, queue, mixer, task = await setup(clock, tts)
        for i, t in enumerate(["Vi har plats på fredag klockan sju.", "Passar det?"]):
            queue.enqueue(SpeechItem(f"t1-s{i}", t, "sv", turn=1))
        assert mixer.busy
        await clock.sleep_ms(60)
        playing = mixer.assistant_playing, mixer.assistant_item_started_ms
        level = mixer.playback_dbfs_at(clock.now_ms() - 10)
        await mixer.drain()
        task.cancel()
        return bus.history, line, playing, level, mixer

    history, line, playing, level, mixer = run(body)
    spans = intervals(history)
    assert [s[2] for s in spans] == ["t1-s0", "t1-s1"]
    assert spans[0][1] == spans[1][0]                     # back to back, no gap, no overlap
    assert all(not e.interrupted for e in history if isinstance(e, PlaybackFinished))
    assert playing[0] and playing[1] is not None and abs(level + 20) < 1
    assert line.heard_ms() == sum(s[1] - s[0] for s in spans)
    assert not mixer.busy and not mixer.assistant_playing and mixer.overlaps == 0
    assert mixer.playback_dbfs_at(10**6) == -120.0


def test_first_audio_leaves_at_once_on_an_idle_line():
    async def body(clock, tts):
        bus, line, queue, mixer, task = await setup(clock, tts)
        await clock.sleep_ms(1010)
        queued_at = clock.now_ms()
        queue.enqueue(SpeechItem("t1-s0", "Hej, vad kan jag hjälpa till med?", "sv", turn=1))
        await clock.sleep_ms(5)
        sent = [t for t, _ in line.frames]
        await mixer.drain()
        task.cancel()
        return queued_at, sent, bus.history

    queued_at, sent, history = run(body)
    first = next(e for e in history if isinstance(e, PlaybackStarted))
    assert first.at_ms == queued_at and sent and sent[0] == queued_at


def test_backchannel_plays_only_on_an_idle_line_and_never_over_the_caller():
    async def body(clock, tts):
        bus, line, queue, mixer, task = await setup(clock, tts)
        await clock.sleep_ms(1000)
        ok = mixer.request_backchannel(BackchannelRequest(clock.now_ms(), "sv", "mm"))
        again = mixer.request_backchannel(BackchannelRequest(clock.now_ms(), "sv", "ja"))   # one at a time
        await mixer.drain()
        bus.publish(VadStart(clock.now_ms(), clock.now_ms() - 100, 0.9))
        over_caller = mixer.request_backchannel(BackchannelRequest(clock.now_ms(), "sv", "ja"))
        bus.publish(VadPause(clock.now_ms(), clock.now_ms(), 3000))
        missing = mixer.request_backchannel(BackchannelRequest(clock.now_ms(), "fa", "mm"))
        queue.enqueue(SpeechItem("t1-s0", "Jag kollar det åt dig nu.", "sv", turn=1))
        while_queued = mixer.request_backchannel(BackchannelRequest(clock.now_ms(), "sv", "ja"))
        await mixer.drain()
        task.cancel()
        return ok, again, over_caller, missing, while_queued, bus.history

    ok, again, over_caller, missing, while_queued, history = run(body)
    assert ok and not again and not over_caller and not missing and not while_queued
    kinds = [e.kind for e in history if isinstance(e, PlaybackStarted)]
    assert kinds == ["backchannel", "assistant"]
    assert_no_overlap(history)


def test_assistant_never_waits_long_on_a_backchannel():
    async def body(clock, tts):
        cfg = GatewayConfig()
        bus, line, queue, mixer, task = await setup(clock, tts, cfg)
        await clock.sleep_ms(1000)
        long_clip = mixer.clips.backchannel("sv", "precis")       # 385 ms of audio
        assert mixer.request_backchannel(BackchannelRequest(clock.now_ms(), "sv", "precis"))
        await clock.sleep_ms(40)
        ready_at = clock.now_ms()
        queue.enqueue(SpeechItem("t1-s0", "Vi har plats på fredag klockan sju.", "sv", turn=1))
        await mixer.drain()
        task.cancel()
        return bus.history, ready_at, long_clip, cfg, mixer

    history, ready_at, clip, cfg, mixer = run(body)
    bc = next(e for e in history if isinstance(e, PlaybackFinished) and e.kind == "backchannel")
    assert bc.interrupted and bc.played_ms < clip.duration_ms
    first = next(e for e in history if isinstance(e, PlaybackStarted) and e.kind == "assistant")
    assert first.at_ms - ready_at <= cfg.speak.backchannel_max_wait_ms + cfg.audio.playout_lead_ms + 40
    assert_no_overlap(history)
    assert mixer.cut_backchannels == 1


def test_filler_plays_to_its_end_then_the_answer():
    async def body(clock, tts):
        bus, line, queue, mixer, task = await setup(clock, tts)
        assert mixer.request_filler("sv", 0)
        assert not mixer.request_filler("sv", 1)                  # one source at a time
        await clock.sleep_ms(100)
        queue.enqueue(SpeechItem("t1-s0", "Det finns ett bord klockan sju.", "sv", turn=1))
        await mixer.drain()
        queue.enqueue(SpeechItem("t2-s0", "Något mer jag kan hjälpa till med?", "sv", turn=2))
        refused = not mixer.request_filler("sv", 1)               # assistant audio queued first
        await mixer.drain()
        task.cancel()
        return bus.history, refused

    history, refused = run(body)
    filler = next(e for e in history if isinstance(e, PlaybackFinished) and e.kind == "filler")
    assert not filler.interrupted and refused
    assert [s[3] for s in intervals(history)] == ["filler", "assistant", "assistant"]
    assert_no_overlap(history)


def test_barge_in_flush_stops_fast_and_reports_unplayed_items():
    async def body(clock, tts):
        bus, line, queue, mixer, task = await setup(clock, tts)
        texts = ["Då har jag fyra personer på fredag klockan sju.", "På namnet Anna.", "Stämmer det?"]
        for i, t in enumerate(texts):
            queue.enqueue(SpeechItem(f"t3-s{i}", t, "sv", turn=3))
        await clock.sleep_ms(3000)                                # s0 (2.6 s) heard, inside s1
        barge_at = clock.now_ms() - 120
        flushed = await mixer.flush(barge_at)
        after_flush = clock.now_ms()
        await clock.sleep_ms(500)
        silent = all(t < after_flush for t, _ in line.frames)
        queue.enqueue(SpeechItem("t4-s0", "Okej, vad vill du ändra på?", "sv", turn=4))
        await mixer.drain()
        task.cancel()
        return bus.history, flushed, silent, after_flush, mixer

    history, flushed, silent, after_flush, mixer = run(body)
    assert isinstance(flushed, PlaybackFlushed) and flushed.stop_ms <= 150
    assert silent
    assert set(flushed.unplayed_item_ids) == {"t3-s1", "t3-s2"}
    cut = [e for e in history if isinstance(e, PlaybackFinished) and e.interrupted]
    assert cut and all(e.at_ms == after_flush for e in cut)
    assert [s[2] for s in intervals(history)][-1] == "t4-s0"
    assert_no_overlap(history)
    assert not mixer.busy


def test_flush_in_the_middle_of_a_sentence_counts_it_unheard():
    async def body(clock, tts):
        bus, line, queue, mixer, task = await setup(clock, tts)
        queue.enqueue(SpeechItem("t1-s0", "Då har jag fyra personer på fredag klockan sju.", "sv", turn=1))
        await clock.sleep_ms(1000)
        flushed = await mixer.flush(clock.now_ms())
        task.cancel()
        return flushed, bus.history

    flushed, history = run(body)
    assert flushed.unplayed_item_ids == ("t1-s0",)
    fin = next(e for e in history if isinstance(e, PlaybackFinished))
    assert fin.interrupted and 900 <= fin.played_ms <= 1060
    assert flushed.dropped_ms <= GatewayConfig().audio.playout_lead_ms


# --- speaker ---------------------------------------------------------------------------------

def test_speaker_backchannels_a_monologue_and_fills_a_slow_tool():
    async def body(clock, tts):
        bus = EventBus()
        bus.history = []
        line = Line(clock)
        clips = await warm_clips(tts)
        sp = Speaker(CFG, line, tts, clips, bus, clock, language="sv", rng_seed=3)
        task = asyncio.create_task(sp.run())
        bus.publish(StateChanged(clock.now_ms(), CallState.GREETING, CallState.LISTENING))
        await clock.sleep_ms(2000)
        t0 = clock.now_ms()
        bus.publish(VadStart(t0 + 100, t0, 0.9))
        words = "vi är fyra personer som vill komma på fredag kväll efter jobbet om det går bra"
        bus.publish(SttPartial(t0 + 1500, "a", words, words, "sv", 1))
        await clock.sleep_ms(3000)
        bus.publish(VadPause(clock.now_ms() + 80, clock.now_ms(), 3000))
        bus.publish(SttPartial(clock.now_ms() + 100, "a", words, words, "sv", 1))   # the recogniser caught up
        await clock.sleep_ms(500)
        bus.publish(VadResume(clock.now_ms(), 500))
        await clock.sleep_ms(1000)
        bus.publish(VadPause(clock.now_ms() + 80, clock.now_ms(), 4000))
        await clock.sleep_ms(600)
        bus.publish(TurnEnd(clock.now_ms(), words, "sv", t0, clock.now_ms() - 600, "silence"))
        bus.publish(StateChanged(clock.now_ms(), CallState.LISTENING, CallState.THINKING))
        bus.publish(ToolStarted(clock.now_ms(), 1, "get_availability"))
        tool_at = clock.now_ms()
        await clock.sleep_ms(1500)
        item = sp.say("Ja, det finns ett bord klockan sju.", "sv", turn=1, index=0)
        await sp.drain()
        busy = sp.busy
        task.cancel()
        return bus.history, tool_at, item, busy

    history, tool_at, item, busy = run(body)
    bcs = [e for e in history if isinstance(e, BackchannelRequest)]
    assert len(bcs) == 1 and bcs[0].token in {"mm", "mhm", "ja", "okej", "precis"}
    filler = next(e for e in history if isinstance(e, PlaybackStarted) and e.kind == "filler")
    assert filler.at_ms - tool_at >= CFG.speak.filler_delay_ms
    assert item == "t1-s0" and not busy
    assert [s[3] for s in intervals(history)] == ["backchannel", "filler", "assistant"]
    assert_no_overlap(history)


def test_speaker_skips_the_filler_when_the_answer_comes_in_time():
    async def body(clock, tts):
        bus = EventBus()
        bus.history = []
        clips = await warm_clips(tts)
        sp = Speaker(CFG, Line(clock), tts, clips, bus, clock, rng_seed=1)
        task = asyncio.create_task(sp.run())
        await clock.sleep_ms(100)
        bus.publish(ToolStarted(clock.now_ms(), 1, "get_availability"))
        await clock.sleep_ms(250)
        bus.publish(ToolFinished(clock.now_ms(), 1, "get_availability", True, 250))
        await clock.sleep_ms(50)
        sp.say("Det finns plats klockan sju på fredag.", "sv", turn=1)
        await sp.drain()
        await clock.sleep_ms(2000)
        sp.set_language("en")
        task.cancel()
        return bus.history, sp

    history, sp = run(body)
    assert not any(isinstance(e, PlaybackStarted) and e.kind == "filler" for e in history)
    assert sp.backchannels.language == "en"


class SlowLine(Line):
    """play() yields to the loop (a network adapter), so a flush can land mid-frame."""

    async def play(self, frame):
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await super().play(frame)


def test_barge_in_storm_never_overlaps_or_leaks_items():
    import random

    async def body(clock, tts):
        bus = EventBus()
        bus.history = []
        line = SlowLine(clock)
        clips = await warm_clips(tts)
        queue = SpeechQueue(tts, CFG.speak, clock)
        mixer = OutputMixer(CFG, line, queue, clips, bus, clock)
        task = asyncio.create_task(mixer.run())
        rng = random.Random(11)
        for turn in range(30):
            for i in range(rng.randint(1, 3)):
                queue.enqueue(SpeechItem(f"t{turn}-s{i}", "Mening " * rng.randint(1, 8), "sv", turn=turn))
            if rng.random() < 0.3:
                mixer.request_backchannel(BackchannelRequest(clock.now_ms(), "sv", "mm"))
            await clock.sleep_ms(rng.randint(0, 900))
            if rng.random() < 0.7:
                await mixer.flush(clock.now_ms())
        await mixer.drain()
        task.cancel()
        return bus.history, mixer

    history, mixer = run(body)
    started = [e.item_id for e in history if isinstance(e, PlaybackStarted)]
    finished = [e.item_id for e in history if isinstance(e, PlaybackFinished)]
    assert sorted(started) == sorted(finished) and len(started) == len(set(started))
    assert_no_overlap(history)
    assert not mixer.busy and mixer.overlaps == 0

"""The Listener end to end on LocalAdapter, FakeSttFactory and a fake mixer view (listen builder).
Synthetic speech and the energy VAD: no AWS, no numpy, deterministic on the FakeClock."""

import asyncio

import pytest

from test_vg_listen_common import noise, pcm_frames, run, silence_pcm, tone
from voice_gateway.audio import frames as fr
from voice_gateway.audio.local import LocalAdapter
from voice_gateway.config import GatewayConfig, LanguageConfig, VadConfig
from voice_gateway.events import (AssistantContext, BargeIn, EventBus, LanguageAction, LanguageDecided,
                                  LanguageEvidence, SttFinal, TurnEnd, TurnHold, VadPause, VadStart)
from voice_gateway.listen.pipeline import Listener
from voice_gateway.listen.transcribe import FakeSttFactory, ScriptedUtterance, SttUnavailable

# The tests below count the streams the Listener opens: without the shadow streams (the second identify stream of the
# first utterance, the start-language stream away from Swedish), which have their own tests at the end.
CFG = GatewayConfig(vad=VadConfig(model="energy"),
                    language=LanguageConfig(second_identify_candidates=(), shadow_start_language=False))
SHADOWS = GatewayConfig(vad=VadConfig(model="energy"))


class FakePlayback:
    """What the mixer exposes: playing flag, item start, level history from the line."""

    def __init__(self, adapter):
        self.adapter, self.clock = adapter, adapter.clock
        self.forced: bool | None = None

    @property
    def assistant_playing(self):
        if self.forced is not None:
            return self.forced
        return self.adapter._playing(self.clock.now_ms())

    @property
    def assistant_item_started_ms(self):
        return self.adapter._run_start if self.assistant_playing else None

    def playback_dbfs_at(self, at_ms):
        a = self.adapter
        return fr.rms_dbfs(bytes(a._track[max(0, at_ms) * 32:(max(0, at_ms) + 20) * 32]))


class Rig:
    def __init__(self, clock, script, *, cfg=CFG, caller="+46701234567", wire=fr.PCM16_16K, **adapter_kw):
        self.clock = clock
        self.adapter = LocalAdapter(clock, caller=caller, wire_format=wire, **adapter_kw)
        self.bus = EventBus()
        self.bus.history = []
        self.stt = FakeSttFactory(script, labeller=self.adapter.label_at)
        self.playback = FakePlayback(self.adapter)
        self.listener = Listener(cfg, self.adapter, self.stt, self.bus, clock, self.playback, caller_e164=caller,
                                 vad_model=None)
        self.task = None

    async def start(self):
        await self.adapter.accept()
        self.task = asyncio.create_task(self.listener.run())
        await self.clock.sleep_ms(40)

    async def stop(self):
        await self.adapter.caller.hang_up()
        await asyncio.wait_for(self.task, 10)

    def events(self, kind):
        return [e for e in self.bus.history if isinstance(e, kind)]

    async def take_turns(self):
        """Acts like the orchestrator: acknowledges every TurnEnd."""
        sub = self.bus.subscribe(TurnEnd)

        async def loop():
            async for _ in sub:
                self.listener.turn_taken()
        return asyncio.create_task(loop())


def speech(ms, hz=200):
    return tone(hz, ms, amp=0.25)


def test_a_swedish_utterance_becomes_a_turn_with_events_in_order():
    async def body(clock):
        r = Rig(clock, [ScriptedUtterance("u1", "jag vill boka bord till fredag", "sv")])
        await r.start()
        await r.adapter.caller.silence_ms(600)
        await r.adapter.caller.say(speech(2200), label="u1")
        await r.adapter.caller.silence_ms(2000)
        await r.stop()
        return r
    r = run(body)
    (end,) = r.events(TurnEnd)
    assert end.text == "jag vill boka bord till fredag" and end.language == "sv"
    assert end.language_decision.action == LanguageAction.KEEP and end.language_decision.reason in ("weak", "no_evidence")
    assert 560 <= end.speech_start_ms <= 800 and abs(end.eos_ms - 2800) <= 160
    assert 550 <= end.at_ms - end.eos_ms <= 1100                         # the turn detector's wait, not more
    kinds = [type(e).__name__ for e in r.bus.history]
    assert kinds.index("VadStart") < kinds.index("SttPartial") < kinds.index("VadPause") < kinds.index("TurnEnd")
    assert [m.identify for m in r.stt.opened][0] == ("sv", "en", "ar", "de", "pl")
    assert r.stt.opened[1].language == "sv"                               # fixed stream after the first utterance
    assert [e.action for e in r.events(LanguageDecided)] == [LanguageAction.KEEP]


def test_hesitation_for_ehm_fyra_personer_is_one_turn():
    async def body(clock):
        script = [ScriptedUtterance("a", "jag skulle vilja boka bord för ehm", "sv"),
                  ScriptedUtterance("b", "fyra personer", "sv")]
        r = Rig(clock, script)
        await r.start()
        await r.adapter.caller.silence_ms(500)
        await r.adapter.caller.say(speech(2200), label="a")
        await r.adapter.caller.silence_ms(1500)                           # the thinking pause
        await r.adapter.caller.say(speech(1000, 230), label="b")
        await r.adapter.caller.silence_ms(2500)
        await r.stop()
        return r
    r = run(body)
    (end,) = r.events(TurnEnd)
    assert end.text == "jag skulle vilja boka bord för ehm fyra personer"
    assert any(h.reason == "filler" for h in r.events(TurnHold))


def test_short_answer_after_a_yes_no_question_is_fast():
    async def body(clock):
        r = Rig(clock, [ScriptedUtterance("y", "ja", "sv")])
        await r.start()
        r.listener.set_context(AssistantContext(0, "yes_no", True, "sv"))
        await r.adapter.caller.silence_ms(400)
        await r.adapter.caller.say(speech(500), label="y")
        await r.adapter.caller.silence_ms(1500)
        await r.stop()
        return r
    (end,) = run(body).events(TurnEnd)
    assert end.text == "ja" and end.at_ms - end.eos_ms <= 450


def test_english_first_utterance_switches_without_waiting_for_a_re_transcription():
    async def body(clock):
        line = ScriptedUtterance("e", "hello I would like to book a table for four", "en", (("en", 0.96), ("sv", 0.02)))
        r = Rig(clock, [line])
        await r.start()
        await r.adapter.caller.silence_ms(500)
        await r.adapter.caller.say(speech(3200), label="e")
        await r.adapter.caller.silence_ms(3000)
        await r.stop()
        return r
    r = run(body)
    (end,) = r.events(TurnEnd)
    d = end.language_decision
    assert (d.action, d.current, d.reason, d.switches) == (LanguageAction.SWITCH, "en", "first_utterance", 1)
    assert end.language == "en" and end.text == "hello I would like to book a table for four"
    assert [m.language for m in r.stt.opened] == [None, "en"]             # identify, then English from here on
    # the identify stream already heard it in English: the TurnEnd is not held back for a replay
    assert end.at_ms - end.eos_ms <= 1000
    assert all(f.stream_id == 1 for f in r.events(SttFinal))
    assert any(e.source == "transcribe_lid" and e.code == "en" for e in r.events(LanguageEvidence))
    assert r.listener.tracker.current == "en" and r.listener.turn.language == "en"


def test_unsupported_language_gets_no_switch_but_a_decision_for_the_brain():
    async def body(clock):
        line = ScriptedUtterance("p", "سلام من می‌خواهم یک میز برای چهار نفر رزرو کنم", "fa", (("fa", 0.94),))
        r = Rig(clock, [line], caller="+98912000111")
        await r.start()
        await r.adapter.caller.silence_ms(400)
        await r.adapter.caller.say(speech(3500), label="p")
        await r.adapter.caller.silence_ms(2500)
        await r.stop()
        return r
    r = run(body)
    (end,) = r.events(TurnEnd)
    assert end.language == "sv" and end.language_decision.action == LanguageAction.UNSUPPORTED
    assert end.language_decision.candidate == "fa" and "fa" in r.stt.opened[0].identify
    assert r.stt.opened[-1].language == "sv"


def test_a_low_confidence_second_utterance_gets_a_lid_probe_and_switches():
    async def body(clock):
        script = [ScriptedUtterance("s", "hej jag vill boka bord", "sv", (("sv", 0.97),)),
                  ScriptedUtterance("e", "actually could we do tomorrow evening for six people please", "en",
                                    (("en", 0.93), ("sv", 0.03)))]
        r = Rig(clock, script)
        await r.start()
        taker = await r.take_turns()
        await r.adapter.caller.silence_ms(400)
        await r.adapter.caller.say(speech(2000), label="s")
        await r.adapter.caller.silence_ms(2500)
        await r.adapter.caller.say(speech(3500, 180), label="e")           # the fixed sv stream mishears: conf 0.3
        await r.adapter.caller.silence_ms(4000)
        await r.stop()
        taker.cancel()
        return r
    r = run(body)
    ends = r.events(TurnEnd)
    assert [e.language for e in ends] == ["sv", "en"]
    assert ends[1].language_decision.reason == "strong" and ends[1].text.startswith("actually could we")
    probes = [m for m in r.stt.opened if m.identify and m is not r.stt.opened[0]]
    assert len(probes) == 1                                                # one probe stream, nothing more
    # the probe heard the utterance in English: its text is used, no replayed re-transcription is waited for
    assert ends[1].at_ms - ends[1].eos_ms < 2000
    assert r.listener.transcriber.last_probe_text[0] == "en"


def test_no_probe_over_a_long_utterance_when_the_recogniser_answers_in_real_time():
    """Live Transcribe hears replayed audio no faster than real time: a 3.5 s probe cannot finish in 2.5 s."""
    async def body(clock):
        script = [ScriptedUtterance("s", "hej jag vill boka bord", "sv", (("sv", 0.97),)),
                  ScriptedUtterance("e", "actually could we do tomorrow evening for six people please", "en",
                                    (("en", 0.93), ("sv", 0.03)))]
        r = Rig(clock, script)
        r.stt.answers_in_real_time = True
        await r.start()
        taker = await r.take_turns()
        await r.adapter.caller.silence_ms(400)
        await r.adapter.caller.say(speech(2000), label="s")
        await r.adapter.caller.silence_ms(2500)
        await r.adapter.caller.say(speech(3500, 180), label="e")
        await r.adapter.caller.silence_ms(4000)
        await r.stop()
        taker.cancel()
        return r
    r = run(body)
    assert len(r.events(TurnEnd)) == 2
    assert [m for m in r.stt.opened if m.identify and m is not r.stt.opened[0]] == []


def test_explicit_language_request_switches_without_a_probe():
    async def body(clock):
        script = [ScriptedUtterance("s", "hej jag vill boka bord", "sv", (("sv", 0.97),)),
                  ScriptedUtterance("x", "kan vi prata engelska", "sv")]
        r = Rig(clock, script)
        await r.start()
        taker = await r.take_turns()
        await r.adapter.caller.silence_ms(400)
        await r.adapter.caller.say(speech(2000), label="s")
        await r.adapter.caller.silence_ms(2500)
        await r.adapter.caller.say(speech(1200, 180), label="x")
        await r.adapter.caller.silence_ms(4000)
        await r.stop()
        taker.cancel()
        return r
    r = run(body)
    ends = r.events(TurnEnd)
    assert ends[1].language == "en" and ends[1].language_decision.reason == "explicit"
    assert [m for m in r.stt.opened if m.identify and m is not r.stt.opened[0]] == []


# --- barge-in and the assistant's own audio ------------------------------------------------------

async def _assistant_speaks(r, ms):
    for f in pcm_frames(speech(ms, 150), 0):
        await r.adapter.play(f)


def test_caller_talking_over_the_assistant_barges_in_within_a_third_of_a_second_of_voice():
    async def body(clock):
        r = Rig(clock, [ScriptedUtterance("c", "nej jag vill ha tisdag istället", "sv")])
        await r.start()
        await r.adapter.caller.silence_ms(300)
        await _assistant_speaks(r, 6000)
        await clock.sleep_ms(1000)
        onset = clock.now_ms()
        await r.adapter.caller.say(speech(1800, 210), label="c")
        await r.adapter.caller.silence_ms(2500)
        await r.stop()
        return r, onset
    r, onset = run(body)
    (b,) = r.events(BargeIn)
    assert onset - 40 <= b.speech_start_ms <= onset + 80
    assert b.at_ms - b.speech_start_ms <= 450 and b.reason in ("duration", "words", "stop_word")
    assert r.events(TurnEnd)[0].text == "nej jag vill ha tisdag istället"


def test_a_short_mm_over_the_assistant_is_neither_a_barge_in_nor_a_turn():
    async def body(clock):
        r = Rig(clock, [ScriptedUtterance("m", "mm", "sv")])
        await r.start()
        await r.adapter.caller.silence_ms(300)
        await _assistant_speaks(r, 9000)
        await clock.sleep_ms(1000)
        await r.adapter.caller.say(speech(260, 210), label="m")
        await clock.sleep_ms(3000)                                         # the assistant is still speaking
        await r.stop()
        return r
    r = run(body)
    assert r.events(BargeIn) == [] and r.events(TurnEnd) == []


def test_echo_of_the_assistant_does_not_barge_in():
    pytest.importorskip("numpy")                                          # the echo line simulation mixes with numpy
    async def body(clock):
        r = Rig(clock, [], echo_db=-18, echo_delay_ms=100)
        await r.start()
        await r.adapter.caller.silence_ms(300)
        for f in pcm_frames(tone(150, 5000, amp=0.5), 0):                  # loud assistant
            await r.adapter.play(f)
        await clock.sleep_ms(5500)
        await r.stop()
        return r
    r = run(body)
    assert r.events(VadStart) and r.events(BargeIn) == [] and r.events(TurnEnd) == []   # the echo was heard, not obeyed


def test_a_real_turn_that_began_over_the_assistant_still_flushes_it():
    async def body(clock):
        r = Rig(clock, [ScriptedUtterance("c", "tisdag", "sv")])
        await r.start()
        r.playback.forced = True                                           # the mixer says it plays; the VAD sees
        await r.adapter.caller.silence_ms(300)                             # speech of only 300 ms, so no barge-in
        await r.adapter.caller.say(speech(300, 210), label="c")
        await clock.sleep_ms(300)
        r.playback.forced = False                                          # the assistant stopped meanwhile
        await clock.sleep_ms(2500)
        await r.stop()
        return r
    r = run(body)
    assert [e.text for e in r.events(TurnEnd)] == ["tisdag"]


# --- the orchestrator's levers -------------------------------------------------------------------

def test_follow_language_reopens_the_stream_and_counts_the_switch():
    async def body(clock):
        r = Rig(clock, [])
        await r.start()
        await r.listener.follow_language("de", clock.now_ms())
        await r.listener.follow_language("de", clock.now_ms())
        await clock.sleep_ms(100)
        await r.stop()
        return r
    r = run(body)
    decisions = r.events(LanguageDecided)
    assert len(decisions) == 1 and decisions[0].reason == "model_tag" and decisions[0].current == "de"
    assert [m.language for m in r.stt.opened] == [None, "de"] and r.listener.turn.language == "de"


def test_turn_taken_resets_so_the_next_speech_is_a_new_turn():
    async def body(clock):
        script = [ScriptedUtterance("a", "bord för två imorgon", "sv"), ScriptedUtterance("b", "tack så mycket", "sv")]
        r = Rig(clock, script)
        await r.start()
        taker = await r.take_turns()
        await r.adapter.caller.silence_ms(300)
        await r.adapter.caller.say(speech(1500), label="a")
        await r.adapter.caller.silence_ms(2500)
        await r.adapter.caller.say(speech(1500, 190), label="b")
        await r.adapter.caller.silence_ms(2500)
        await r.stop()
        taker.cancel()
        return r
    r = run(body)
    assert [e.text for e in r.events(TurnEnd)] == ["bord för två imorgon", "tack så mycket"]


def test_stt_failure_surfaces_from_run():
    class Down(FakeSttFactory):
        async def open(self, mode, *, stream_id, audio_origin_ms):
            raise ConnectionError("down")

    async def body(clock):
        r = Rig(clock, [])
        r.listener.stt = r.listener.transcriber.factory = Down()
        await r.adapter.accept()
        with pytest.raises(SttUnavailable):
            await asyncio.wait_for(r.listener.run(), 5)
    run(body)


def test_the_listener_keeps_up_with_real_time_audio_frames():
    """3 s of speech is 150 frames; the listener's per-frame work must stay small."""
    import time

    async def body(clock):
        t0 = time.perf_counter()
        r = Rig(clock, [ScriptedUtterance("u", "hej", "sv")])
        await r.start()
        await r.adapter.caller.say(speech(3000), label="u")
        await r.adapter.caller.silence_ms(2000)
        await r.stop()
        return r, time.perf_counter() - t0
    r, elapsed = run(body)
    frames = 5000 // 20
    assert elapsed / frames < 0.003                                        # well under 3 ms of CPU per 20 ms frame


def test_polly_speech_over_a_simulated_phone_line_with_silero_vad():
    pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")
    from test_vg_listen_common import FIXTURES, silero_model_path
    from voice_gateway.audio.wav import read_wav
    path = silero_model_path()
    if not path:
        pytest.skip("silero_vad.onnx not available (set VG_SILERO_MODEL)")
    cfg = GatewayConfig(vad=VadConfig(model="silero", silero_model_path=path))
    clip = read_wav(FIXTURES / "sv_booking.wav", 16000)

    async def body(clock):
        line = ScriptedUtterance("c", "hej jag skulle vilja boka ett bord för fyra personer på fredag klockan sju", "sv")
        r = Rig(clock, [line], cfg=cfg, wire=fr.TELEPHONY_MULAW_8K, noise_dbfs=-62)
        await r.start()
        await r.adapter.caller.silence_ms(800)
        await r.adapter.caller.say(clip, label="c")
        await r.adapter.caller.silence_ms(2500)
        await r.stop()
        return r
    r = run(body)
    (end,) = r.events(TurnEnd)
    clip_end = 800 + len(clip) // 32
    assert end.text.startswith("hej jag skulle vilja boka") and end.language == "sv"
    assert 800 - 100 <= end.speech_start_ms <= 800 + 400 and abs(end.eos_ms - clip_end) < 450
    assert 500 <= end.at_ms - end.eos_ms <= 1200
    assert len(r.events(VadStart)) == 1


# --- shadow streams: Turkish on a +46 line, back to Swedish --------------------------------------

def test_a_turkish_first_utterance_is_found_by_the_second_identify_stream():
    """A +46 caller's first stream offers sv, en, ar, de, pl (Transcribe takes five): the second one sv, en, tr, fi,
    es. Live 2026-11-20 Turkish was heard as English nonsense (lang-J)."""
    async def body(clock):
        line = ScriptedUtterance("t", "merhaba yarın akşam saat sekizde iki kişilik bir masa istiyorum", "tr",
                                 (("tr", 0.95), ("en", 0.03)))
        r = Rig(clock, [line], cfg=SHADOWS)
        await r.start()
        await r.adapter.caller.silence_ms(500)
        await r.adapter.caller.say(speech(3200), label="t")
        await r.adapter.caller.silence_ms(3000)
        await r.stop()
        return r
    r = run(body)
    (end,) = r.events(TurnEnd)
    d = end.language_decision
    assert (d.action, d.current, d.reason) == (LanguageAction.SWITCH, "tr", "first_utterance")
    assert end.text.startswith("merhaba yarın") and end.language == "tr"
    first, second = r.stt.opened[0].identify, r.stt.opened[1].identify
    assert "tr" not in first and second == ("sv", "en", "tr", "fi", "es")
    # the live stream goes Turkish, the identify shadow closes and a Swedish one opens (away from Swedish now)
    assert [m.language for m in r.stt.opened[2:]] == ["tr", "sv"]
    assert end.at_ms - end.eos_ms <= 1200                     # no re-transcription waited for


def test_a_swedish_first_utterance_ignores_the_second_identify_stream():
    async def body(clock):
        line = ScriptedUtterance("s", "hej jag vill boka ett bord för fyra", "sv", (("sv", 0.96),))
        r = Rig(clock, [line], cfg=SHADOWS)
        await r.start()
        await r.adapter.caller.silence_ms(500)
        await r.adapter.caller.say(speech(2500), label="s")
        await r.adapter.caller.silence_ms(3000)
        await r.stop()
        return r
    r = run(body)
    (end,) = r.events(TurnEnd)
    assert end.language == "sv" and end.language_decision.action == LanguageAction.KEEP
    assert [m.language for m in r.stt.opened[2:]] == ["sv"]           # the identify shadow closed, no other one


def test_away_from_swedish_a_swedish_stream_runs_beside_and_brings_the_caller_back():
    """lang-E live: Swedish on the en-GB stream came back as English nonsense at 0.66 and the explicit "på svenska"
    was lost. The start-language stream hears the words, the explicit request switches back at once."""
    async def body(clock):
        script = [ScriptedUtterance("e", "hello do you have a table for four on friday evening", "en",
                                    (("en", 0.95), ("sv", 0.03))),
                  ScriptedUtterance("s", "förlåt vi kan ta det på svenska istället klockan sju passar bra", "sv",
                                    (("sv", 0.95),))]
        r = Rig(clock, script, cfg=SHADOWS)
        await r.start()
        taker = await r.take_turns()
        await r.adapter.caller.silence_ms(500)
        await r.adapter.caller.say(speech(3000), label="e")
        await r.adapter.caller.silence_ms(2500)
        assert r.listener.transcriber.shadow_mode.language == "sv"       # away from Swedish: the shadow runs
        await r.adapter.caller.say(speech(3500, 180), label="s")         # the en stream mishears it: conf 0.3
        await r.adapter.caller.silence_ms(3000)
        await r.stop()
        taker.cancel()
        return r
    r = run(body)
    ends = r.events(TurnEnd)
    assert [e.language for e in ends] == ["en", "sv"]
    assert ends[1].language_decision.reason == "explicit" and ends[1].text.startswith("förlåt vi kan ta det")
    assert [m.language for m in r.stt.opened[2:]] == ["en", "sv", "sv"]   # en live, sv shadow, sv live (no new shadow)
    probes = [m for m in r.stt.opened[2:] if m.identify]
    assert probes == []                                                  # the shadow replaced the probe


def test_the_second_identify_streams_partial_names_the_language_before_its_final():
    """Live lang-J: the Turkish final came 0.5 to 1 s after the turn end, its partials said tr from the first word."""
    async def body(clock):
        line = ScriptedUtterance("t", "merhaba yarın akşam saat sekizde iki kişilik bir masa istiyorum", "tr",
                                 (("tr", 0.95),))
        r = Rig(clock, [line], cfg=SHADOWS)
        r.stt.final_delay_ms = 3000                       # no final from either stream before the turn ends
        await r.start()
        await r.adapter.caller.silence_ms(500)
        await r.adapter.caller.say(speech(3200), label="t")
        await r.adapter.caller.silence_ms(5000)
        await r.stop()
        return r
    r = run(body)
    end = r.events(TurnEnd)[0]
    assert end.language_decision.action == LanguageAction.SWITCH and end.language == "tr"
    assert end.text.startswith("merhaba")

"""The call state machine (orchestrator.py) on a FakeClock with a scripted
brain, a fake speaker and a fake listener: every transition of the
docstring, barge-in (flush, not heard, context kept), the caller who keeps
talking, language switches (the listener's and the brain's), fillers only
for real tools, transfer, still there, hang-up and errors. Pure stdlib +
the application's deps (root CI)."""

import asyncio
from collections import deque

import pytest

from voice_gateway.audio.adapter import CallInfo, TransferOutcome
from voice_gateway.audio.frames import PCM16_16K
from voice_gateway.brain.scripted import Reply, ScriptedBrain
from voice_gateway.clock import FakeClock
from voice_gateway.config import GatewayConfig
from voice_gateway.events import (AgentSentence, AssistantContext, BargeIn, CallState, LanguageAction,
                                  LanguageDecided, PlaybackFinished, PlaybackFlushed, PlaybackStarted, ProviderAction,
                                  StateChanged, ToolStarted, TurnEnd, VadStart)
from voice_gateway.orchestrator import CallOrchestrator, Components

S = CallState


# --- fakes ----------------------------------------------------------------------------------------

class FakeAdapter:
    def __init__(self, clock, transfer_answers=True):
        self.clock, self.transfer_answers = clock, transfer_answers
        self.hangups, self.transfers = [], []
        self._closed = False
        self.gone = asyncio.Event()

    async def accept(self):
        return CallInfo("ref-1", "+46100000001", "+46701112233", PCM16_16K)

    def caller_frames(self):
        raise NotImplementedError

    async def play(self, frame):
        pass

    async def clear(self):
        return 0

    async def hangup(self, reason):
        self.hangups.append(reason)
        self.close()

    async def transfer(self, to_e164, timeout_s):
        self.transfers.append(to_e164)
        await self.clock.sleep_ms(3000)
        return TransferOutcome(self.transfer_answers, "answered" if self.transfer_answers else "no_answer")

    def close(self):
        self._closed = True
        self.gone.set()

    @property
    def closed(self):
        return self._closed


class FakeSpeaker:
    """Plays one item at a time for len(text) * 20 ms on the clock; flush cuts it."""

    def __init__(self, bus, clock, language="sv"):
        self.bus, self.clock, self.language = bus, clock, language
        self.queue, self.said, self.flushes, self.languages, self.played = deque(), [], [], [], []
        self.current = None
        self._n = 0
        self._wake = asyncio.Event()
        self._play = None

    def say(self, text, language, *, kind="assistant", turn=0, index=0):
        self._n += 1
        item = f"i{self._n}"
        self.queue.append((item, text, language, kind))
        self.said.append((kind, language, text))
        self._wake.set()
        return item

    async def run(self):
        while True:
            if not self.queue:
                self._wake.clear()
                await self._wake.wait()
                continue
            item, text, language, kind = self.queue.popleft()
            ms = max(200, len(text) * 20)
            self.current = (item, kind, self.clock.now_ms())
            self.bus.publish(PlaybackStarted(self.clock.now_ms(), item, kind, language, ms))
            self._play = asyncio.ensure_future(self.clock.sleep_ms(ms))
            await asyncio.wait({self._play})
            if not self._play.cancelled():
                self.current = None
                self.played.append(text)
                self.bus.publish(PlaybackFinished(self.clock.now_ms(), item, kind, ms, False))

    async def flush(self, barge_in_at_ms):
        now = self.clock.now_ms()
        unplayed = tuple(x[0] for x in self.queue)
        self.queue.clear()
        if self.current:
            item, kind, started = self.current
            self.current = None
            self._play.cancel()
            self.bus.publish(PlaybackFinished(now, item, kind, now - started, True))
        flushed = PlaybackFlushed(now + 40, 60, now + 40 - barge_in_at_ms, unplayed)
        self.flushes.append(flushed)
        self.bus.publish(flushed)
        return flushed

    async def drain(self, timeout_ms=30000):
        end = self.clock.now_ms() + timeout_ms
        while (self.queue or self.current) and self.clock.now_ms() < end:
            await self.clock.sleep_ms(20)

    def set_language(self, language):
        self.languages.append(language)

    @property
    def busy(self):
        return bool(self.queue or self.current)

    def texts(self, kind=None):
        return [t for k, _, t in self.said if kind is None or k == kind]


class FakeListener:
    def __init__(self, adapter):
        self.adapter = adapter
        self.contexts, self.follows, self.taken = [], [], 0

    async def run(self):
        await self.adapter.gone.wait()

    def set_context(self, ctx):
        self.contexts.append(ctx)

    async def follow_language(self, code, at_ms):
        self.follows.append(code)

    def turn_taken(self):
        self.taken += 1


class Caller:
    """Drives the call from the caller's side by publishing listen events."""

    def __init__(self, orch, clock, adapter):
        self.orch, self.clock, self.adapter, self.bus = orch, clock, adapter, orch.bus
        self.states = []
        self.bus.listen(lambda e: self.states.append(e.new), StateChanged)

    async def wait_state(self, state, nth=1, timeout_ms=60000):
        end = self.clock.now_ms() + timeout_ms
        while self.states.count(state) < nth:
            assert self.clock.now_ms() < end, f"never reached {state} #{nth}: {self.states}"
            await self.clock.sleep_ms(10)

    async def wait_playing(self, kind="assistant", timeout_ms=60000):
        end = self.clock.now_ms() + timeout_ms
        while not (self.orch.speaker.current and self.orch.speaker.current[1] == kind):
            assert self.clock.now_ms() < end, "no playback"
            await self.clock.sleep_ms(10)

    async def utter(self, text, *, speech_ms=1200, silence_ms=550, language="sv", decision=None):
        start = self.clock.now_ms()
        self.bus.publish(VadStart(start + 100, start, 0.9))
        await self.clock.sleep_ms(speech_ms)
        eos = self.clock.now_ms()
        await self.clock.sleep_ms(silence_ms)
        self.bus.publish(TurnEnd(self.clock.now_ms(), text, language, start, eos, "silence",
                                 language_decision=decision))

    async def barge_in(self):
        now = self.clock.now_ms()
        self.bus.publish(VadStart(now, now - 100, 0.95, during_playback=True))
        await self.clock.sleep_ms(250)
        self.bus.publish(BargeIn(self.clock.now_ms(), now - 100, "duration"))
        await self.clock.sleep_ms(10)

    async def hang_up(self):
        self.adapter.close()


def run_call(script, *, replies=None, cfg=None, transfer_answers=True, **brain_kwargs):
    """Runs one call; returns (summary, brain, speaker, listener, adapter, caller)."""
    cfg = cfg or GatewayConfig()
    made = {}

    async def main():
        clock = FakeClock()
        driver = asyncio.create_task(clock.drive())
        adapter = FakeAdapter(clock, transfer_answers)

        def brain_factory(c, bus, clk):
            made["brain"] = ScriptedBrain(c, bus, clk, script=list(replies or [Reply()]), **brain_kwargs)
            return made["brain"]

        def speaker_factory(c, a, tts, clips, bus, clk, language, seed):
            made["speaker"] = FakeSpeaker(bus, clk, language)
            return made["speaker"]

        def listener_factory(c, a, stt, bus, clk, playback, caller, names):
            made["listener"] = FakeListener(a)
            return made["listener"]

        orch = CallOrchestrator(cfg, adapter, Components(None, None, None, clock, brain_factory, record_events=True,
                                                         speaker_factory=speaker_factory,
                                                         listener_factory=listener_factory, emit_metrics=False))
        caller = Caller(orch, clock, adapter)
        made["caller"], made["adapter"] = caller, adapter
        call = asyncio.create_task(orch.run())
        acting = asyncio.create_task(script(caller))
        summary = await asyncio.wait_for(call, timeout=20)
        if acting.done() and acting.exception():
            raise acting.exception()
        acting.cancel()
        driver.cancel()
        return summary

    summary = asyncio.run(main())
    return summary, made.get("brain"), made.get("speaker"), made.get("listener"), made["adapter"], made["caller"]


def kinds(summary, cls):
    return [e for e in summary.events if isinstance(e, cls)]


GREETING_SENTENCES = ["Hej och välkommen till Testkrogen.", "Du pratar med vår digitala assistent.",
                      "Vad kan jag hjälpa dig med?"]


# --- greeting -------------------------------------------------------------------------------------

def test_greeting_then_listening_then_a_reply_then_listening_with_the_context():
    async def script(c):
        await c.wait_state(S.LISTENING)
        await c.utter("Jag vill boka ett bord")
        await c.wait_state(S.LISTENING, 2)
        await c.hang_up()

    reply = Reply("Absolut, för hur många personer? Och vilken dag?", asked_question="number")
    summary, brain, speaker, listener, adapter, _ = run_call(script, replies=[reply])
    assert speaker.said[:3] == [("fixed", "sv", s) for s in GREETING_SENTENCES]      # Booqy wording, one by one
    assert ("greeting_played", None) in brain.calls
    states = [s for _, s in summary.states]
    assert states == [S.GREETING, S.LISTENING, S.THINKING, S.SPEAKING, S.LISTENING, S.ENDING, S.ENDED]
    assert speaker.texts("assistant") == ["Absolut, för hur många personer?", "Och vilken dag?"]
    (ctx,) = kinds(summary, AssistantContext)
    assert ctx.asked_question == "number" and listener.contexts == [ctx]
    assert summary.end_reason == "hangup" and brain.calls[-1] == ("finish", "hangup") and adapter.hangups == []
    turn = summary.metrics.turns[0]
    assert turn.eos_to_first_audio == turn.first_audio_ms - turn.eos_ms and 600 <= turn.turn_end_to_first_audio < 700


def test_not_a_booqy_number_hangs_up_at_once_and_says_nothing():
    async def script(c):
        pass

    summary, brain, speaker, _, adapter, _ = run_call(script, booqy_number=False)
    assert summary.end_reason == "not_a_booqy_number" and adapter.hangups == ["not_a_booqy_number"]
    assert speaker is None and [s for _, s in summary.states] == [S.ENDED]


def test_a_refused_call_hears_the_refusal_then_the_line_ends():
    async def script(c):
        pass

    summary, brain, speaker, _, adapter, _ = run_call(script, refused=True)
    assert speaker.said[0][0] == "fixed" and speaker.said[0][2].startswith("Tyvärr kan vi inte ta emot bokningar")
    assert summary.end_reason == "refused" and adapter.hangups == ["refused"]
    assert ("finish", "refused") in brain.calls and not speaker.busy


def test_barge_in_before_the_disclosure_repeats_the_short_disclosure_before_the_next_reply():
    async def script(c):
        await c.wait_playing("fixed")
        await c.clock.sleep_ms(300)                      # inside "Hej och välkommen till Testkrogen."
        await c.barge_in()
        await c.utter("Hej, jag vill boka")
        await c.wait_state(S.LISTENING, 2)
        await c.hang_up()

    summary, brain, speaker, *_ = run_call(script)
    assert ("greeting_interrupted",) in brain.calls
    assert summary.end_reason == "hangup"
    after = speaker.said[3:]                             # the greeting was cut in its first sentence
    assert after[0] == ("fixed", "sv", "Jag är restaurangens digitala assistent.")   # art. 50 before the reply
    assert after[1][0] == "assistant"
    assert ("greeting_played", "Jag är restaurangens digitala assistent.") in brain.calls


def test_barge_in_after_the_disclosure_counts_the_greeting_as_heard():
    async def script(c):
        await c.wait_playing("fixed")
        while c.orch.speaker.current is None or c.orch.speaker.current[0] != "i3":
            await c.clock.sleep_ms(10)                   # "Vad kan jag hjälpa dig med?" is playing
        await c.barge_in()
        await c.hang_up()

    summary, brain, speaker, *_ = run_call(script)
    assert ("greeting_played", None) in brain.calls and ("greeting_interrupted",) not in brain.calls


# --- barge-in, the caller who keeps talking -------------------------------------------------------

def test_barge_in_flushes_marks_the_unheard_rest_and_keeps_the_context():
    reply = Reply("Jag har ett bord klockan sju i morgon. Det gäller fyra personer. Stämmer det?",
                  awaiting_confirmation=True, asked_question="yes_no", gap_ms=0)

    async def script(c):
        await c.wait_state(S.LISTENING)
        await c.utter("Bord för fyra i morgon klockan sju")
        await c.wait_state(S.SPEAKING)
        while c.orch.speaker.current is None or c.orch.speaker.current[0] == "i4":
            await c.clock.sleep_ms(10)                   # the first reply sentence has played
        await c.barge_in()
        await c.utter("Nej vänta, klockan åtta")
        await c.wait_state(S.LISTENING, 3)
        await c.hang_up()

    summary, brain, speaker, listener, *_ = run_call(script, replies=[reply, Reply("Klockan åtta, stämmer det?")])
    assert len(speaker.flushes) == 1
    assert ("mark_not_heard", ["Jag har ett bord klockan sju i morgon."]) in brain.calls   # heard: only the first
    assert "Stämmer det?" not in speaker.played           # the read-back's yes question never played
    turns = [c for c in brain.calls if c[0] == "turn"]
    assert [t[2] for t in turns] == ["Bord för fyra i morgon klockan sju", "Nej vänta, klockan åtta"]
    assert brain.calls.count(("start", "ref-1")) == 1     # the same brain: the call's context is kept
    assert summary.metrics.barge_ins == 1 and summary.metrics.turns[0].barge_in_stop_ms == 40
    assert summary.metrics.turns[0].barge_in_detect_ms == 350
    assert listener.taken >= 3
    contexts = kinds(summary, AssistantContext)
    assert len(contexts) == 1 and contexts[0].asked_question is None    # the interrupted reply set no context


def test_the_caller_who_keeps_talking_before_any_audio_is_not_answered_twice():
    async def script(c):
        await c.wait_state(S.LISTENING)
        await c.utter("Jag vill boka för", silence_ms=550)
        await c.clock.sleep_ms(200)                      # the agent thinks (first sentence at 600 ms)
        await c.utter("fyra personer i morgon")
        await c.wait_state(S.LISTENING, 3)
        await c.hang_up()

    summary, brain, speaker, *_ = run_call(script, replies=[Reply("Svar ett."), Reply("Svar två för fyra.")])
    assert "Svar ett." not in speaker.texts()             # dropped, never said
    assert speaker.texts("assistant") == ["Svar två för fyra."]
    assert ("mark_not_heard", []) in brain.calls
    assert [c[2] for c in brain.calls if c[0] == "turn"] == ["Jag vill boka för", "fyra personer i morgon"]


def test_a_yes_said_over_the_read_back_does_not_count_as_the_answer():
    """The caller starts "ja" while the read-back still plays, no barge-in (a backchannel word), and the turn ends
    after the reply drained: the read-back counts as not heard, so the guard needs a new read-back and yes."""
    reply = Reply("Ett bord för fyra i morgon klockan sju. Stämmer det?", awaiting_confirmation=True,
                  asked_question="yes_no", gap_ms=0)

    async def script(c):
        await c.wait_state(S.LISTENING)
        await c.utter("Bord för fyra i morgon klockan sju")
        await c.wait_state(S.SPEAKING)
        start = c.clock.now_ms()                         # the read-back's first sentence is playing
        c.bus.publish(VadStart(start + 100, start, 0.9, during_playback=True))
        await c.wait_state(S.LISTENING, 2)               # the reply drained before the caller's turn ended
        await c.clock.sleep_ms(300)
        c.bus.publish(TurnEnd(c.clock.now_ms(), "ja", "sv", start, c.clock.now_ms() - 200, "silence"))
        await c.wait_state(S.LISTENING, 3)
        await c.hang_up()

    summary, brain, *_ = run_call(script, replies=[reply, Reply("Då läser jag upp det igen.")])
    marks = [c for c in brain.calls if c[0] == "mark_not_heard"]
    assert marks and marks[0][1] == ["Ett bord för fyra i morgon klockan sju.", "Stämmer det?"]
    assert brain.calls.index(marks[0]) < max(i for i, c in enumerate(brain.calls) if c[0] == "turn")


def test_a_yes_after_the_read_back_ended_is_the_answer():
    reply = Reply("Ett bord för fyra i morgon klockan sju. Stämmer det?", awaiting_confirmation=True,
                  asked_question="yes_no", gap_ms=0)

    async def script(c):
        await c.wait_state(S.LISTENING)
        await c.utter("Bord för fyra i morgon klockan sju")
        await c.wait_state(S.LISTENING, 2)
        await c.utter("ja", speech_ms=300)
        await c.wait_state(S.LISTENING, 3)
        await c.hang_up()

    summary, brain, *_ = run_call(script, replies=[reply, Reply("Klart.")])
    assert not [c for c in brain.calls if c[0] == "mark_not_heard"]


def test_after_the_grace_window_the_reply_plays():
    cfg = GatewayConfig.from_json('{"call": {"continuation_grace_ms": 100}}')

    async def script(c):
        await c.wait_state(S.LISTENING)
        await c.utter("Jag vill boka")
        await c.clock.sleep_ms(300)
        c.bus.publish(VadStart(c.clock.now_ms(), c.clock.now_ms(), 0.9))   # a cough, past the window
        await c.wait_state(S.SPEAKING)
        await c.hang_up()

    summary, brain, speaker, *_ = run_call(script, cfg=cfg)
    assert speaker.texts("assistant") and ("mark_not_heard", []) not in brain.calls


# --- tools and fillers ------------------------------------------------------------------------------

def test_a_tool_turn_announces_the_tool_and_end_call_does_not():
    async def script(c):
        await c.wait_state(S.LISTENING)
        await c.utter("Har ni bord i morgon?")
        await c.wait_state(S.LISTENING, 2)
        await c.utter("Tack, hej då")
        await c.wait_state(S.ENDED)

    replies = [Reply("Ja, klockan sju är ledigt.", tool="get_availability"),
               Reply("Tack för samtalet.", tool="end_call", actions=(ProviderAction("end_call", {}),))]
    summary, brain, speaker, _, adapter, _ = run_call(script, replies=replies)
    assert [e.name for e in kinds(summary, ToolStarted)] == ["get_availability"]   # a filler may cover only this
    assert summary.end_reason == "agent_end" and adapter.hangups == ["agent_end"]
    assert speaker.texts()[-1] == "Tack för samtalet." and ("finish", "agent_end") in brain.calls
    assert summary.metrics.turns[0].tools == ("get_availability",) and summary.metrics.turns[0].rounds == 2


# --- language -------------------------------------------------------------------------------------

def test_a_switch_the_listener_decided_says_the_disclosure_first_and_keeps_the_brain():
    decision = LanguageDecided(0, LanguageAction.SWITCH, "en", "en", "strong", 1)

    async def script(c):
        await c.wait_state(S.LISTENING)
        await c.utter("Hi, I would like to book a table for four tomorrow", language="en", decision=decision)
        await c.wait_state(S.LISTENING, 2)
        await c.hang_up()

    summary, brain, speaker, listener, *_ = run_call(script, replies=[Reply("Of course. What time?", "en",
                                                                            call_language="en")])
    assert ("set_language", "en", "strong") in brain.calls
    assert speaker.said[3] == ("fixed", "en", "I am the restaurant's digital assistant.")
    assert speaker.said[4] == ("assistant", "en", "Of course.")
    assert speaker.languages == ["en"] and listener.follows == []       # the listener already follows itself
    assert [c for c in brain.calls if c[0] == "turn"][0][3] == "en"


def test_a_switch_the_brain_made_on_the_model_tag_moves_listener_and_speaker():
    async def script(c):
        await c.wait_state(S.LISTENING)
        await c.utter("Hallo, ich möchte einen Tisch reservieren", language="sv")
        await c.wait_state(S.LISTENING, 2)
        await c.hang_up()

    summary, brain, speaker, listener, *_ = run_call(
        script, replies=[Reply("Ich bin der digitale Assistent des Restaurants. Gerne.", "de", call_language="de")])
    assert listener.follows == ["de"] and speaker.languages == ["de"]
    assert kinds(summary, AssistantContext)[0].language == "de"


# --- transfer -------------------------------------------------------------------------------------

TRANSFER = ProviderAction("transfer_to_number", {"transfer_number": "+46812345678",
                                                 "client_message": "Jag kopplar dig till restaurangen, ett ögonblick."})


def test_an_answered_transfer_ends_the_call_without_our_hangup():
    async def script(c):
        await c.wait_state(S.LISTENING)
        await c.utter("Kan jag prata med en människa")
        await c.wait_state(S.ENDED)

    summary, brain, speaker, _, adapter, _ = run_call(script, replies=[Reply("Absolut.", actions=(TRANSFER,))])
    assert S.TRANSFERRING in [s for _, s in summary.states]
    assert speaker.texts("fixed")[-1] == "Jag kopplar dig till restaurangen, ett ögonblick."
    assert adapter.transfers == ["+46812345678"] and adapter.hangups == []
    assert ("transfer_result", True) in brain.calls and ("finish", "transferred") in brain.calls


def test_an_unanswered_transfer_says_so_and_listens_again():
    async def script(c):
        await c.wait_state(S.LISTENING)
        await c.utter("Kan jag prata med en människa")
        await c.wait_state(S.LISTENING, 2)
        await c.hang_up()

    summary, brain, speaker, _, adapter, _ = run_call(script, replies=[Reply("Absolut.", actions=(TRANSFER,))],
                                                      transfer_answers=False)
    assert ("transfer_result", False) in brain.calls
    assert speaker.texts("fixed")[-1] == "Ingen på restaurangen kunde svara just nu. Vill du att de ringer upp dig?"
    assert summary.end_reason == "hangup"


# --- still there, hang-up, errors -------------------------------------------------------------------

def test_still_there_twice_then_the_call_ends():
    async def script(c):
        await c.wait_state(S.ENDED, timeout_ms=120000)

    summary, brain, speaker, _, adapter, _ = run_call(script)
    still = [t for t in speaker.texts("fixed") if t.startswith("Är du kvar")]
    assert len(still) == 2 and summary.end_reason == "still_there" and adapter.hangups == ["still_there"]
    assert ("finish", "still_there") in brain.calls


def test_speech_resets_the_still_there_timer():
    async def script(c):
        await c.wait_state(S.LISTENING)
        await c.clock.sleep_ms(6000)
        await c.utter("Hej")
        await c.wait_state(S.LISTENING, 2)
        await c.clock.sleep_ms(6000)
        await c.hang_up()

    summary, brain, speaker, *_ = run_call(script)
    assert not [t for t in speaker.texts() if t.startswith("Är du kvar")]


def test_a_stuck_brain_turn_says_the_error_sentence_and_ends(monkeypatch):
    """A turn whose worker never returns (a hung DynamoDB or Lambda call) must not leave the caller in silence
    with the line and the Transcribe stream open."""
    from voice_gateway import orchestrator
    monkeypatch.setattr(orchestrator, "TURN_MAX_MS", 3000)
    monkeypatch.setattr(orchestrator, "TURN_SAVE_WAIT_MS", 100)

    async def script(c):
        await c.wait_state(S.LISTENING)
        await c.utter("Boka åt mig")
        await c.wait_state(S.ENDED, timeout_ms=3600000)

    summary, brain, speaker, _, adapter, _ = run_call(script, replies=[Reply("Visst.", first_ms=600000)])
    assert summary.end_reason == "error" and adapter.hangups == ["error"]
    assert speaker.texts("fixed")[-1] == "Ursäkta, något gick fel hos oss. Vill du att restaurangen ringer upp dig?"
    assert not speaker.texts("assistant")                # the stuck reply is never said
    assert ("finish", "error") in brain.calls


def test_noise_that_never_ends_a_turn_cannot_keep_the_call_open(monkeypatch):
    from voice_gateway import orchestrator
    monkeypatch.setattr(orchestrator, "CALL_MAX_MS", 20000)

    async def script(c):
        await c.wait_state(S.LISTENING)
        while True:                                      # VAD keeps firing, no TurnEnd ever comes
            now = c.clock.now_ms()
            c.bus.publish(VadStart(now, now, 0.9))
            await c.clock.sleep_ms(1000)

    summary, brain, speaker, _, adapter, _ = run_call(script)
    assert summary.end_reason == "max_length" and adapter.hangups == ["max_length"]
    assert ("finish", "max_length") in brain.calls


def test_a_caller_hangup_mid_turn_lets_the_turn_save_before_finish():
    async def script(c):
        await c.wait_state(S.LISTENING)
        await c.utter("Boka åt mig")
        await c.wait_state(S.THINKING)
        await c.hang_up()

    summary, brain, *_ = run_call(script, replies=[Reply("Visst.", tool="hold_table", tool_ms=2000)])
    names = [c[0] for c in brain.calls]
    assert names.index("turn") < names.index("finish") and brain.calls[-1] == ("finish", "hangup")
    assert summary.end_reason == "hangup"


def test_a_failing_brain_turn_says_the_error_sentence_and_ends():
    class Broken(ScriptedBrain):
        async def turn(self, text, *, language, asr_locale=None):
            raise RuntimeError("boom")
            yield  # pragma: no cover

    async def script(c):
        await c.wait_state(S.LISTENING)
        await c.utter("Hej")
        await c.wait_state(S.ENDED)

    made = {}

    async def main():
        clock = FakeClock()
        driver = asyncio.create_task(clock.drive())
        adapter = FakeAdapter(clock)
        comps = Components(None, None, None, clock, lambda c, b, k: made.setdefault("brain", Broken(c, b, k)),
                           record_events=True, emit_metrics=False,
                           speaker_factory=lambda c, a, t, cl, b, k, lang, s: made.setdefault("sp", FakeSpeaker(b, k)),
                           listener_factory=lambda c, a, st, b, k, p, cr, n: FakeListener(a))
        orch = CallOrchestrator(GatewayConfig(), adapter, comps)
        caller = Caller(orch, clock, adapter)
        task = asyncio.create_task(script(caller))
        summary = await asyncio.wait_for(orch.run(), 20)
        task.cancel()
        driver.cancel()
        return summary, adapter

    summary, adapter = asyncio.run(main())
    assert made["sp"].texts("fixed")[-1].startswith("Ursäkta, något gick fel hos oss")
    assert summary.end_reason == "error" and adapter.hangups == ["error"] and ("finish", "error") in made["brain"].calls


def test_every_state_change_is_on_the_bus_and_in_the_summary():
    async def script(c):
        await c.wait_state(S.LISTENING)
        await c.hang_up()

    summary, *_ = run_call(script)
    changes = kinds(summary, StateChanged)
    assert [c.new for c in changes] == [s for _, s in summary.states]
    assert summary.metrics_summary["end_reason"] == "hangup"


@pytest.mark.parametrize("sentence_kind", ["assistant", "fixed"])
def test_sentences_keep_their_kind_and_language(sentence_kind):
    async def script(c):
        await c.wait_state(S.LISTENING)
        await c.utter("Hej")
        await c.wait_state(S.LISTENING, 2)
        await c.hang_up()

    reply = Reply("Ursäkta, något gick fel hos oss.", error=sentence_kind == "fixed")
    summary, brain, speaker, *_ = run_call(script, replies=[reply])
    said = [s for s in kinds(summary, AgentSentence)]
    assert said[0].kind == sentence_kind and speaker.said[-1] == (sentence_kind, "sv", said[0].text)

"""AgentBrain over the real phone agent (moto tables, the real tools, the
fake tool Lambdas of tests/test_phone_agent_support.py): the call row as
today, the booking guards unchanged (read-back and a later clear yes),
a barge-in that makes a read-back unheard, a language switch that keeps
the booking context and still needs a new read-back, streaming with tool
events only for real tools, the unsupported language, transfer, the
disclosure evidence and the end of the call. No AWS."""

import asyncio
import json

import pytest

from shared.agent_model import AgentModelError, ToolUse, Turn
from shared.features import phone_agent as pa
from shared.features.phone_agent import copy, language, session
from test_phone_agent_calls import NUMBER, live  # noqa: F401 (fixture)
from test_phone_agent_support import CALLER, TENANT_A, env  # noqa: F401 (fixture)

from voice_gateway.audio.adapter import CallInfo
from voice_gateway.audio.frames import PCM16_16K
from voice_gateway.brain.agent_brain import AgentBrain, asked_question, awaiting_confirmation
from voice_gateway.brain.streaming import StreamingAgentModel
from voice_gateway.brain.voice_port import GatewayVoice
from voice_gateway.clock import FakeClock
from voice_gateway.config import BrainConfig, GatewayConfig
from voice_gateway.events import AgentSentence, AgentTurnDone, EventBus, ToolFinished, ToolStarted

assert live and env
TOMORROW = "2026-11-03"
HOLD = {"date": TOMORROW, "time": "19:00", "partySize": 4}
READ_BACK = "[sv] Ett bord för fyra i morgon klockan sju i namnet Anna, stämmer det?"
BOOK = ToolUse("u2", "create_reservation", {"holdId": "p1", "name": "Anna", "confirmed": True})


class Scripted:
    """A model that answers a list of Turns in order (the agent loop's port)."""

    def __init__(self, turns):
        self.turns = list(turns)

    def converse(self, system, messages, tools, *, max_tokens, timeout_s=None):
        item = self.turns.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeBedrock:
    def __init__(self, *answers):
        self.answers = list(answers)

    def converse_stream(self, **kwargs):
        return {"stream": iter(self.answers.pop(0))}


def text(chunk, index=0):
    return {"contentBlockDelta": {"contentBlockIndex": index, "delta": {"text": chunk}}}


def tool(name, args, index=1, use_id="u1"):
    return [{"contentBlockStart": {"contentBlockIndex": index, "start": {"toolUse": {"toolUseId": use_id,
                                                                                    "name": name}}}},
            {"contentBlockDelta": {"contentBlockIndex": index, "delta": {"toolUse": {"input": json.dumps(args)}}}},
            {"contentBlockStop": {"contentBlockIndex": index}}]


def stop(reason):
    return {"messageStop": {"stopReason": reason}}


class Call:
    """One gateway call through AgentBrain, driven synchronously."""

    def __init__(self, e, model=None, *, model_factory=None, ref="gw-1"):
        e.live._clients["voice"] = GatewayVoice()
        self.e = e
        self.bus = EventBus()
        self.bus.history = []
        self.loop = asyncio.new_event_loop()
        factory = model_factory or (lambda sink: model)
        self.brain = AgentBrain(GatewayConfig(), self.bus, FakeClock(), app=e.live, model_factory=factory)
        self.start = self.run(self.brain.start(CallInfo(ref, NUMBER, CALLER, PCM16_16K)))

    def run(self, coro):
        return self.loop.run_until_complete(coro)

    def say(self, words, lang="sv"):
        async def collect():
            return [item async for item in self.brain.turn(words, language=lang)]
        items = self.run(collect())
        assert isinstance(items[-1], AgentTurnDone) and sum(isinstance(i, AgentTurnDone) for i in items) == 1
        return items

    def row(self):
        return self.e.live.calls.get_call(TENANT_A, self.brain.call_id)

    def state(self):
        return json.loads(self.row().get("toolState") or "{}")

    def close(self):
        self.loop.close()


def spoken(items):
    return " ".join(i.text for i in items if isinstance(i, AgentSentence))


def done(items):
    return items[-1]


def book_script(*middle):
    return Scripted([Turn(tool_uses=(ToolUse("u1", "hold_table", HOLD),)), Turn(text=READ_BACK), *middle])


# --- the call row -------------------------------------------------------------------------------------

def test_start_answers_with_the_owner_greeting_and_the_aws_voice_languages(live):
    c = Call(live, Scripted([]))
    start = c.start
    assert start.call_id and not start.refused and start.language == "sv"
    assert "Du pratar med vår digitala assistent." in start.greeting and "AI" not in start.greeting
    assert start.greeting_sentences[start.disclosure_until] == "Du pratar med vår digitala assistent."
    assert set(start.allowed) == set(language.AWS_CALL_LANGUAGES)
    row = c.row()
    assert row["callRef"] == "connect-gw-gw-1" and row["status"] == "answered" and row["language"] == "sv"
    c.run(c.brain.greeting_played())
    assert c.row()["disclosure"]["playedAt"] and c.row()["disclosure"]["verbatim"] is True
    c.close()


def test_an_unknown_number_is_not_answered(live):
    live.live._clients["voice"] = GatewayVoice()
    brain = AgentBrain(GatewayConfig(), EventBus(), FakeClock(), app=live.live, model_factory=lambda s: None)
    start = asyncio.run(brain.start(CallInfo("x", "+46999999999", CALLER, PCM16_16K)))
    assert start.call_id is None and start.greeting is None


def test_a_booking_needs_the_read_back_and_a_later_clear_yes(live):
    live.availability.set(TOMORROW, ["19:00", "20:00"])
    c = Call(live, book_script(Turn(tool_uses=(BOOK,)), Turn(text="[sv] Klart, du är bokad. Välkommen.")))
    first = c.say("Ett bord för fyra i morgon klockan sju, Anna")
    assert spoken(first) == "Ett bord för fyra i morgon klockan sju i namnet Anna, stämmer det?"
    assert done(first).awaiting_confirmation and done(first).asked_question == "yes_no"
    assert done(first).tools == ("hold_table",) and not live.invoke.reservations
    second = c.say("Ja")
    assert spoken(second) == "Klart, du är bokad. Välkommen." and done(second).outcome == "booked"
    assert len(live.invoke.reservations) == 1 and not done(second).awaiting_confirmation
    c.close()


def test_a_barge_in_on_the_read_back_means_no_booking_on_the_next_yes(live):
    """The guard of the barge-in: the caller talked over the read-back, so
    their "ja" is no confirmation; the agent must read back again."""
    live.availability.set(TOMORROW, ["19:00", "20:00"])
    c = Call(live, book_script(Turn(tool_uses=(BOOK,)), Turn(text=READ_BACK), Turn(tool_uses=(BOOK,)),
                               Turn(text="[sv] Klart, du är bokad.")))
    c.say("Ett bord för fyra i morgon klockan sju, Anna")
    c.run(c.brain.mark_not_heard(["Ett bord för fyra i morgon"]))
    state = c.state()
    assert state["proposals"]["p1"]["turn"] == 2 and "resume" not in state
    messages = json.loads(c.row()["agentMessages"])
    assert messages[-1] == {"role": "assistant", "content": "Ett bord för fyra i morgon ..."}   # what was heard
    refused = c.say("Ja")
    assert not live.invoke.reservations                                   # the unheard read-back never books
    assert spoken(refused).endswith("stämmer det?") and done(refused).awaiting_confirmation
    booked = c.say("Ja")
    assert len(live.invoke.reservations) == 1 and done(booked).outcome == "booked"
    history = json.loads(c.row()["agentMessages"])
    assert history[0] == {"role": "user", "content": "Ett bord för fyra i morgon klockan sju, Anna"}   # context kept
    c.close()


def test_a_language_switch_keeps_the_booking_context_and_asks_for_a_new_read_back(live):
    live.availability.set(TOMORROW, ["19:00", "20:00"])
    english = "[en] A table for four tomorrow at seven for Anna, is that right?"
    c = Call(live, book_script(Turn(tool_uses=(BOOK,)), Turn(text=english), Turn(tool_uses=(BOOK,)),
                               Turn(text="[en] Done, you are booked.")))
    c.say("Ett bord för fyra i morgon klockan sju, Anna")
    said = c.run(c.brain.set_language("en", 0, "strong"))
    assert said == "I am the restaurant's digital assistant." and c.brain.language == "en"
    row = c.row()
    assert row["language"] == "en" and row["languageSwitches"] == 1 and "en" in row["disclosure"]["languages"]
    first = c.say("Yes, that is right", "en")
    assert not live.invoke.reservations and spoken(first) == english[5:]   # heard in Swedish: read back again
    assert {i.language for i in first if isinstance(i, AgentSentence)} == {"en"}
    second = c.say("Yes", "en")
    assert len(live.invoke.reservations) == 1 and done(second).outcome == "booked"
    assert json.loads(c.row()["agentMessages"])[0]["content"] == "Ett bord för fyra i morgon klockan sju, Anna"
    assert c.run(c.brain.set_language("en", 0, "strong")) is None          # no second disclosure, no count
    assert c.row()["languageSwitches"] == 1
    c.close()


def test_the_switch_cap_holds_on_the_gateway_line(live):
    c = Call(live, Scripted([]))
    for code in ("en", "de", "sv"):
        c.run(c.brain.set_language(code, 0, "strong"))
    assert c.row()["languageSwitches"] == 3 and session.language_locked(c.row())
    assert c.run(c.brain.set_language("en", 0, "strong")) is None and c.row()["language"] == "sv"
    assert c.run(c.brain.set_language("fa", 0, "strong")) is None          # no voice: never a switch
    c.close()


# --- streaming ----------------------------------------------------------------------------------------

def test_streaming_speaks_the_preamble_announces_the_real_tool_and_reconciles(live):
    live.availability.set(TOMORROW, ["19:00", "20:00"])
    rounds = ([text("[sv] Ett ögonblick, "), text("jag kollar det.")]
              + tool("get_availability", {"date": TOMORROW, "partySize": 4}) + [stop("tool_use")],
              [text("[sv] Klockan sju är ledigt i morgon. "), text("Vill du att jag bokar det?"), stop("end_turn")])

    def factory(sink):
        return StreamingAgentModel(sink, BrainConfig(), client=FakeBedrock(*rounds), model_id="m")

    c = Call(live, model_factory=factory)
    items = c.say("Har ni ett bord för fyra i morgon?")
    kinds = [type(i).__name__ for i in items]
    assert kinds == ["AgentSentence", "ToolStarted", "ToolFinished", "AgentSentence", "AgentSentence",
                     "AgentTurnDone"]
    sentences = [i for i in items if isinstance(i, AgentSentence)]
    assert [(s.text, s.preamble) for s in sentences] == [("Ett ögonblick, jag kollar det.", True),
                                                        ("Klockan sju är ledigt i morgon.", False),
                                                        ("Vill du att jag bokar det?", False)]
    assert items[1].name == "get_availability" and done(items).tools == ("get_availability",)
    assert done(items).rounds == 2 and done(items).asked_question == "yes_no"
    on_bus = [e for e in c.bus.history if isinstance(e, AgentSentence)]
    assert on_bus == sentences                                             # published once each
    assert c.row()["turnCount"] >= 3
    c.close()


def test_a_switch_on_the_model_tag_streams_the_disclosure_first_and_never_twice(live):
    rounds = ([text("[en] Of course. "), text("For how many people would that be?"), stop("end_turn")],)

    def factory(sink):
        return StreamingAgentModel(sink, BrainConfig(), client=FakeBedrock(*rounds), model_id="m")

    c = Call(live, model_factory=factory)
    items = c.say("Hi, I would like to book a table for tomorrow evening please")
    said = [(i.kind, i.language, i.text) for i in items if isinstance(i, AgentSentence)]
    assert said == [("fixed", "en", "I am the restaurant's digital assistant."),
                    ("assistant", "en", "Of course. For how many people would that be?")]
    assert done(items).language == "en" and c.brain.language == "en"
    assert [a.name for a in done(items).actions] == ["language_detection"]
    row = c.row()
    assert row["language"] == "en" and row["languageSwitches"] == 1 and "en" in row["disclosure"]["languages"]
    c.close()


def test_end_call_never_announces_a_tool(live):
    rounds = ([text("[sv] Tack för samtalet, hej då.")] + tool("end_call", {"outcome": "info"}) + [stop("tool_use")],)

    def factory(sink):
        return StreamingAgentModel(sink, BrainConfig(), client=FakeBedrock(*rounds), model_id="m")

    c = Call(live, model_factory=factory)
    items = c.say("Tack, det var allt")
    assert not [i for i in items if isinstance(i, (ToolStarted, ToolFinished))]
    assert spoken(items) == "Tack för samtalet, hej då." and [a.name for a in done(items).actions] == ["end_call"]
    c.close()


def test_an_unsupported_language_is_held_and_answered_by_the_fixed_copy_in_its_own_voices(live):
    rounds = ([text("[fa] سلام، بله، برای چند نفر میز می‌خواهید؟"), stop("end_turn")],)

    def factory(sink):
        return StreamingAgentModel(sink, BrainConfig(), client=FakeBedrock(*rounds), model_id="m")

    c = Call(live, model_factory=factory)
    items = c.say("سلام، من یک میز برای چهار نفر فردا می‌خواهم")
    sentences = [(i.kind, i.language) for i in items if isinstance(i, AgentSentence)]
    assert {k for k, _ in sentences} == {"fixed"} and sentences[-1] == ("fixed", "en")
    assert all(lang != "fa" for _, lang in sentences)
    assert spoken(items) == copy.unsupported_language(connect=True)        # Elin never reads Persian
    assert done(items).language == "sv" and c.row()["language"] == "sv"
    c.close()


# --- transfer, errors, the end ------------------------------------------------------------------------

def test_a_transfer_is_left_to_the_line_and_an_unanswered_one_is_recorded(live):
    c = Call(live, Scripted([Turn(text="[sv] Jag kopplar dig.", tool_uses=(ToolUse("t", "transfer_to_staff", {}),)),
                             Turn(text="[sv] Ett ögonblick.")]))
    items = c.say("Kan jag prata med en människa")
    (action,) = [a for a in done(items).actions if a.name == "transfer_to_number"]
    assert action.arguments["transfer_number"] == "+46812345678"
    assert action.arguments["client_message"] == copy.TRANSFERRING["sv"]
    assert c.row()["status"] == "talking"                                  # not finished before the line transfers
    said = c.run(c.brain.transfer_result(False))
    assert said == copy.TRANSFER_UNANSWERED["sv"]
    row, state = c.row(), c.state()
    assert row["transfer"]["answered"] is False and state.get("outcome") != "transferred"
    c.run(c.brain.finish("hangup"))
    assert c.row()["status"] == "processed" and c.row()["outcome"] != "transferred"
    c.close()


def test_an_answered_transfer_finishes_as_transferred(live):
    c = Call(live, Scripted([Turn(tool_uses=(ToolUse("t", "transfer_to_staff", {}),)), Turn(text="[sv] Visst.")]))
    c.say("En människa tack")
    assert c.run(c.brain.transfer_result(True)) is None
    c.run(c.brain.finish("transferred"))
    row = c.row()
    assert (row["status"], row["outcome"], row["endReason"]) == ("processed", "transferred", "transferred")
    c.close()


def test_a_model_failure_is_the_error_sentence_and_a_broken_turn_never_goes_silent(live, monkeypatch):
    c = Call(live, Scripted([AgentModelError("rejected")]))
    items = c.say("Hej")
    assert spoken(items) == copy.ERROR["sv"] and done(items).error
    assert {i.kind for i in items if isinstance(i, AgentSentence)} == {"fixed"}

    def boom(*args, **kwargs):
        raise RuntimeError("dynamo down")
    monkeypatch.setattr(session, "call_turn", boom)
    items = c.say("Hallå")
    assert spoken(items) == copy.ERROR["sv"] and done(items).error and done(items).rounds == 0
    c.close()


def test_greeting_interrupted_repeats_the_short_disclosure_once_and_finish_meters(live):
    c = Call(live, Scripted([]))
    assert c.run(c.brain.greeting_interrupted()) == copy.DISCLOSURE_SHORT["sv"]
    assert c.row()["disclosure"]["repeats"] == 1
    c.run(c.brain.greeting_played(spoken=copy.DISCLOSURE_SHORT["sv"]))
    assert c.row()["disclosure"]["playedAt"]
    c.run(c.brain.finish("still_there"))
    row = c.row()
    assert row["status"] == "processed" and row["endReason"] == "still_there" and row["metered"] is True
    c.run(c.brain.finish("hangup"))                                        # idempotent
    assert c.row()["endReason"] == "still_there"
    c.close()


def test_the_max_call_length_ends_with_the_owner_sentence(live, monkeypatch):
    c = Call(live, Scripted([]))
    live.clock.value = live.clock.value.replace(hour=live.clock.value.hour + 1)
    items = c.say("Hej igen")
    assert spoken(items) == copy.MAX_LENGTH["sv"]
    assert [a.name for a in done(items).actions] == ["end_call"] and c.row()["status"] == "processed"
    c.close()


# --- pure helpers -------------------------------------------------------------------------------------

@pytest.mark.parametrize("reply, kind", [
    ("Ett bord klockan sju, stämmer det?", "yes_no"), ("Ska jag boka det?", "yes_no"),
    ("För hur många personer?", "number"), ("Vilken tid passar?", "number"),
    ("Vad heter du?", "open"), ("Välkommen.", None), ("Is that right?", "yes_no"),
])
def test_asked_question(reply, kind):
    assert asked_question(reply) == kind


def test_awaiting_confirmation():
    assert awaiting_confirmation({"turn": 2, "proposals": {"p1": {"turn": 2}}})
    assert awaiting_confirmation({"turn": 2, "proposals": {"p1": {"turn": 3}}})          # carried by not heard
    assert not awaiting_confirmation({"turn": 3, "proposals": {"p1": {"turn": 2}}})
    assert not awaiting_confirmation({"turn": 2, "outcome": "booked", "proposals": {"p1": {"turn": 2}}})
    assert not awaiting_confirmation({"turn": 2, "proposals": {"p1": {"turn": 2, "reservationId": "r1"}}})


def test_pa_now_is_the_fixture_clock(live):
    assert pa.now() == live.clock.value

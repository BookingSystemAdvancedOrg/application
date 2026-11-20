"""Retest findings on dev, 2026-11-20 (second round): a long Persian first
turn answered silently in Swedish, "halv sju" held as 19:30, the model
thinking aloud and confusing "fredag" said on a Friday, a German read-back
promising a phone call instead of the SMS, and a slow turn after a finished
booking. Each test reproduces the finding first."""

import json
import re
from datetime import date

import pytest

from shared.agent_model import ToolUse, Turn
from shared.features.phone_agent import agent, copy, language
from shared.features.phone_agent.tools import Executor
from test_phone_agent_calls import live  # noqa: F401 (fixture)
from test_phone_agent_live_bugs import TOMORROW, _booked, _create, _ex, _hold, _lex, _next, _result
from test_phone_agent_support import CALLER, LOC_A, TENANT_A, env  # noqa: F401 (fixture)
from test_phone_agent_v2 import Scripted

assert live and env
DASHES = re.compile("[–—]|&[nm]dash;")
PERSIAN_LONG = "سلام، می‌خواهم برای فردا شب ساعت هفت یک میز برای چهار نفر رزرو کنم"
AWS = language.AWS_CALL_LANGUAGES


# --- 1: a strong hint for a language the line cannot speak -------------------------------------------

def test_a_long_persian_turn_tagged_swedish_is_the_unsupported_path_not_a_silent_swedish_answer():
    hint = language.hint(PERSIAN_LONG)
    assert hint == "fa" and "fa" not in AWS
    decided = language.decision("sv", hint, "sv", PERSIAN_LONG, allowed=AWS)
    assert (decided.language, decided.unsupported, decided.reason) == ("sv", "fa", "unsupported")
    flash = language.FLASH_LANGUAGES                     # the ElevenLabs line cannot speak Persian either
    assert language.decision("sv", hint, None, PERSIAN_LONG, allowed=flash).unsupported == "fa"


def test_the_strong_unsupported_hint_never_overrides_what_must_still_work():
    sv = "Hej, jag skulle vilja boka ett bord för fyra personer i morgon"
    assert language.decision("sv", language.hint(sv), "sv", sv, allowed=AWS).unsupported is None
    assert language.decision("sv", "fa", "sv", "سلام", allowed=AWS).unsupported is None      # one word: short
    english = "میشه انگلیسی صحبت کنیم لطفا"          # asks for English in Persian: English, not the question
    decided = language.decision("sv", language.hint(english), "en", english, allowed=AWS)
    assert (decided.language, decided.unsupported) == ("en", None)
    flash = language.FLASH_LANGUAGES                     # a language the line speaks: the old rules
    assert language.decision("sv", "fa", "sv", PERSIAN_LONG, allowed=flash + ("fa",)).unsupported is None


def test_persian_first_turn_with_a_tool_switches_to_the_english_voice_and_keeps_the_lookup(live):
    live.availability.set(TOMORROW, ["19:00"])
    out, attrs = _lex(live, "contact-fa-long", "Hej")
    live.model.responses = [
        Turn(tool_uses=(ToolUse("a1", "get_availability", {"date": TOMORROW, "partySize": 4, "time": "19:00"}),)),
        Turn(text="[sv] Det finns ett bord klockan sju i morgon kväll. Vilket namn ska bokningen stå på?")]
    out, attrs = _lex(live, "contact-fa-long", PERSIAN_LONG, attrs)
    assert attrs["lang"] == "en" and attrs["voice"] == "Amy"
    assert copy.UNSUPPORTED_LANGUAGE["en"] in attrs["say"] and "Vilket namn" not in attrs["say"]
    row = live.live.calls.get_call(attrs["tenantId"], attrs["callId"])
    messages = json.loads(row["agentMessages"])
    assert any(b.get("type") == "tool_result" for m in messages if isinstance(m.get("content"), list)
               for b in m["content"])             # the lookup stays in the history: the booking context survives
    assert json.loads(row["toolState"])["unsupportedStep"] == "english"


# --- 2: "halv sju" is 18:30 on the server too -----------------------------------------------------------

@pytest.mark.parametrize("lang, said, wrong, right", [
    ("sv", "Halv sju går bra", "19:30", "18:30"),
    ("no", "Halv syv passer fint", "19:30", "18:30"),
    ("da", "Halv syv er fint", "19:30", "18:30"),
    ("de", "Halb acht passt gut", "20:30", "19:30"),
    ("nl", "Half acht is goed", "20:30", "19:30"),
    ("sv", "Vi tar halv 7", "19:30", "18:30"),
    ("en", "Half past six is fine", "19:30", "18:30"),
    ("en", "Half seven is fine", "18:30", "19:30"),          # British: half seven is 19:30 in English only
])
def test_a_half_hour_the_caller_said_is_the_only_half_hour_that_can_be_held(env, lang, said, wrong, right):
    env.availability.set(TOMORROW, ["18:30", "19:00", "19:30", "20:00", "20:30"])
    ex = _ex(env, language_=lang)
    model = Scripted([Turn(tool_uses=(_hold(1, wrong),)), Turn(tool_uses=(_hold(2, right),)),
                      Turn(text=f"[{lang}] ok")])
    messages = []
    agent.run_turn(model, messages, said, ex, system="s", lang=lang)
    refused = _result(messages, "h1")
    assert refused["held"] is False and refused["code"] == "halfHour" and refused["time"] == right
    assert "holdId" not in refused and right in refused["say"]
    held = _result(messages, "h2")
    assert held["holdId"] == "p1" and held["time"] == right
    assert [p["time"] for p in ex.state["proposals"].values()] == [right]


def test_the_half_hour_guard_leaves_other_utterances_alone(env):
    env.availability.set(TOMORROW, ["19:00", "19:30"])
    for text in ("Sju går bra", "Nej, inte halv sju, klockan sju", "Ja", None):
        ex = _ex(env)
        ex.caller_text = text
        assert ex.run("hold_table", {"date": TOMORROW, "time": "19:00", "partySize": 2})["holdId"] == "p1"
    ex = _ex(env, language_="fr")                         # "half" is no clock word in French
    ex.caller_text = "half sept"
    assert ex.run("hold_table", {"date": TOMORROW, "time": "19:30", "partySize": 2})["holdId"] == "p1"


# --- 3: no thinking aloud; "fredag" said on a Friday ------------------------------------------------------

@pytest.mark.parametrize("raw, said", [
    ("Halv åtta... nej, klockan sju på kvällen. Stämmer det?", "Klockan sju på kvällen. Stämmer det?"),
    ("Okej. Då är det tre personer till? Nej, två personer. Vilket namn?", "Okej. Två personer. Vilket namn?"),
    ("Um halb acht… nein, um sieben Uhr. Stimmt das?", "Um sieben Uhr. Stimmt das?"),
    ("Nej, tyvärr är sju fullt. Passar åtta?", "Nej, tyvärr är sju fullt. Passar åtta?"),     # a real no stays
    ("Vill du ha mer information om det?", "Vill du ha mer information om det?"),
])
def test_a_spoken_self_correction_keeps_only_the_final_answer(raw, said):
    assert agent.spoken_text(raw, "sv") == said


def test_the_prompt_forbids_thinking_aloud():
    prompt = agent.system_prompt("sv", "Sara", "Bistro", date(2026, 11, 6), "formal")
    assert "Never say a correction or your deliberation" in prompt
    assert not DASHES.search(prompt)


FRIDAY = date(2026, 11, 6)
HOURS = {"fri": [{"opensAt": "11:00", "closesAt": "23:00"}]}


def _friday_ex(e, now_time="12:00", caller_text="Ett bord på fredag för två"):
    ex = Executor(tenant_id=TENANT_A, tenant_row={"tenantId": TENANT_A, "name": "Bistro"},
                  location={"locationId": LOC_A, "name": "Bistro", "businessHours": HOURS}, loc_config={},
                  invoke=e.invoke, state={"turn": 1}, caller_phone=CALLER, today=FRIDAY, now_time=now_time)
    ex.caller_text = caller_text
    return ex


def test_fredag_said_on_a_friday_during_service_is_asked_once(env):
    env.availability.set("2026-11-06", ["19:00"])
    env.availability.set("2026-11-13", ["19:00"])
    ex = _friday_ex(env)
    asked = ex.run("get_availability", {"date": "2026-11-13", "partySize": 2})
    assert asked["code"] == "whichDay" and "nästa fredag" in asked["say"] and "I dag" in asked["say"]
    assert asked["today"] == "2026-11-06" and asked["next"] == "2026-11-13" and not asked.get("times")
    again = ex.run("get_availability", {"date": "2026-11-13", "partySize": 2})       # retried at once: no answer
    assert again["code"] == "whichDay" and not again.get("times")                    # (retest 3: was let through)
    ex.state["turn"] = ex.turn = 2
    ex.caller_text = "Nästa fredag"                                                  # the caller's answer decides
    again = ex.run("get_availability", {"date": "2026-11-13", "partySize": 2})
    assert again["times"] == ["19:00"] and again["date"] == "2026-11-13" and again["relative"] == "in 7 days"


def test_nasta_fredag_and_after_service_mean_next_friday_and_a_wrong_date_is_corrected(env):
    env.availability.set("2026-11-06", ["19:00"])
    env.availability.set("2026-11-13", ["19:00"])
    ex = _friday_ex(env, caller_text="Nästa fredag klockan sju")
    wrong = ex.run("get_availability", {"date": "2026-11-06", "partySize": 2})
    assert wrong["code"] == "weekdayMismatch" and wrong["date"] == "2026-11-13" and not wrong.get("times")
    assert ex.run("get_availability", {"date": "2026-11-13", "partySize": 2})["times"] == ["19:00"]
    late = _friday_ex(env, now_time="23:30")               # today's service has ended: next Friday
    assert late.run("hold_table", {"date": "2026-11-06", "time": "19:00", "partySize": 2})["code"] == \
        "weekdayMismatch"
    assert late.run("hold_table", {"date": "2026-11-13", "time": "19:00", "partySize": 2})["holdId"] == "p1"
    today = _friday_ex(env, caller_text="I dag fredag klockan sju")
    assert today.run("hold_table", {"date": "2026-11-06", "time": "19:00", "partySize": 2})["holdId"] == "p1"


def test_a_weekday_said_on_another_day_is_the_next_one_and_the_result_says_which(env):
    env.availability.set("2026-11-06", ["19:00"])
    env.availability.set("2026-11-13", ["19:00"])
    ex = _ex(env)                                          # Monday 2026-11-02
    ex.caller_text = "Fredag klockan sju för två"
    wrong = ex.run("get_availability", {"date": "2026-11-13", "partySize": 2})
    assert wrong["code"] == "weekdayMismatch" and wrong["date"] == "2026-11-06" and wrong["weekday"] == "Friday"
    right = ex.run("get_availability", {"date": "2026-11-06", "partySize": 2})
    assert right["times"] == ["19:00"] and right["relative"] == "in 4 days"
    ex.caller_text = "Fredag den trettonde"               # a date of the month: the caller said which
    assert ex.run("get_availability", {"date": "2026-11-13", "partySize": 2})["times"] == ["19:00"]


# --- 4: the confirmation is the SMS link, never a phone call ---------------------------------------------

def test_the_prompt_says_the_confirmation_is_a_text_message():
    prompt = agent.system_prompt("de", "Sara", "Bistro", date(2026, 11, 2), "formal")
    assert "mention that in the read-back" not in prompt
    assert "Never say that anyone will call the caller" in prompt and not DASHES.search(prompt)


@pytest.mark.parametrize("lang, sentence", [
    ("de", "Wir rufen Sie unter der Nummer an, von der Sie anrufen."),
    ("sv", "Vi ringer dig på numret du ringer från."),
    ("en", "We will call you on the number you are calling from."),
    ("no", "Vi ringer deg på nummeret du ringer fra."),
])
def test_a_read_back_never_promises_a_call_back_to_the_number(env, lang, sentence):
    env.availability.set(TOMORROW, ["18:30"])
    ex = _ex(env, language_=lang)
    readback_ = {"de": "Ein Tisch für vier am Dienstag um halb sieben am Abend, auf den Namen Anna.",
                 "sv": "Ett bord för fyra på tisdag klockan halv sju på kvällen, i namnet Anna.",
                 "en": "A table for four on Tuesday at half past six in the evening, for Anna.",
                 "no": "Et bord for fire på tirsdag klokka halv syv om kvelden, i navnet Anna."}[lang]
    question = {"de": "Stimmt das?", "sv": "Stämmer det?", "en": "Is that correct?", "no": "Stemmer det?"}[lang]
    model = Scripted([Turn(tool_uses=(_hold(1, "18:30"),)), Turn(text=f"[{lang}] {readback_} {sentence} {question}")])
    result = agent.run_turn(model, [], "x", ex, system="s", lang=lang)
    assert sentence not in result.reply and readback_ in result.reply
    assert ex.state["readBack"]["holdId"] == "p1"


def test_a_callback_promise_is_kept_when_there_is_no_hold():
    model = Scripted([Turn(text="[sv] Restaurangen ringer dig på numret du ringer från i morgon.")])
    from test_phone_agent_v2 import NoTools
    result = agent.run_turn(model, [], "Kan ni ringa mig?", NoTools(), system="s")
    assert "ringer dig på numret" in result.reply


# --- 5: a yes after a finished booking ---------------------------------------------------------------------

def _booked_ex(env):
    env.availability.set(TOMORROW, ["19:00"])
    ex = _ex(env)
    model = Scripted([Turn(tool_uses=(_hold(1, "19:00", 2),)),
                      Turn(text="[sv] Ett bord för två på tisdag klockan sju på kvällen, i namnet Anna. Stämmer det?"),
                      Turn(tool_uses=(_create(1, "p1"),)),
                      Turn(text="[sv] Klart, bordet är bokat. Kan jag hjälpa dig med något mer?")])
    messages = []
    agent.run_turn(model, messages, "Två personer klockan sju i morgon, Anna", ex, system="s")
    _next(ex)
    agent.run_turn(model, messages, "Ja", ex, system="s")
    assert _booked(env) == ["19:00"]
    return ex, messages


@pytest.mark.parametrize("said", ["ja tack boka det", "Ja", "Ja, boka det tack", "Okej, ja"])
def test_a_repeated_yes_after_the_booking_is_answered_at_once_without_the_model(env, said):
    ex, messages = _booked_ex(env)
    _next(ex)
    model = Scripted([])                                   # the model is never asked
    result = agent.run_turn(model, messages, said, ex, system="s")
    assert model.calls == 0 and not result.tool_calls
    assert result.reply == copy.ALREADY_BOOKED["sv"] and "?" in result.reply
    assert messages[-1] == {"role": "assistant", "content": result.reply}
    assert _booked(env) == ["19:00"]


def test_anything_else_after_the_booking_still_goes_to_the_model(env):
    ex, messages = _booked_ex(env)
    _next(ex)
    model = Scripted([Turn(text="[sv] Vi ligger på Storgatan ett.")])
    agent.run_turn(model, messages, "Ja, var ligger ni?", ex, system="s")
    assert model.calls == 1
    _next(ex)
    model = Scripted([Turn(text="[sv] Vilken tid på lördag?")])
    agent.run_turn(model, messages, "Ja, ett bord till på lördag", ex, system="s")
    assert model.calls == 1


def test_already_booked_sentences_have_no_dashes():
    assert {"sv", "en"} <= set(copy.ALREADY_BOOKED)
    assert not any(DASHES.search(x) for x in copy.ALREADY_BOOKED.values())


# --- adversarial review of the fixes ------------------------------------------------------------------------

def test_a_reply_in_another_language_the_line_speaks_is_not_overridden_by_the_hint():
    decided = language.decision("sv", "fa", "ar", PERSIAN_LONG, allowed=AWS)      # the model read Arabic
    assert decided.unsupported is None
    assert language.decision("sv", "fa", "fa", PERSIAN_LONG, allowed=AWS).unsupported == "fa"


def test_a_locked_call_never_takes_the_unsupported_path_from_the_hint():
    from shared.features.phone_agent import session
    result = agent.TurnResult("Hej", language="sv")
    assert session._after_turn("sv", "fa", result, PERSIAN_LONG, {}, ("sv",), locked=True).unsupported is None


def test_the_half_hour_guard_accepts_another_time_the_caller_also_named(env):
    env.availability.set(TOMORROW, ["18:30", "19:00"])
    ex = _ex(env)
    ex.caller_text = "Halv sju eller klockan sju"
    assert ex.run("hold_table", {"date": TOMORROW, "time": "19:00", "partySize": 2})["holdId"] == "p1"


def test_a_weekday_with_a_week_is_left_to_the_model(env):
    env.availability.set("2026-11-13", ["19:00"])
    ex = _ex(env)
    ex.caller_text = "Fredag om en vecka"
    assert ex.run("get_availability", {"date": "2026-11-13", "partySize": 2})["times"] == ["19:00"]
    ex.caller_text = "Fredag går inte, men lördag"                   # two weekdays: no check
    assert "code" not in ex.run("get_availability", {"date": "2026-11-13", "partySize": 2})


def test_a_yes_two_utterances_after_the_booking_goes_to_the_model(env):
    ex, messages = _booked_ex(env)
    _next(ex)
    _next(ex)
    model = Scripted([Turn(text="[sv] Vad mer kan jag hjälpa till med?")])
    agent.run_turn(model, messages, "Ja", ex, system="s")
    assert model.calls == 1


def test_the_disclosure_turn_is_never_skipped_by_the_already_booked_sentence(env):
    ex, messages = _booked_ex(env)
    _next(ex)
    model = Scripted([Turn(text="[sv] Jag är restaurangens digitala assistent. Bokningen är klar.")])
    agent.run_turn(model, messages, "Ja", ex, system="s", keep_preamble=True)
    assert model.calls == 1


def test_the_hint_never_hides_a_write_the_model_just_made():
    from shared.features.phone_agent import session
    wrote = agent.TurnResult("Booked.", tool_calls=[{"name": "create_reservation", "ok": True}], language="en")
    decided = session._after_turn("en", "fa", wrote, PERSIAN_LONG, {}, AWS)
    assert (decided.language, decided.unsupported) == ("en", None)
    read = agent.TurnResult("Seven is free.", tool_calls=[{"name": "get_availability", "ok": True}], language="en")
    assert session._after_turn("en", "fa", read, PERSIAN_LONG, {}, AWS).unsupported == "fa"

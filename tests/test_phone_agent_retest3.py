"""Retest 2 leftovers on dev, 2026-11-20: the model read back "fredag den
sjunde november" for the 27th and then corrected itself aloud ("Rättelse:
..."), "fredag" said on a Friday was silently taken for next Friday, the
part of the day changed between replies, and the first reply on the
recognize-text line greeted again with another greeting. Each test
reproduces the finding first."""

import json
import re
from datetime import date

import pytest

from shared.agent_model import ToolUse, Turn
from shared.features.phone_agent import agent, copy, readback, spoken, tools
from shared.features.phone_agent.tools import Executor
from test_phone_agent_calls import live  # noqa: F401 (fixture)
from test_phone_agent_live_bugs import TOMORROW, _create, _ex, _hold, _lex, _next, _result
from test_phone_agent_support import CALLER, LOC_A, TENANT_A, env  # noqa: F401 (fixture)
from test_phone_agent_v2 import Scripted

assert live and env
DASHES = re.compile("[\\u2013\\u2014]|&[nm]dash;")

SV_ORDINALS = ("första andra tredje fjärde femte sjätte sjunde åttonde nionde tionde elfte tolfte trettonde "
               "fjortonde femtonde sextonde sjuttonde artonde nittonde tjugonde tjugoförsta tjugoandra "
               "tjugotredje tjugofjärde tjugofemte tjugosjätte tjugosjunde tjugoåttonde tjugonionde trettionde "
               "trettioförsta").split()
SV_NUMBERS = ("ett två tre fyra fem sex sju åtta nio tio elva tolv tretton fjorton femton sexton sjutton arton "
              "nitton tjugo tjugoett tjugotvå tjugotre tjugofyra tjugofem tjugosex tjugosju tjugoåtta tjugonio "
              "trettio trettioett").split()


# --- 1: the server says how the slot is read back -----------------------------------------------------

@pytest.mark.parametrize("day", range(1, 32))
def test_swedish_ordinal_and_number_words_are_right_for_every_day_of_the_month(day):
    assert spoken.ordinal(day, "sv") == SV_ORDINALS[day - 1]
    assert spoken.number(day, "sv") == SV_NUMBERS[day - 1]
    said = spoken.day(f"2026-12-{day:02d}", "sv")
    assert said.endswith(f" den {SV_ORDINALS[day - 1]} december")
    assert readback.dates(said, "sv", date(2026, 12, 1)) == [date(2026, 12, day)]


def test_the_slot_phrase_is_the_owner_example():
    assert spoken.slot("2026-11-27", "17:00", 2, "sv") == \
        "fredag den tjugosjunde november klockan fem på eftermiddagen, två personer"
    assert spoken.slot("2026-11-27", "17:30", 1, "sv") == \
        "fredag den tjugosjunde november klockan halv sex på kvällen, en person"
    assert spoken.slot("2026-11-27", "17:00", 2, "en") == \
        "Friday the twenty-seventh of November at five o'clock in the afternoon, two people"
    assert spoken.slot("2026-11-27", "19:30", 4, "fr") is None          # no spoken form: readAs is used


@pytest.mark.parametrize("lang", spoken.LANGS)
def test_every_slot_phrase_is_a_read_back_the_server_accepts_and_a_wrong_day_is_not(lang):
    today = date(2026, 12, 1)
    for day in range(1, 32):
        iso = f"2026-12-{day:02d}"
        for hour in range(24):
            for minute in (0, 30):
                at = f"{hour:02d}:{minute:02d}"
                said = spoken.slot(iso, at, 2, lang)
                assert readback.covers(said, iso, at, lang, party=2, today=today) is True, said
                assert not DASHES.search(said)
    wrong = spoken.slot("2026-12-07", "17:00", 2, lang)
    assert readback.covers(wrong, "2026-12-27", "17:00", lang, party=2, today=today) is False


@pytest.mark.parametrize("at, part", [
    ("04:30", "på natten"), ("05:30", "på morgonen"), ("10:00", "på morgonen"), ("10:30", "på förmiddagen"),
    ("11:30", "på förmiddagen"), ("12:30", "på eftermiddagen"), ("17:00", "på eftermiddagen"),
    ("17:30", "på kvällen"), ("23:30", "på kvällen"),
])
def test_one_rule_for_the_part_of_the_day(at, part):
    assert spoken.period(at, "sv") == part
    assert spoken.clock(at, "sv").endswith(part)


def test_the_english_read_as_follows_the_same_rule():
    assert tools.clock_words("17:00") == "five o'clock in the afternoon"
    assert tools.clock_words("17:30") == "half past five in the evening"
    assert spoken.clock("17:00", "en") == "at five o'clock in the afternoon"
    assert spoken.clock("12:00", "sv") == "klockan tolv mitt på dagen"


def test_availability_and_hold_results_carry_the_spoken_slot(env):
    env.availability.set(TOMORROW, ["17:00", "17:30", "19:00"])
    ex = _ex(env)
    found = ex.run("get_availability", {"date": TOMORROW, "partySize": 2, "time": "17:00"})
    assert found["spokenDay"] == "tisdag den tredje november"
    assert found["spoken"] == "tisdag den tredje november klockan fem på eftermiddagen, två personer"
    assert found["spokenTimes"]["17:30"] == "klockan halv sex på kvällen"
    held = ex.run("hold_table", {"date": TOMORROW, "time": "17:30", "partySize": 2})
    assert held["spoken"] == "tisdag den tredje november klockan halv sex på kvällen, två personer"
    en = _ex(env, language_="en").run("hold_table", {"date": TOMORROW, "time": "17:00", "partySize": 2})
    assert en["spoken"] == "Tuesday the third of November at five o'clock in the afternoon, two people"


def test_a_refused_read_back_gives_the_exact_phrase_and_forbids_a_visible_correction(env):
    env.availability.set(TOMORROW, ["17:00"])
    ex = _ex(env)
    ex.heard("x")                                          # tracked
    ex.run("hold_table", {"date": TOMORROW, "time": "17:00", "partySize": 2})
    ex.heard("Då har jag tisdag den sjunde november klockan halv sex, två personer. Stämmer det?")
    _next(ex)
    refused = ex.run("create_reservation", {"holdId": "p1", "name": "Anna", "confirmed": True})
    assert refused["code"] == "readBackMismatch"
    assert refused["spoken"] == "tisdag den tredje november klockan fem på eftermiddagen, två personer"
    assert "exactly" in refused["say"] and "Rättelse" in refused["say"] and "once" in refused["say"]
    assert not env.invoke.reservations


def test_the_prompt_says_to_read_back_the_spoken_phrase():
    prompt = agent.system_prompt("sv", "Sara", "Bistro", date(2026, 11, 20), "formal")
    assert "`spoken`" in prompt and not re.search(r"(?<!\w)sjunde november", prompt)
    assert "eftermiddagen" in prompt and "kvällen" in prompt
    assert not DASHES.search(prompt)


# --- 1b: correction words are never spoken -----------------------------------------------------------

@pytest.mark.parametrize("raw, said", [
    ("Halv sex på fredag den sjunde november. Rättelse: det är fredag den tjugosjunde november, klockan fem. "
     "Stämmer det?", "Det är fredag den tjugosjunde november, klockan fem. Stämmer det?"),
    ("Fredag den sjunde, jag menar, fredag den tjugosjunde november. Stämmer det?",
     "Fredag den tjugosjunde november. Stämmer det?"),
    ("Klockan sex, nej förlåt, klockan fem på eftermiddagen. Stämmer det?",
     "Klockan fem på eftermiddagen. Stämmer det?"),
    ("Friday the seventh. Correction: Friday the twenty-seventh at five. Is that right?",
     "Friday the twenty-seventh at five. Is that right?"),
    ("Five o'clock, I mean, half past five. Is that right?", "Half past five. Is that right?"),
    ("Um sechs Uhr. Korrektur: um fünf Uhr. Stimmt das?", "Um fünf Uhr. Stimmt das?"),
    ("Nej, förlåt, vi har tyvärr fullt då. Passar sju?", "Nej, förlåt, vi har tyvärr fullt då. Passar sju?"),
    ("Vad jag menar är att köket stänger tio.", "Vad jag menar är att köket stänger tio."),
])
def test_a_correction_marker_keeps_only_the_final_statement(raw, said):
    code = "en" if raw[:1] in "FI" else "sv"
    assert agent.spoken_text(raw, code) == said


def test_a_corrected_read_back_counts_only_for_what_the_caller_heard_last(env):
    env.availability.set(TOMORROW, ["17:00"])
    ex = _ex(env)
    model = Scripted([Turn(tool_uses=(_hold(1, "17:00", 2),)),
                      Turn(text="[sv] Tisdag den sjunde november klockan halv sex. Rättelse: tisdag den tredje "
                                "november klockan fem på eftermiddagen, två personer, i namnet Anna. Stämmer det?"),
                      Turn(tool_uses=(_create(1, "p1"),)), Turn(text="[sv] Klart.")])
    messages = []
    result = agent.run_turn(model, messages, "Två personer klockan fem i morgon, Anna", ex, system="s")
    assert "sjunde" not in result.reply and "Rättelse" not in result.reply
    _next(ex)
    agent.run_turn(model, messages, "Ja", ex, system="s")
    assert [r["startTime"] for r in env.invoke.reservations.values()] == ["17:00"]


# --- 2: "fredag" said on a Friday is decided by the server --------------------------------------------

FRIDAY = date(2026, 11, 20)
HOURS = {"fri": [{"opensAt": "11:00", "closesAt": "23:00"}]}
TODAY_ISO, NEXT_ISO = "2026-11-20", "2026-11-27"


def _friday(e, now_time="12:00", language_="sv"):
    e.availability.set(TODAY_ISO, ["17:00", "19:00"])
    e.availability.set(NEXT_ISO, ["17:00", "19:00"])
    return Executor(tenant_id=TENANT_A, tenant_row={"tenantId": TENANT_A, "name": "Bistro"},
                    location={"locationId": LOC_A, "name": "Bistro", "businessHours": HOURS}, loc_config={},
                    invoke=e.invoke, state={"turn": 1}, caller_phone=CALLER, today=FRIDAY, now_time=now_time,
                    language=language_)


def _say(ex, text):
    _next(ex)
    ex.caller_text = text


def test_the_model_cannot_pass_next_friday_after_the_question_in_the_same_turn(env):
    ex = _friday(env)
    ex.caller_text = "Ett bord på fredag för två"
    assert ex.run("get_availability", {"date": NEXT_ISO, "partySize": 2})["code"] == "whichDay"
    again = ex.run("get_availability", {"date": NEXT_ISO, "partySize": 2})          # retried at once
    assert again["code"] == "whichDay" and not again.get("times")
    assert ex.run("hold_table", {"date": TODAY_ISO, "time": "19:00", "partySize": 2})["code"] == "whichDay"


def test_a_later_turn_without_the_weekday_cannot_bypass_the_question_with_an_iso_date(env):
    ex = _friday(env)
    ex.caller_text = "Jag vill boka på fredag"
    _say(ex, "Två personer klockan sju")                    # the model never asked
    assert ex.run("get_availability", {"date": NEXT_ISO, "partySize": 2})["code"] == "whichDay"
    assert ex.run("hold_table", {"date": NEXT_ISO, "time": "19:00", "partySize": 2})["code"] == "whichDay"
    assert not ex.state["proposals"]


@pytest.mark.parametrize("answer, chosen, other", [
    ("Nästa", NEXT_ISO, TODAY_ISO), ("Nästa fredag tack", NEXT_ISO, TODAY_ISO),
    ("I dag", TODAY_ISO, NEXT_ISO), ("Idag", TODAY_ISO, NEXT_ISO), ("Ikväll", TODAY_ISO, NEXT_ISO),
])
def test_the_answer_decides_and_the_other_friday_is_refused(env, answer, chosen, other):
    ex = _friday(env)
    ex.caller_text = "Ett bord på fredag för två"
    assert ex.run("get_availability", {"date": NEXT_ISO, "partySize": 2})["code"] == "whichDay"
    _say(ex, answer)
    assert ex.run("get_availability", {"date": chosen, "partySize": 2})["times"]
    _say(ex, "Ja, fredag klockan sju")                     # the weekday again: the chosen Friday
    assert ex.run("hold_table", {"date": other, "time": "19:00", "partySize": 2})["code"] == "weekdayMismatch"
    assert ex.run("hold_table", {"date": chosen, "time": "19:00", "partySize": 2})["holdId"] == "p1"


def test_a_yes_to_the_question_gets_a_short_reask_naming_both_days_never_the_same_sentence(env):
    ex = _friday(env)
    model = Scripted([Turn(tool_uses=(ToolUse("a1", "get_availability", {"date": NEXT_ISO, "partySize": 2}),)),
                      Turn(text="[sv] I dag eller nästa fredag?")])
    messages = []
    agent.run_turn(model, messages, "Ett bord på fredag för två", ex, system="s")
    assert _result(messages, "a1")["code"] == "whichDay"
    _next(ex)
    idle = Scripted([])
    first = agent.run_turn(idle, messages, "Ja", ex, system="s")
    assert idle.calls == 0 and "i dag" in first.reply and "nästa fredag" in first.reply
    assert "den tjugonde november" in first.reply and "den tjugosjunde november" in first.reply
    _next(ex)
    second = agent.run_turn(idle, messages, "Ja", ex, system="s")
    assert idle.calls == 0 and second.reply != first.reply and second.reply.endswith("?")
    assert "den tjugonde november" in second.reply
    _next(ex)
    ex.caller_text = "Ja"
    assert ex.run("hold_table", {"date": NEXT_ISO, "time": "19:00", "partySize": 2})["code"] == "weekdayMismatch"
    assert ex.run("hold_table", {"date": TODAY_ISO, "time": "19:00", "partySize": 2})["holdId"] == "p1"


def test_a_no_to_the_today_question_means_next_friday(env):
    ex = _friday(env)
    ex.caller_text = "Fredag för två"
    ex.run("get_availability", {"date": TODAY_ISO, "partySize": 2})
    for said in ("Ja", "Mm"):
        _say(ex, said)
        assert ex.day_reask(said, "sv")
    _say(ex, "Nej")
    assert ex.day_reask("Nej", "sv") is None
    assert ex.run("get_availability", {"date": NEXT_ISO, "partySize": 2})["times"]


def test_after_the_last_closing_time_fredag_is_next_friday_without_a_question(env):
    ex = _friday(env, now_time="23:15")
    ex.caller_text = "Fredag klockan sju för två"
    assert ex.run("get_availability", {"date": NEXT_ISO, "partySize": 2})["times"]
    assert ex.run("get_availability", {"date": TODAY_ISO, "partySize": 2})["code"] == "weekdayMismatch"


def test_the_which_day_results_carry_both_days_spoken(env):
    ex = _friday(env)
    ex.caller_text = "Fredag för två"
    asked = ex.run("get_availability", {"date": NEXT_ISO, "partySize": 2})
    assert asked["spokenToday"] == "fredag den tjugonde november"
    assert asked["spokenNext"] == "fredag den tjugosjunde november"
    assert not DASHES.search(json.dumps(asked, ensure_ascii=False))


def test_another_weekday_or_a_date_closes_the_question(env):
    env.availability.set("2026-11-21", ["19:00"])
    ex = _friday(env)
    ex.caller_text = "Fredag för två"
    ex.run("get_availability", {"date": TODAY_ISO, "partySize": 2})
    _say(ex, "Nej förresten, lördag")
    assert ex.run("get_availability", {"date": "2026-11-21", "partySize": 2})["times"] == ["19:00"]
    ex = _friday(env)
    ex.caller_text = "Fredag för två"
    ex.run("get_availability", {"date": TODAY_ISO, "partySize": 2})
    _say(ex, "Den tjugosjunde")
    assert ex.run("get_availability", {"date": NEXT_ISO, "partySize": 2})["times"]


# --- 4: the greeting on the Connect line and on recognize-text -------------------------------------------

def _counting(e, reply):
    seen = []
    e.model.converse = lambda system, messages, tools, **kw: (seen.append(system), Turn(text=reply))[1]
    return seen


def _greeting(e, attrs):
    return e.live.calls.get_call(attrs["tenantId"], attrs["callId"])["disclosure"]["text"]


def test_recognize_text_hej_without_a_played_greeting_gets_the_exact_owner_greeting(live):
    seen = _counting(live, "[sv] Hej och välkommen till oss! Vad vill du?")
    out, attrs = _lex(live, "rt-hej", "Hej")
    said = out["messages"][0]["content"]
    greeting = _greeting(live, attrs)
    assert greeting.startswith("Hej och välkommen till ") and "Du pratar med vår digitala assistent." in greeting
    assert not seen                                          # no model round
    assert copy.ssml_names(greeting.replace("'", "&apos;")) in said
    assert "AI" not in said


def test_recognize_text_with_a_question_first_says_the_disclosure_then_the_answer_without_a_welcome(live):
    _counting(live, "[sv] Hej och välkommen! Ja, vi har öppet till tio i kväll.")
    out, attrs = _lex(live, "rt-q", "Hej, hur länge har ni öppet i dag?")
    said = out["messages"][0]["content"]
    assert "Du pratar med vår digitala assistent." in said and "vi har öppet till tio" in said
    assert said.count("välkommen") == 1


def test_the_connect_line_answers_without_a_second_welcome_after_the_flow_greeting(live):
    seen = _counting(live, "[sv] Hej och välkommen till Bistro. Vad kan jag hjälpa dig med?")
    event = {"callerNumber": CALLER, "dialedNumber": __import__("test_phone_agent_calls").NUMBER, "greeted": "true"}
    out, attrs = _lex(live, "connect-hej", "Hej", event)
    said = out["messages"][0]["content"]
    assert seen and "välkommen" not in said and "Vad kan jag hjälpa dig med?" in said


def test_a_speech_turn_counts_as_greeted_by_the_flow(live):
    from test_phone_agent_calls import NUMBER
    seen = _counting(live, "[sv] Hej och välkommen. Vad kan jag hjälpa dig med?")
    event = {"sessionId": "connect-speech", "inputTranscript": "Hej", "inputMode": "Speech",
             "invocationSource": "DialogCodeHook", "bot": {"localeId": "sv_SE"}, "sessionState": {
                 "intent": {"name": "FallbackIntent", "state": "InProgress"},
                 "sessionAttributes": {"callerNumber": CALLER, "dialedNumber": NUMBER}}}
    out = live.live.handler({"lexV2": event})
    said = out["messages"][0]["content"]
    assert seen and "välkommen" not in said


def test_the_prompt_says_the_greeting_was_said():
    prompt = agent.system_prompt("sv", "Sara", "Bistro", date(2026, 11, 20), "formal")
    assert "never welcome" in prompt.lower() and "AI-assistent" not in prompt


def test_new_copy_has_no_dashes():
    for table in (copy.WHICH_DAY_AGAIN, copy.WHICH_DAY_TODAY):
        assert not any(DASHES.search(x) for x in table.values())
    assert not DASHES.search(tools.SAY["readBackMismatch"] + tools.SAY["whichDay"])



# --- adversarial review of the fixes ------------------------------------------------------------------------

def test_fredag_with_a_week_on_a_friday_is_left_to_the_model_and_opens_no_question(env):
    env.availability.set("2026-12-04", ["19:00"])
    ex = _friday(env)
    ex.caller_text = "Fredag om två veckor"
    assert ex.run("get_availability", {"date": "2026-12-04", "partySize": 2})["times"] == ["19:00"]
    assert "dayQuestion" not in ex.state and "dayChosen" not in ex.state


def test_a_question_opened_without_a_turn_number_never_crashes(env):
    ex = _friday(env)
    ex.state["turn"] = ex.turn = None
    ex.caller_text = "Fredag för två"
    ex.state["turn"] = ex.turn = 3
    ex.caller_text = "Nästa"
    assert ex.run("get_availability", {"date": NEXT_ISO, "partySize": 2})["times"]


def test_a_welcome_sentence_that_carries_the_booking_is_never_dropped():
    said = "Välkommen till Bistro, ett bord för två klockan sju i morgon. Stämmer det?"
    assert agent.without_welcome(said, "sv") == said
    assert agent.without_welcome("Hej och välkommen till Bistro. Vad kan jag hjälpa dig med?", "sv") == \
        "Vad kan jag hjälpa dig med?"
    assert agent.without_welcome("Hej och välkommen!", "sv") == ""


def test_the_day_question_never_lets_a_hold_through_and_a_live_hold_is_never_answered_by_the_reask(env):
    ex = _friday(env)
    ex.caller_text = "Fredag för två"
    for day_ in (TODAY_ISO, NEXT_ISO, "2026-11-21"):
        assert ex.run("hold_table", {"date": day_, "time": "19:00", "partySize": 2}).get("holdId") is None
    assert not ex.state["proposals"]

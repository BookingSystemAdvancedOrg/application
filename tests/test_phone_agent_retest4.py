"""Retest 3 leftovers on dev, 2026-11-20: an offer list put every time
under the last part of the day ("halv fem eller halv sex på kvällen" made
16:30 evening; "half past four, five o'clock, and half past five in the
afternoon" made 17:30 afternoon), and the assistant called the caller by
name ("Tack, Anna", "Thank you, Eve") despite the prompt rule. Each test
reproduces the finding first."""

import re
from datetime import date

import pytest

from shared.agent_model import Turn
from shared.features.phone_agent import agent, readback, spoken
from test_phone_agent_live_bugs import TOMORROW, _create, _ex, _hold, _next
from test_phone_agent_support import env  # noqa: F401 (fixture)
from test_phone_agent_v2 import Scripted

assert env
DASHES = re.compile("[\\u2013\\u2014]|&[nm]dash;")


# --- 1: the server says the offer list, grouped by the part of the day -------------------------------

@pytest.mark.parametrize("lang, times, said", [
    ("sv", ["16:30", "17:00", "17:30"], "halv fem eller fem på eftermiddagen, eller halv sex på kvällen"),
    ("sv", ["16:30", "17:30"], "halv fem på eftermiddagen, eller halv sex på kvällen"),
    ("sv", ["18:00", "18:30", "19:00"], "sex, halv sju eller sju på kvällen"),
    ("sv", ["17:30", "16:30", "17:30"], "halv fem på eftermiddagen, eller halv sex på kvällen"),
    ("sv", ["11:30", "12:00", "12:30"], "halv tolv på förmiddagen, tolv mitt på dagen, eller halv ett på "
                                        "eftermiddagen"),
    ("en", ["16:30", "17:00", "17:30"], "half past four or five o'clock in the afternoon, or half past five in "
                                        "the evening"),
    ("no", ["16:30", "17:30"], "halv fem på ettermiddagen, eller halv seks om kvelden"),
    ("da", ["16:30", "17:30"], "halv fem om eftermiddagen, eller halv seks om aftenen"),
    ("de", ["16:30", "17:00", "17:30"], "halb fünf oder fünf Uhr nachmittags, oder halb sechs abends"),
])
def test_the_offer_phrase_groups_consecutive_times_by_their_part_of_the_day(lang, times, said):
    assert spoken.offer(times, lang) == said


def test_the_offer_phrase_is_none_without_a_spoken_form_or_times():
    assert spoken.offer(["17:00"], "fr") is None
    assert spoken.offer([], "sv") is None
    assert spoken.offer(["17:00"], "sv") == "fem på eftermiddagen"


def test_every_offered_time_keeps_its_own_clock_face_in_english():
    heard = readback.clock_times(spoken.offer(["16:30", "17:00", "17:30"], "en"), "en")
    assert sorted(heard) == [(4, 30), (5, 0), (5, 30)]


def test_availability_results_carry_the_offer_phrase(env):
    env.availability.set(TOMORROW, ["16:30", "17:00", "17:30", "20:00"])
    found = _ex(env).run("get_availability", {"date": TOMORROW, "partySize": 2, "time": "17:00"})
    assert found["offerSpoken"] == "halv fem eller fem på eftermiddagen, eller halv sex på kvällen"
    en = _ex(env, language_="en").run("get_availability", {"date": TOMORROW, "partySize": 2, "time": "17:00"})
    assert en["offerSpoken"] == "half past four or five o'clock in the afternoon, or half past five in the evening"
    other = _ex(env, language_="fr").run("get_availability", {"date": TOMORROW, "partySize": 2, "time": "17:00"})
    assert "offerSpoken" not in other


def test_a_refused_hold_carries_the_offer_phrase_of_its_suggestions(env):
    env.availability.set(TOMORROW, ["16:30", "17:30"])
    refused = _ex(env).run("hold_table", {"date": TOMORROW, "time": "17:00", "partySize": 2})
    assert refused["held"] is False
    assert refused["offerSpoken"] == "halv fem på eftermiddagen, eller halv sex på kvällen"


def test_the_prompt_says_to_offer_with_the_offer_phrase():
    prompt = agent.system_prompt("sv", "Sara", "Bistro", date(2026, 11, 20), "formal")
    assert "`offerSpoken`" in prompt
    assert not DASHES.search(prompt)


# --- 2: the caller is never called by name -------------------------------------------------------------

@pytest.mark.parametrize("raw, names, said", [
    ("Tack, Anna. Då har jag tisdag klockan fem, två personer, under namnet Anna. Stämmer det?", ["Anna"],
     "Tack. Då har jag tisdag klockan fem, två personer, under namnet Anna. Stämmer det?"),
    ("Thank you, Eve. Your table is booked.", ["Eve"], "Thank you. Your table is booked."),
    ("Tack så mycket, Anna, bokningen är klar.", ["Anna Svensson"], "Tack så mycket, bokningen är klar."),
    ("Danke, Eve. Bis dann.", ["Eve"], "Danke. Bis dann."),
    ("Tack, Anna. Välkommen.", ["Bo"], "Tack, Anna. Välkommen."),                # not the guest's name
    ("Tack, Anna. Välkommen.", [], "Tack, Anna. Välkommen."),
    ("Bokningen är under namnet Anna. Tack, Anna.", ["Anna"], "Bokningen är under namnet Anna. Tack."),
])
def test_a_leading_thanks_with_the_guest_name_loses_the_name_only(raw, names, said):
    assert agent.without_name_thanks(raw, names) == said


@pytest.mark.parametrize("text, names", [
    ("Då har jag tisdag klockan fem under namnet Anna Svensson. Stämmer det?", ["Anna Svensson"]),
    ("Tuesday at five in the name of Eve. Is that right?", ["Eve"]),
    ("Dienstag um fünf auf den Namen Eve Weber. Stimmt das?", ["Eve Weber"]),
    ("Tisdag klockan fem, i namnet Anna. Stämmer det?", ["Anna"]),
    ("Ett bord för två. Stämmer det?", []),
])
def test_the_read_back_name_is_found(text, names):
    assert readback.names(text) == names


def test_the_caller_names_themselves_and_the_next_reply_does_not_call_them_by_it(env):
    env.availability.set(TOMORROW, ["17:00"])
    ex = _ex(env)
    model = Scripted([Turn(text="[sv] Tack, Anna. Hur många blir ni?")])
    result = agent.run_turn(model, [], "Jag heter Anna", ex, system="s")
    assert result.reply == "Tack. Hur många blir ni?"


def test_the_name_in_the_read_back_stays_and_the_booking_still_needs_it(env):
    env.availability.set(TOMORROW, ["17:00"])
    ex = _ex(env)
    model = Scripted([Turn(tool_uses=(_hold(1, "17:00", 2),)),
                      Turn(text="[sv] Tack, Anna. Då har jag tisdag den tredje november klockan fem på "
                                "eftermiddagen, två personer, under namnet Anna. Stämmer det?"),
                      Turn(tool_uses=(_create(1, "p1"),)), Turn(text="[sv] Tack, Anna. Bokningen är klar.")])
    messages = []
    first = agent.run_turn(model, messages, "Två personer klockan fem i morgon, det är Anna", ex, system="s")
    assert first.reply.startswith("Tack. Då har jag") and "under namnet Anna" in first.reply
    _next(ex)
    second = agent.run_turn(model, messages, "Ja", ex, system="s")
    assert [r["startTime"] for r in env.invoke.reservations.values()] == ["17:00"]
    assert second.reply == "Tack. Bokningen är klar."


def test_the_prompt_forbids_the_name_also_in_thanks():
    prompt = agent.system_prompt("sv", "Sara", "Bistro", date(2026, 11, 20), "formal")
    assert "Tack, Anna" in prompt and "under namnet" in prompt
    assert not DASHES.search(prompt)

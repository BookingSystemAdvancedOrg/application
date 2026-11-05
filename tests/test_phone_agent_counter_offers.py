"""ADR-L "Counter-offers" (DECISIONS.md 2026-11-04): scripted calls against
the agent-model simulator, each in Swedish and English.

The owner's rule: (1) the requested time has a table for the party -> book
it; (2) otherwise the closest free times the same day, on both sides of
the requested time; (3) a full day -> the next day; (4) a nearby table at
the requested time when the location has a floor plan. The tool answers
below are the shapes `hold_table` and `get_availability` return (ADR-L
"Bedrock agent" table); the agent loop and the real tools are Run 2 work.
"""

import pytest
from test_phone_agent_clients import Call, _use

from shared import agent_model
from shared.agent_model import COUNTER_OFFERS, NEARBY_TABLE, nearest_times

SV, EN = 0, 1
HOLD = ("hold_table", {"date": "2026-11-03", "time": "19:00", "partySize": 4})
OPENINGS = [
    ("Hej, jag vill boka ett bord för fyra imorgon klockan 19", "Ja", "Jag heter Anna", SV),
    ("Hi, I'd like to book a table for four tomorrow at 7 pm", "Yes", "My name is Anna", EN),
]

OPENING_YES = [(opening, yes, lang) for opening, yes, _, lang in OPENINGS]
OPENING = [(opening, lang) for opening, _, _, lang in OPENINGS]


# 1. The requested time is free: booked, no counter-offer ----------------------------------

@pytest.mark.parametrize("opening, yes, name, lang", OPENINGS)
def test_requested_time_free_is_booked(opening, yes, name, lang):
    call = Call()
    assert _use(call.say(opening)) == HOLD
    proposal = call.tool({"holdId": "h1", "date": "2026-11-03", "time": "19:00", "partySize": 4})
    assert "19:00" in proposal.text and not proposal.tool_uses
    call.say(yes)
    assert _use(call.say(name))[0] == "create_reservation"
    assert ("bokad" if lang == SV else "booked") in call.tool({"reservationId": "r1", "guaranteeRequired": False}).text


# 2. Time taken: the closest free times the same day, on both sides -------------------------

@pytest.mark.parametrize("opening, yes, name, lang", OPENINGS)
def test_time_taken_offers_closest_times_on_both_sides(opening, yes, name, lang):
    call = Call()
    call.say(opening)
    offer = call.tool({"held": False, "reason": "time_taken", "time": "19:00",
                       "before": ["18:30", "17:30", "17:00"], "after": ["19:30", "20:30", "21:00"], "nearby": []})
    expected = COUNTER_OFFERS["time_taken"][lang].format(
        time="19:00", times="17:30, 18:30" + (" eller " if lang == SV else " or ") + "19:30")
    assert offer.text == expected                     # three closest (tie -> earlier), spoken in clock order
    assert _use(call.say("Klockan 19.30" if lang == SV else "At 7:30 pm")) == (
        "hold_table", {"date": "2026-11-03", "time": "19:30", "partySize": 4})
    held = call.tool({"holdId": "h2", "date": "2026-11-03", "time": "19:30", "partySize": 4})
    assert "19:30" in held.text
    call.say(yes)
    assert _use(call.say(name)) == ("create_reservation", {"holdId": "h2", "name": "Anna", "partySize": 4,
                                                           "language": "sv" if lang == SV else "en"})


@pytest.mark.parametrize("opening, yes, lang", OPENING_YES)
def test_time_taken_with_free_times_on_one_side_only(opening, yes, lang):
    call = Call()
    call.say(opening)
    offer = call.tool({"held": False, "reason": "time_taken", "time": "19:00",
                       "before": [], "after": ["20:00", "20:30", "21:00", "21:30"], "nearby": []})
    assert "20:00" in offer.text and "21:30" not in offer.text   # at most three
    assert offer.text == COUNTER_OFFERS["time_taken"][lang].format(
        time="19:00", times="20:00, 20:30" + (" eller " if lang == SV else " or ") + "21:00")
    # a plain "yes" to a list of times asks which one
    assert call.say(yes).text == agent_model.SENTENCES["ask_time"][lang]


@pytest.mark.parametrize("ask, lang", [("Har ni bord för två imorgon klockan 20?", SV),
                                       ("Are there any free tables for two tomorrow at 8 pm?", EN)])
def test_availability_question_with_a_time_offers_the_closest_times(ask, lang):
    call = Call()
    assert _use(call.say(ask)) == ("get_availability", {"date": "2026-11-03", "partySize": 2, "time": "20:00"})
    answer = call.tool({"times": ["17:00", "18:00", "19:30", "20:30", "21:30"], "requestedTime": "20:00",
                        "before": ["19:30", "18:00", "17:00"], "after": ["20:30", "21:30"], "dayFull": False})
    assert answer.text == COUNTER_OFFERS["near_time"][lang].format(
        time="20:00", times="19:30, 20:30" + (" eller " if lang == SV else " or ") + "21:30")
    assert _use(call.say("20.30" if lang == SV else "8:30 pm"))[1]["time"] == "20:30"


# 3. The whole day is full: the next day, once, then a callback ----------------------------

@pytest.mark.parametrize("opening, yes, lang", OPENING_YES)
def test_full_day_offers_the_next_day_keeping_the_time(opening, yes, lang):
    call = Call()
    call.say(opening)
    full = call.tool({"held": False, "reason": "day_full", "time": "19:00", "before": [], "after": [], "nearby": [],
                      "dayFull": True})
    assert full.text == agent_model.CONFIRMATIONS["get_availability_full"][lang]
    assert _use(call.say(yes)) == ("get_availability", {"date": "2026-11-04", "partySize": 4, "time": "19:00"})
    offer = call.tool({"times": ["18:00", "19:00", "20:00"], "requestedTime": "19:00",
                       "before": ["18:00"], "after": ["20:00"], "dayFull": False})
    assert offer.text == COUNTER_OFFERS["near_time"][lang].format(
        time="19:00", times="18:00, 19:00" + (" eller " if lang == SV else " or ") + "20:00")
    assert _use(call.say("Klockan 19" if lang == SV else "At 7 pm"))[1] == {"date": "2026-11-04", "time": "19:00",
                                                                            "partySize": 4}


@pytest.mark.parametrize("opening, yes, lang", OPENING_YES)
def test_next_day_also_full_offers_a_callback_not_a_third_day(opening, yes, lang):
    call = Call()
    call.say(opening)
    call.tool({"held": False, "reason": "day_full", "before": [], "after": [], "nearby": [], "dayFull": True})
    assert _use(call.say(yes))[0] == "get_availability"
    second = call.tool({"times": [], "dayFull": True})
    assert second.text == COUNTER_OFFERS["next_day_full"][lang]
    name_, payload = _use(call.say(yes))
    assert name_ == "request_callback" and payload["topic"] == ("bokning" if lang == SV else "booking")


@pytest.mark.parametrize("opening, yes, other_day, lang", [
    ("Hej, jag vill boka ett bord för fyra på måndag klockan 19", "Ja", "Fredag klockan 19 istället", SV),
    ("Hi, I'd like to book a table for four on Monday at 7 pm", "Yes", "Friday at 7 pm instead", EN),
])
def test_a_new_date_from_the_caller_gets_its_own_next_day(opening, yes, other_day, lang):
    """The next day is offered once per day the caller asks for: Monday is
    full, Tuesday is offered, the caller asks for Friday instead, Friday is
    full -> the agent offers Saturday, not a callback."""
    call = Call()
    assert _use(call.say(opening)) == ("hold_table", {"date": "2026-11-09", "time": "19:00", "partySize": 4})
    full = call.tool({"held": False, "reason": "day_full", "time": "19:00", "before": [], "after": [], "nearby": [],
                      "dayFull": True})
    assert full.text == agent_model.CONFIRMATIONS["get_availability_full"][lang]
    assert _use(call.say(yes)) == ("get_availability", {"date": "2026-11-10", "partySize": 4, "time": "19:00"})
    offer = call.tool({"times": ["18:00", "20:00"], "requestedTime": "19:00", "before": ["18:00"],
                       "after": ["20:00"], "dayFull": False})
    assert offer.text == COUNTER_OFFERS["near_time"][lang].format(
        time="19:00", times="18:00" + (" eller " if lang == SV else " or ") + "20:00")
    assert _use(call.say(other_day)) == ("hold_table", {"date": "2026-11-06", "time": "19:00", "partySize": 4})
    friday = call.tool({"held": False, "reason": "day_full", "time": "19:00", "before": [], "after": [],
                        "nearby": [], "dayFull": True})
    assert friday.text == agent_model.CONFIRMATIONS["get_availability_full"][lang]   # not next_day_full
    assert _use(call.say(yes)) == ("get_availability", {"date": "2026-11-07", "partySize": 4, "time": "19:00"})
    saturday_full = call.tool({"times": [], "dayFull": True})
    assert saturday_full.text == COUNTER_OFFERS["next_day_full"][lang]               # then a callback, no third day
    assert _use(call.say(yes))[0] == "request_callback"


# 4. A nearby table at the requested time (location with a floor plan) --------------------

@pytest.mark.parametrize("opening, yes, name, lang", OPENINGS)
@pytest.mark.parametrize("kind", sorted(NEARBY_TABLE))
def test_nearby_table_is_offered_first_and_held_on_yes(opening, yes, name, lang, kind):
    call = Call()
    call.say(opening)
    offer = call.tool({"held": False, "reason": "time_taken", "time": "19:00", "before": ["18:30"], "after": ["19:30"],
                       "nearby": [{"tableIds": ["t7", "t8"], "seats": 4, "kind": kind}]})
    assert offer.text == COUNTER_OFFERS["nearby_table"][lang].format(time="19:00", table=NEARBY_TABLE[kind][lang])
    assert "18:30" not in offer.text                   # the nearby table is offered on its own first
    assert _use(call.say(yes)) == ("hold_table", {"date": "2026-11-03", "time": "19:00", "partySize": 4,
                                                 "tableIds": ["t7", "t8"]})
    held = call.tool({"holdId": "h3", "date": "2026-11-03", "time": "19:00", "partySize": 4,
                      "tableIds": ["t7", "t8"]})
    assert "19:00" in held.text
    call.say(yes)
    assert _use(call.say(name))[1]["holdId"] == "h3"


@pytest.mark.parametrize("opening, no, lang", [("Jag vill boka ett bord för fyra imorgon klockan 19", "Nej", SV),
                                               ("I want to book a table for four tomorrow at 7 pm", "No", EN)])
def test_nearby_table_declined_falls_back_to_the_closest_times(opening, no, lang):
    call = Call()
    call.say(opening)
    call.tool({"held": False, "reason": "time_taken", "time": "19:00", "before": ["18:30"], "after": ["19:30"],
               "nearby": [{"tableIds": ["t7", "t8"], "seats": 4, "kind": "adjacent_pair"}]})
    declined = call.say(no)
    assert declined.text == COUNTER_OFFERS["time_taken"][lang].format(
        time="19:00", times="18:30" + (" eller " if lang == SV else " or ") + "19:30")
    assert _use(call.say("18.30" if lang == SV else "6:30 pm"))[1]["time"] == "18:30"


@pytest.mark.parametrize("opening, lang", OPENING)
def test_without_a_floor_plan_only_times_are_offered(opening, lang):
    """A location without a published floor plan: the tool returns no
    `nearby` entries and the agent offers times only."""
    call = Call()
    call.say(opening)
    offer = call.tool({"held": False, "reason": "time_taken", "time": "19:00", "before": ["18:30"], "after": ["19:30"]})
    assert offer.text == COUNTER_OFFERS["time_taken"][lang].format(
        time="19:00", times="18:30" + (" eller " if lang == SV else " or ") + "19:30")


# 5. The ranking itself -------------------------------------------------------------------

@pytest.mark.parametrize("requested, before, after, expected", [
    ("19:00", ["18:30", "18:00", "17:00"], ["19:30", "20:00", "21:00"], ["18:00", "18:30", "19:30"]),  # tie: earlier
    ("19:00", ["18:00"], ["19:30", "20:00", "20:30"], ["18:00", "19:30", "20:00"]),
    ("19:00", [], ["21:00"], ["21:00"]),
    ("19:00", [], [], []),
])
def test_nearest_times_rank_by_distance_then_speak_in_clock_order(requested, before, after, expected):
    assert nearest_times(requested, before, after) == expected


def test_every_counter_offer_sentence_exists_in_swedish_and_english():
    for key, (sv, en) in COUNTER_OFFERS.items():
        assert sv and en and sv != en, key
    for kind, (sv, en) in NEARBY_TABLE.items():
        assert sv and en and sv != en, kind
    assert agent_model.MAX_OFFERS == 3


def test_scripts_are_deterministic():
    def run():
        call = Call()
        call.say("Jag vill boka ett bord för fyra imorgon klockan 19")
        call.tool({"held": False, "time": "19:00", "before": ["18:30"], "after": ["19:30"], "nearby": []})
        call.say("19.30")
        return [m["content"] for m in call.messages if m["role"] == "assistant"]
    assert run() == run()

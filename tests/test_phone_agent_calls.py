"""Scripted calls (ADR-L test plan, "scripted call simulator"): text in, text
out through the real phone-agent-call Lambda (webhooks, the turn endpoint,
the agent loop with SimulatedAgentModel, the real tools and offers() over
fake answers of the existing Lambdas), each in Swedish and English, each
asserting that the art. 50 disclosure is the first agent turn."""

import json

import pytest

from shared.features import phone_agent as pa
from shared.features.phone_agent import offers as of
from shared.features.phone_agent.calls import normalized_text as calls_normalized
from test_phone_agent_support import (CALLER, L, LAYOUT, LOC_A, TENANT_A, call, enable, env, set_feature,
                                      webhook)

assert env
SV, EN = "sv", "en"
NUMBER = "+46100000001"
KEY = "secret-key"
TOMORROW, DAY_AFTER = "2026-11-03", "2026-11-04"
LINES = {
    SV: {"book": "Hej, jag vill boka ett bord för fyra imorgon klockan 19", "yes": "Ja", "no": "Nej",
         "name": "Jag heter Anna", "at1930": "Klockan 19.30", "human": "Kan jag prata med en människa",
         "menu": "Har ni något vegetariskt på menyn?", "unknown": "Kan jag ta med min hund?",
         "cancel": "Jag vill avboka min bokning", "move": "Jag vill flytta min bokning till imorgon klockan 20",
         "booked": "bokad"},
    EN: {"book": "Hi, I'd like to book a table for four tomorrow at 7 pm", "yes": "Yes", "no": "No",
         "name": "My name is Anna", "at1930": "At 7:30 pm", "human": "Can I speak to a person",
         "menu": "Do you have anything vegetarian on the menu?", "unknown": "Can I bring my dog?",
         "cancel": "I want to cancel my booking", "move": "I'd like to move my booking to tomorrow at 8 pm",
         "booked": "booked"},
}
BOTH = pytest.mark.parametrize("lang", [SV, EN])


class Phone:
    """One call: the provider's webhooks and turn requests."""

    def __init__(self, e, ref="ref-1", caller=CALLER):
        self.e, self.ref = e, ref
        status, body = webhook(e, "voice", [{"kind": "call_started", "call_ref": ref, "from": caller, "to": NUMBER}])
        assert status == 200
        self.ids = body.get("dynamicVariables") or {}
        self.call_id = self.ids.get("callId") or pa.call_id_for(ref, pa.now())

    def row(self):
        return self.e.live.calls.get_call(TENANT_A, self.call_id)

    def event(self, kind, **data):
        return webhook(self.e, "voice", [{"kind": kind, "call_ref": self.ref, **self.ids, **data}])

    def say(self, text):
        body = {"model": "booqy", "messages": [{"role": "user", "content": text}], "booqy": self.ids}
        out = self.e.live.handler({"routeKey": "$default", "rawPath": "/v1/chat/completions",
                                   "headers": {"x-booqy-agent-key": KEY},
                                   "requestContext": {"http": {"method": "POST"}}, "body": json.dumps(body)})
        assert out["statusCode"] == 200, out
        chunk = json.loads(out["body"].split("\n\n")[0].removeprefix("data: "))
        return chunk["choices"][0]["delta"]["content"]

    def hang_up(self, minutes=2):
        self.e.clock.value = self.e.clock.value.replace(minute=self.e.clock.value.minute + minutes)
        return self.event("call_ended")

    def turns(self):
        return pa.query_all(pa.tenant_pk(TENANT_A), self.row()["SK"] + "#T#")


@pytest.fixture
def live(env, monkeypatch):
    monkeypatch.setattr(env.live, "_endpoint_key", lambda: KEY)
    enable(env)
    return env


def ended(e):
    return [d for t, d in e.published if t == "PhoneCall.Ended"]


def assert_disclosure_first(phone):
    first = phone.turns()[0]
    assert first["role"] == "agent" and ("AI-assistent" in first["text"])
    assert phone.row()["disclosure"]["text"] == first["text"]


# --- booking and counter-offers -------------------------------------------------------------------

@BOTH
def test_book_inside_rules(live, lang):
    live.availability.set(TOMORROW, ["18:00", "19:00", "20:00"])
    phone = Phone(live)
    phone.event("transcript", role="agent", final=True, text=phone.row()["disclosure"]["text"])
    assert phone.row()["disclosure"]["playedAt"]                         # evidence that it played
    assert "19:00" in phone.say(LINES[lang]["book"])
    phone.say(LINES[lang]["yes"])
    assert LINES[lang]["booked"] in phone.say(LINES[lang]["name"])
    (booking,) = live.invoke.reservations.values()
    assert booking["source"] == "phone" and booking["phone"] == CALLER and booking["tableIds"] == ["t4"]
    phone.hang_up()
    row = phone.row()
    assert (row["status"], row["outcome"], row["counterOffer"], int(row["billedMinutes"])) == \
        ("processed", "booked", "none", 2)
    assert ended(live)[0]["counterOffer"] == "none" and ended(live)[0]["reservationId"] == "r1"
    assert_disclosure_first(phone)
    _, badge = call(live, "GET", L + "/reservations/{reservationId}/call", reservationId="r1")
    assert badge["callId"] == phone.call_id


@BOTH
def test_time_taken_offers_three_closest_and_books_one(live, lang):
    live.availability.set(TOMORROW, ["17:00", "17:30", "18:30", "19:30", "20:30", "21:00"])
    live.invoke.layout = None   # no floor plan: times only
    phone = Phone(live)
    reply = phone.say(LINES[lang]["book"])
    assert "17:30, 18:30" in reply and "19:30" in reply and "21:00" not in reply
    assert "19:30" in phone.say(LINES[lang]["at1930"])
    phone.say(LINES[lang]["yes"])
    phone.say(LINES[lang]["name"])
    phone.hang_up()
    assert phone.row()["counterOffer"] == "other_time"
    assert list(live.invoke.reservations.values())[0]["startTime"] == "19:30"


@BOTH
def test_full_day_next_day_once_then_callback(live, lang):
    phone = Phone(live)
    phone.say(LINES[lang]["book"])                                       # tomorrow is full
    assert "18:00" not in phone.say(LINES[lang]["yes"])                  # day after: also full
    phone.say(LINES[lang]["yes"])                                        # -> callback
    phone.hang_up()
    row = phone.row()
    assert row["outcome"] == "callback" and row["counterOffer"] == "next_day"
    _, body = call(live, "GET", L + "/callbacks")
    assert len(body["callbacks"]) == 1


@BOTH
def test_full_day_next_day_free_is_booked(live, lang):
    live.availability.set(DAY_AFTER, ["19:00", "20:00"])
    phone = Phone(live)
    phone.say(LINES[lang]["book"])
    assert "19:00" in phone.say(LINES[lang]["yes"])
    phone.say(LINES[lang]["at1930"].replace("19.30", "19").replace("7:30", "7"))
    phone.say(LINES[lang]["yes"])
    phone.say(LINES[lang]["name"])
    phone.hang_up()
    assert phone.row()["counterOffer"] == "next_day"
    assert list(live.invoke.reservations.values())[0]["date"] == DAY_AFTER


@BOTH
def test_nearby_pair_offered_first_and_held_on_yes(live, lang):
    # 19:00 has no online option for four; t2a + t2b (1.0 m apart) are free, so is t2c (1.5 m away).
    live.availability.set(TOMORROW, ["18:00", "20:00"], staff={"19:00": {"t2a": 2, "t2b": 2, "t2c": 2}})
    phone = Phone(live)
    reply = phone.say(LINES[lang]["book"])
    assert ("bredvid varandra" if lang == SV else "next to each other") in reply
    phone.say(LINES[lang]["yes"])                                        # holds exactly t2a + t2b
    phone.say(LINES[lang]["yes"])
    phone.say(LINES[lang]["name"])
    phone.hang_up()
    booking = list(live.invoke.reservations.values())[0]
    assert (booking["startTime"], booking["tableIds"]) == ("19:00", ["t2a", "t2b"])
    assert phone.row()["counterOffer"] == "nearby_table"


@BOTH
def test_nearby_declined_falls_back_to_times(live, lang):
    live.availability.set(TOMORROW, ["18:00", "20:00"], staff={"19:00": {"t2a": 2, "t2b": 2}})
    phone = Phone(live)
    phone.say(LINES[lang]["book"])
    reply = phone.say(LINES[lang]["no"])
    assert "18:00" in reply and "20:00" in reply


@BOTH
def test_nearby_off_gives_times_only(live, lang):
    enable(live, version=1, counterOffers={"nearbyTables": False})
    live.availability.set(TOMORROW, ["18:00", "20:00"], staff={"19:00": {"t2a": 2, "t2b": 2}})
    reply = Phone(live).say(LINES[lang]["book"])
    assert "18:00" in reply and ("bredvid" not in reply and "next to" not in reply)


@BOTH
def test_table_taken_before_booking_is_re_offered(live, lang):
    live.availability.set(TOMORROW, ["18:30", "19:00", "19:30"])
    live.invoke.layout = None
    phone = Phone(live)
    phone.say(LINES[lang]["book"])
    phone.say(LINES[lang]["yes"])
    live.invoke.taken.add((TOMORROW, "19:00"))
    live.availability.set(TOMORROW, ["18:30", "19:30"])
    reply = phone.say(LINES[lang]["name"])
    assert "18:30" in reply and "19:30" in reply and not live.invoke.reservations


@BOTH
def test_guarantee_size_party_gets_a_callback(live, lang, monkeypatch):
    from shared import guarantee
    monkeypatch.setattr(guarantee, "required", lambda location, tenant_row, party: party >= 4)
    live.availability.set(TOMORROW, ["19:00"])
    phone = Phone(live)
    phone.say(LINES[lang]["book"])
    phone.say(LINES[lang]["yes"])
    phone.say(LINES[lang]["name"])                                       # refused: guarantee -> callback offer
    phone.say(LINES[lang]["yes"])
    phone.hang_up()
    assert phone.row()["outcome"] == "callback" and not live.invoke.reservations
    assert not any(c[0] == "send_guarantee_link" for c in live.lam.calls)     # no card data, no link in v1


# --- move, cancel, info, callback, transfer ---------------------------------------------------------

@BOTH
def test_cancel_own_booking(live, lang):
    live.invoke.listed[TOMORROW] = [{"reservationId": "r9", "customerPhone": CALLER, "status": "reserved",
                                     "date": TOMORROW, "startTime": "19:00", "partySize": 2},
                                    {"reservationId": "r8", "customerPhone": "+46700000000", "status": "reserved",
                                     "date": TOMORROW, "startTime": "19:00"}]
    phone = Phone(live)
    assert "19:00" in phone.say(LINES[lang]["cancel"])
    phone.say(LINES[lang]["yes"])
    phone.hang_up()
    assert phone.row()["outcome"] == "cancelled" and phone.row()["reservationId"] == "r9"
    (fn, route, event), = [c for c in live.invoke.calls if c[0] == "mark-arrived"]
    assert event["pathParameters"]["reservationId"] == "r9" and json.loads(event["body"])["status"] == "cancelled"


@BOTH
def test_move_own_booking(live, lang):
    live.invoke.listed[TOMORROW] = [{"reservationId": "r9", "customerPhone": CALLER, "status": "reserved",
                                     "date": TOMORROW, "startTime": "19:00"}]
    phone = Phone(live)
    phone.say(LINES[lang]["move"])
    phone.hang_up()
    assert phone.row()["outcome"] == "moved"
    (_, route, event), = [c for c in live.invoke.calls if c[0] == "mark-arrived"]
    assert route.startswith("PATCH") and json.loads(event["body"]) == {"date": TOMORROW, "startTime": "20:00"}


def test_another_callers_booking_is_never_touched(live):
    live.invoke.listed[TOMORROW] = [{"reservationId": "r8", "customerPhone": "+46700000000", "status": "reserved",
                                     "date": TOMORROW, "startTime": "19:00"}]
    phone = Phone(live)
    reply = phone.say(LINES[SV]["cancel"])
    assert "hittar tyvärr ingen" in reply and not [c for c in live.invoke.calls if c[0] == "mark-arrived"]


@BOTH
def test_menu_question(live, lang):
    phone = Phone(live)
    assert "lasagne" in phone.say(LINES[lang]["menu"]).lower()
    phone.hang_up()
    assert phone.row()["outcome"] == "info"


@BOTH
def test_unknown_question_becomes_a_callback(live, lang):
    phone = Phone(live)
    phone.say(LINES[lang]["unknown"])
    phone.say(LINES[lang]["yes"])
    phone.hang_up()
    assert phone.row()["outcome"] == "callback"
    assert call(live, "GET", L + "/callbacks")[1]["callbacks"][0]["phone"] == CALLER


@BOTH
def test_speak_to_a_human_is_transferred_and_minutes_stop(live, lang):
    phone = Phone(live)
    live.clock.value = live.clock.value.replace(minute=1)
    phone.say(LINES[lang]["human"])
    assert ("transfer", phone.ref, "+46812345678", 25) in live.voice.calls
    row = phone.row()
    assert row["status"] == "processed" and row["outcome"] == "transferred" and int(row["billedMinutes"]) == 1


@BOTH
def test_transfer_unanswered_resumes_and_offers_callback(live, lang):
    live.voice.transfer_answered = False
    phone = Phone(live)
    reply = phone.say(LINES[lang]["human"])
    assert ("ringer upp" if lang == SV else "call you back") in reply
    phone.say(LINES[lang]["yes"])
    phone.hang_up()
    assert phone.row()["outcome"] == "callback"


# --- guards ---------------------------------------------------------------------------------------

def _refused(e, reason):
    phone = Phone(e)
    row = phone.row()
    assert (row["status"], row["outcome"], row["refusedReason"], int(row["billedMinutes"])) == \
        ("processed", "refused", reason, 0)
    assert ("say", phone.ref, "Tyvärr kan vi inte ta emot bokningar per telefon just nu, besök vår hemsida.") in \
        e.voice.calls
    assert ("PhoneCall.Refused", {"tenantId": TENANT_A, "locationId": LOC_A, "callId": phone.call_id,
                                  "reason": reason}) in e.published


def test_entitlement_off_refuses(live):
    set_feature(live, TENANT_A, False)
    _refused(live, "entitlement")


def test_kill_switch_refuses(live):
    call(live, "POST", L + "/kill-switch", body={"on": True, "version": 1})
    _refused(live, "kill_switch")


def test_outside_agent_hours_refuses(live):
    enable(live, version=1, mode="after_hours", agentHours={"mon": [{"from": "22:00", "to": "23:00"}]})
    _refused(live, "hours")


def test_cap_reached_mid_month_refuses_and_warns(live):
    call(live, "PUT", "/tenant/phone-agent/config", body={"agentName": "Sara", "tone": "friendly",
                                                          "languages": ["sv"], "recording": {"enabled": False},
                                                          "minutesCapPerMonth": 5, "version": 0})
    phone = Phone(live, "ref-cap")
    phone.hang_up(minutes=4)                                              # 4 of 5 minutes: 80 % warning
    assert [c["notice"]["kind"] for c in live.lam.calls] == ["phone-agent.cap-warning"]
    phone2 = Phone(live, "ref-cap-2")
    phone2.hang_up(minutes=1)
    assert any(t == "PhoneMinutes.CapReached" for t, _ in live.published)
    _refused_ref = Phone(live, "ref-cap-3").row()
    assert _refused_ref["refusedReason"] == "cap"
    assert [c["notice"]["kind"] for c in live.lam.calls] == ["phone-agent.cap-warning"]   # warned once


def test_webhooks_also_answer_on_the_function_url(live):
    body = json.dumps({"events": [{"kind": "call_started", "call_ref": "fu-1", "from": CALLER, "to": NUMBER}]})
    out = live.live.handler({"routeKey": "$default", "rawPath": "/public/phone-agent/voice/webhook",
                             "headers": {"x-sim-signature": "ok"}, "requestContext": {"http": {"method": "POST"}},
                             "body": body})
    assert out["statusCode"] == 200 and json.loads(out["body"])["dynamicVariables"]["tenantId"] == TENANT_A


def test_unknown_number_and_bad_signatures(live):
    assert webhook(live, "voice", [{"kind": "call_started", "call_ref": "x", "to": "+46999"}])[0] == 404
    assert webhook(live, "voice", [], auth=False)[0] == 404
    assert webhook(live, "telephony", [], auth=False)[0] == 404


# --- disclosure, lifecycle, metering ---------------------------------------------------------------

def test_disclosure_repeated_when_interrupted_and_in_a_new_language(live):
    phone = Phone(live)
    phone.event("interrupted")
    phone.event("language_detected", lang="en")
    phone.event("language_detected", lang="en")                         # once per language
    row = phone.row()
    assert row["disclosure"]["repeats"] == 1 and row["disclosure"]["languages"] == ["sv", "en"]
    said = [c[2] for c in live.voice.calls if c[0] == "say"]
    assert said[1] == "Jag är en AI-assistent för Location loc-a-1."
    assert said[2] == "I am an AI assistant for Location loc-a-1." and len(said) == 3
    texts = [t["text"] for t in phone.turns()]
    assert "AI-assistent" in texts[0] and texts[1:] == said[1:]


def test_disclosure_evidence_is_the_providers_first_final_agent_transcript(live):
    phone = Phone(live)
    greeting = phone.row()["disclosure"]["text"]
    phone.event("transcript", role="user", final=True, text="Hallå?")                 # the caller: no evidence
    phone.event("transcript", role="agent", final=False, text=greeting[:12])         # partial: not yet
    row = phone.row()
    assert row["status"] == "answered" and row["disclosure"]["playedAt"] is None
    spoken = "  " + greeting.upper().replace(",", "").replace(".", " .") + " "        # the provider's rendering
    phone.event("transcript", role="agent", final=True, text=spoken)
    row = phone.row()
    assert row["status"] == "disclosed" and row["disclosure"]["playedAt"] == pa.iso(pa.now())
    assert row["disclosure"]["spoken"] == spoken and row["disclosure"]["verbatim"] is True
    phone.event("transcript", role="agent", final=True, text="Vilken dag vill ni komma?")   # later turns
    assert phone.row()["disclosure"]["spoken"] == spoken
    _, detail = call(live, "GET", L + "/calls/{callId}", callId=phone.call_id)
    assert detail["disclosure"]["spoken"] == spoken and detail["disclosure"]["verbatim"] is True


def test_disclosure_evidence_keeps_a_diverging_provider_text(live):
    phone = Phone(live)
    phone.event("transcript", role="agent", final=True, text="Hej, du pratar med Sara.")
    row = phone.row()
    assert row["status"] == "disclosed" and row["disclosure"]["playedAt"]
    assert row["disclosure"]["spoken"] == "Hej, du pratar med Sara." and row["disclosure"]["verbatim"] is False
    assert "AI-assistent" in row["disclosure"]["text"]                              # what Booqy sent is kept
    assert calls_normalized("Hej, du pratar med Sara!") == calls_normalized("  hej  du PRATAR med sara ")


def test_two_end_webhooks_meter_once(live):
    phone = Phone(live)
    phone.hang_up(minutes=3)
    phone.event("call_ended")
    webhook(live, "telephony", [{"kind": "ended", "call_ref": phone.ref, "to": NUMBER}])
    assert len(ended(live)) == 1
    assert pa.usage(TENANT_A, "2026-11-24")["minutes"] == 3


def test_max_call_length_ends_and_processes_once(live):
    phone = Phone(live)
    live.clock.value = live.clock.value.replace(minute=9)              # 480 s max
    reply = phone.say("Hej")
    assert "för långt" in reply and phone.row()["status"] == "processed"
    jobs = __import__("test_phone_agent_support").load("phone-agent-jobs")
    assert jobs.sweep(live.clock.value)["processed"] == 0                # nothing left for the sweep


def test_sweep_ends_a_stuck_call(live):
    phone = Phone(live)
    jobs = __import__("test_phone_agent_support").load("phone-agent-jobs")
    jobs._clients["voice"] = live.voice
    assert jobs.sweep(live.clock.value.replace(minute=10)) == {"ended": 1, "processed": 1}
    assert phone.row()["status"] == "processed" and ("end", phone.ref, "max_length") in live.voice.calls


def test_turn_endpoint_refuses_wrong_key_and_unknown_calls(live):
    phone = Phone(live)
    base = {"routeKey": "$default", "rawPath": "/v1/chat/completions", "requestContext": {"http": {"method": "POST"}}}
    body = json.dumps({"messages": [{"role": "user", "content": "Hej"}], "booqy": phone.ids})
    assert live.live.handler({**base, "headers": {"x-booqy-agent-key": "nope"}, "body": body})["statusCode"] == 401
    other = json.dumps({"messages": [], "booqy": {"tenantId": "01bbbbbbbbbbbbbbbbbbbbbbbb", "callId": phone.call_id}})
    assert live.live.handler({**base, "headers": {"x-booqy-agent-key": KEY}, "body": other})["statusCode"] == 404


# --- offers() on the floor plan ---------------------------------------------------------------------

def test_edge_gap_and_pairs():
    t = {e["elementId"]: e for e in LAYOUT["elements"]}
    assert of.edge_gap(t["t2a"], t["t2b"]) == pytest.approx(1.0)
    assert of.edge_gap(t["t2b"], t["t2c"]) == pytest.approx(1.5)
    staff = {"slots": [{"startTime": "19:00",
                        "tables": [{"tableId": "t2b", "seats": 2}, {"tableId": "t2c", "seats": 2}]}]}
    assert of.nearby({"slots": []}, staff, LAYOUT, "19:00", 4) == []    # 1.5 m apart: not a pair


def test_larger_and_phone_only_tables():
    guest = {"slots": [{"startTime": "19:00", "tables": [], "options": [],
                        "unavailable": [{"tableId": "t6", "reason": "too_large"},
                                        {"tableId": "t4p", "reason": "phone"}]}]}
    staff = {"slots": [{"startTime": "19:00",
                        "tables": [{"tableId": "t6", "seats": 6}, {"tableId": "t4p", "seats": 4}]}]}
    kinds = [n["kind"] for n in of.nearby(guest, staff, LAYOUT, "19:00", 4)]
    assert kinds == ["larger", "phone_only"]
    assert [n["kind"] for n in of.nearby(guest, staff, LAYOUT, "19:00", 4, phone_only_tables=False)] == ["larger"]
    assert of.nearby(guest, staff, {}, "19:00", 4) == []                  # no floor plan
    assert of.offers(guest, staff, LAYOUT, "19:00", 4)["reason"] == "time_taken"

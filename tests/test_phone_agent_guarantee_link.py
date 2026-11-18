"""The phone agent's side of card guarantee bookings (owner 2026-11-18):
availability and hold say the card link will be texted, create_reservation
asks the manual route for a pending booking with a Stripe link and texts it
to the caller ID, the fixed sentence; hidden or foreign numbers and no SMS
sender get the callback; phoneGuarantee "skip" books reserved; the
location switch is validated and returned. The route and webhook side:
tests/test_phone_guarantee_link.py."""

import json
import re

import pytest

from shared import guarantee as shared_guarantee
from shared.features import phone_agent as pa
from shared.features.phone_agent import copy
from shared.features.phone_agent.tools import Executor
from test_phone_agent_calls import LINES, Phone, live  # noqa: F401 (fixture)
from test_phone_agent_support import CALLER, L, LOC_A as PA_LOC, TENANT_A, TODAY, call, enable, env  # noqa: F401

DASHES = re.compile("[\u2013\u2014]|&[nm]dash;")
CHECKOUT_URL = "https://checkout.stripe.com/c/pay/cs_test_1"

# --- the agent's tools -------------------------------------------------------------------------------

TOMORROW = "2026-11-03"


class PendingInvoke:
    """The phone agent's Lambdas (test_phone_agent_support.FakeInvoker), with
    the manual route answering a guarantee link as the real one does."""

    def __init__(self, inner):
        self.inner = inner

    def __call__(self, fn, event):
        status, body = self.inner(fn, event)
        request = json.loads(event.get("body") or "null") or {}
        if fn == "create-pending-reservation" and status == 201 and request.get("guarantee") == "link":
            body = {**body, "status": "pending", "date": request["date"], "startTime": request["startTime"],
                    "partySize": request["partySize"], "language": request["language"],
                    "guarantee": {"noShowFee": 40000, "lateCancelFee": 0, "cancelCutoffHours": 0},
                    "guaranteeLink": {"url": CHECKOUT_URL, "minutes": 30, "expiresAt": "2026-11-03T18:32:00Z"}}
        return status, body


class Texts:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, **kwargs):
        if self.fail:
            raise RuntimeError("sns down")
        self.sent.append(kwargs)


def _ex(e, *, caller=CALLER, loc=None, sms=None, state=None, language="sv"):
    return Executor(tenant_id=TENANT_A, tenant_row={"tenantId": TENANT_A, "name": "Bistro"},
                    location={"locationId": PA_LOC, "name": "Bistro"}, loc_config=loc or {},
                    invoke=PendingInvoke(e.invoke), state={} if state is None else state, caller_phone=caller,
                    today=TODAY, send_sms=sms, language=language)


@pytest.fixture
def needs_card(env, monkeypatch):
    monkeypatch.setattr(shared_guarantee, "required", lambda location, tenant_row, party: party >= 4)
    env.availability.set(TOMORROW, ["19:00"])
    return env


def _booked(e):
    return list(e.invoke.reservations.values())


def test_guarantee_party_is_held_pending_and_the_caller_gets_the_link(needs_card):
    texts = Texts()
    ex = _ex(needs_card, sms=texts)
    times = ex.run("get_availability", {"date": TOMORROW, "partySize": 4, "time": "19:00"})
    assert times["times"] == ["19:00"] and times["guaranteeRequired"] is True and "text message" in times["say"]
    assert "code" not in times
    hold = ex.run("hold_table", {"date": TOMORROW, "time": "19:00", "partySize": 4})
    assert hold["holdId"] and hold["guaranteeRequired"] is True

    done = ex.run("create_reservation", {"holdId": hold["holdId"], "name": "Anna", "confirmed": True})

    assert done["guaranteeLinkSent"] is True and done["booked"] is False and done["pending"] is True
    assert done["sentence"] == copy.GUARANTEE_LINK_SENT["sv"] and done["heldMinutes"] == 30
    (booking,) = _booked(needs_card)
    assert booking["guarantee"] == "link" and booking["phone"] == CALLER and booking["source"] == "phone"
    (sms,) = texts.sent
    assert sms["to"] == CALLER and sms["tenant_id"] == TENANT_A and sms["location_id"] == PA_LOC
    assert CHECKOUT_URL in sms["text"] and "4 personer" in sms["text"] and "400 kr" in sms["text"]
    assert ex.state["outcome"] == "booked" and ex.state["guaranteePending"] is True
    again = ex.run("create_reservation", {"holdId": hold["holdId"], "name": "Anna", "confirmed": True})
    assert again["pending"] is True and len(_booked(needs_card)) == 1 and len(texts.sent) == 1
    resent = ex.run("send_guarantee_link", {"reservationId": done["reservationId"]})
    assert resent["sent"] is True and len(texts.sent) == 2 and texts.sent[1]["text"] == sms["text"]
    assert ex.run("send_guarantee_link", {"reservationId": done["reservationId"]})["code"] == "already_resent"
    assert ex.run("send_guarantee_link", {"reservationId": "other"})["code"] == "guarantee_not_available"


def test_english_call_gets_the_english_sentence(needs_card):
    ex = _ex(needs_card, sms=Texts(), language="en")
    hold = ex.run("hold_table", {"date": TOMORROW, "time": "19:00", "partySize": 4})["holdId"]
    done = ex.run("create_reservation", {"holdId": hold, "name": "Anna", "confirmed": True})
    assert done["sentence"] == copy.GUARANTEE_LINK_SENT["en"] and _booked(needs_card)[0]["language"] == "en"


def test_skip_books_the_guarantee_party_reserved_without_a_link(needs_card):
    texts = Texts()
    ex = _ex(needs_card, sms=texts, loc={"phoneGuarantee": "skip"})
    hold = ex.run("hold_table", {"date": TOMORROW, "time": "19:00", "partySize": 4})
    assert "guaranteeRequired" not in hold
    done = ex.run("create_reservation", {"holdId": hold["holdId"], "name": "Anna", "confirmed": True})
    assert done["booked"] is True and "guaranteeLinkSent" not in done
    assert "guarantee" not in _booked(needs_card)[0] and texts.sent == []


@pytest.mark.parametrize("caller", [None, "anonymous", "+12025550123"])
def test_hidden_or_foreign_number_gets_the_callback(needs_card, caller):
    texts = Texts()
    ex = _ex(needs_card, caller=caller, sms=texts)
    held = ex.run("hold_table", {"date": TOMORROW, "time": "19:00", "partySize": 4})
    assert held["code"] == "guaranteeRequired" and "request_callback" in held["say"] and "holdId" not in held
    assert ex.state["needsCallback"] == "guarantee" and texts.sent == [] and not _booked(needs_card)


def test_without_an_sms_sender_the_guarantee_party_gets_the_callback(needs_card):
    held = _ex(needs_card, sms=None).run("hold_table", {"date": TOMORROW, "time": "19:00", "partySize": 4})
    assert held["code"] == "guaranteeRequired"


def test_no_booking_and_no_text_without_a_clear_yes(needs_card):
    texts = Texts()
    ex = _ex(needs_card, sms=texts)
    hold = ex.run("hold_table", {"date": TOMORROW, "time": "19:00", "partySize": 4})["holdId"]
    assert ex.run("create_reservation", {"holdId": hold, "name": "Anna", "confirmed": False})["code"] == \
        "notConfirmed"
    assert ex.run("create_reservation", {"holdId": hold, "name": "Anna"})["code"] == "notConfirmed"
    assert not _booked(needs_card) and texts.sent == []


def test_read_back_first_still_applies_to_a_guarantee_booking(needs_card):
    texts = Texts()
    state = {"turn": 1}
    ex = _ex(needs_card, sms=texts, state=state)
    hold = ex.run("hold_table", {"date": TOMORROW, "time": "19:00", "partySize": 4})["holdId"]
    assert ex.run("create_reservation", {"holdId": hold, "name": "Anna", "confirmed": True})["code"] == \
        "readBackFirst"
    assert not _booked(needs_card) and texts.sent == []


def test_a_failed_text_offers_the_callback_and_never_books_twice(needs_card):
    ex = _ex(needs_card, sms=Texts(fail=True))
    hold = ex.run("hold_table", {"date": TOMORROW, "time": "19:00", "partySize": 4})["holdId"]
    failed = ex.run("create_reservation", {"holdId": hold, "name": "Anna", "confirmed": True})
    assert failed["code"] == "guaranteeLinkFailed" and "request_callback" in failed["say"]
    assert ex.run("create_reservation", {"holdId": hold, "name": "Anna", "confirmed": True})["code"] == \
        "guaranteeLinkFailed"
    assert len(_booked(needs_card)) == 1 and ex.state["needsCallback"] == "guarantee"
    (_, route, event), = [c for c in needs_card.invoke.calls if c[0] == "mark-arrived"]   # tables freed at once
    assert route == "POST /locations/{locationId}/reservations/{reservationId}/status"
    assert event["pathParameters"]["reservationId"] == "r1"
    assert json.loads(event["body"]) == {"status": "cancelled", "reason": "Kortlänken kunde inte skickas",
                                         "notifyGuest": False}


def _found(e, party, status="reserved"):
    e.invoke.listed[TOMORROW] = [{"reservationId": "r9", "customerPhone": CALLER, "status": status,
                                  "date": TOMORROW, "startTime": "19:00", "partySize": party}]


def _moves(e):
    return [c for c in e.invoke.calls if c[0] == "mark-arrived"]


def test_a_move_never_promises_a_link_and_a_party_growing_into_the_guarantee_gets_the_callback(needs_card):
    _found(needs_card, 2)
    texts = Texts()
    ex = _ex(needs_card, sms=texts)
    ex.run("find_reservations", {})
    times = ex.run("get_availability", {"date": TOMORROW, "partySize": 4, "time": "19:00"})
    assert "guaranteeRequired" not in times and "text message" not in str(times)
    hold = ex.run("hold_table", {"date": TOMORROW, "time": "19:00", "partySize": 4})
    assert "guaranteeRequired" not in hold

    moved = ex.run("move_reservation", {"reservationId": "r9", "date": TOMORROW, "time": "19:00",
                                        "partySize": 4, "confirmed": True})

    assert moved["ok"] is False and moved["code"] == "guaranteeRequired" and "request_callback" in moved["say"]
    assert ex.state["needsCallback"] == "guarantee" and not _moves(needs_card) and texts.sent == []


def test_a_guarantee_party_keeping_its_size_moves_as_before(needs_card):
    _found(needs_card, 6)
    ex = _ex(needs_card, sms=Texts())
    ex.run("find_reservations", {})
    moved = ex.run("move_reservation", {"reservationId": "r9", "date": TOMORROW, "time": "19:00",
                                        "partySize": 6, "confirmed": True})
    assert moved["moved"] is True and len(_moves(needs_card)) == 1
    assert "partySize" in json.loads(_moves(needs_card)[0][2]["body"])


def test_a_booking_waiting_for_its_card_keeps_its_party(needs_card):
    _found(needs_card, 6, status="pending")
    ex = _ex(needs_card, sms=Texts())
    assert ex.run("find_reservations", {})["reservations"][0]["pending"] is True
    moved = ex.run("move_reservation", {"reservationId": "r9", "date": TOMORROW, "time": "19:00",
                                        "partySize": 10, "confirmed": True})
    assert moved["code"] == "guaranteeRequired" and not _moves(needs_card)
    smaller = ex.run("move_reservation", {"reservationId": "r9", "date": TOMORROW, "time": "19:00",
                                          "partySize": 2, "confirmed": True})
    assert smaller["code"] == "guaranteeRequired" and not _moves(needs_card)
    assert ex.run("move_reservation", {"reservationId": "r9", "date": TOMORROW, "time": "19:00",
                                       "confirmed": True})["moved"] is True


def test_the_booking_just_made_pending_keeps_its_party_in_the_same_call(needs_card):
    ex = _ex(needs_card, sms=Texts())
    hold = ex.run("hold_table", {"date": TOMORROW, "time": "19:00", "partySize": 4})["holdId"]
    rid = ex.run("create_reservation", {"holdId": hold, "name": "Anna", "confirmed": True})["reservationId"]
    moved = ex.run("move_reservation", {"reservationId": rid, "date": TOMORROW, "time": "19:00",
                                        "partySize": 8, "confirmed": True})
    assert moved["code"] == "guaranteeRequired" and not _moves(needs_card)


def test_simulator_dry_run_answers_the_link_without_writing(needs_card):
    ex = Executor(tenant_id=TENANT_A, tenant_row={"tenantId": TENANT_A}, location={"locationId": PA_LOC},
                  loc_config={}, invoke=needs_card.invoke, state={}, dry_run=True, today=TODAY)
    hold = ex.run("hold_table", {"date": TOMORROW, "time": "19:00", "partySize": 4})["holdId"]
    done = ex.run("create_reservation", {"holdId": hold, "name": "Anna", "confirmed": True})
    assert done["guaranteeLinkSent"] is True and done["reservationId"] == f"sim-{hold}" and not _booked(needs_card)


def test_the_fixed_sentence_exists_in_every_priority_language_without_dashes():
    assert set(copy.REVIEWED) <= set(copy.GUARANTEE_LINK_SENT)
    for text in copy.GUARANTEE_LINK_SENT.values():
        assert not DASHES.search(text) and "!" not in text


def test_prompt_tells_the_read_back_about_the_link():
    from datetime import date
    from shared.features.phone_agent import agent
    prompt = agent.system_prompt("sv", "Sara", "Bistro", date(2026, 11, 17), "formal",
                                 last_day=date(2026, 11, 30))
    assert "guaranteeRequired" in prompt and "guaranteeLinkSent" in prompt and "text message" in prompt
    assert not DASHES.search(prompt)


# --- a whole call through phone-agent-call: the SMS goes out as a phone-agent notice ------------------

@pytest.mark.parametrize("lang", ["sv", "en"])
def test_live_call_books_pending_and_texts_the_link_through_notification(live, lang, monkeypatch):
    monkeypatch.setattr(shared_guarantee, "required", lambda location, tenant_row, party: party >= 4)
    live.live._clients["invoke"] = PendingInvoke(live.invoke)
    live.availability.set(TOMORROW, ["19:00"])
    phone = Phone(live)
    phone.say(LINES[lang]["book"])
    phone.say(LINES[lang]["yes"])
    reply = phone.say(LINES[lang]["name"])
    assert reply == copy.GUARANTEE_LINK_SENT[lang]
    (booking,) = live.invoke.reservations.values()
    assert booking["guarantee"] == "link" and booking["phone"] == CALLER
    (notice,) = [c["notice"] for c in live.lam.calls if (c.get("notice") or {}).get("kind") ==
                 "phone-agent.guarantee-link"]
    assert notice["channel"] == "sms" and notice["to"] == CALLER and CHECKOUT_URL in notice["text"]
    assert notice["tenantId"] == TENANT_A and notice["locationId"] == PA_LOC and notice["version"] == 1
    phone.hang_up()
    row = phone.row()
    assert row["outcome"] == "booked" and row["guaranteePending"] is True
    (ended,) = [d for t, d in live.published if t == "PhoneCall.Ended"]
    assert ended["guaranteePending"] is True


# --- the location switch -------------------------------------------------------------------------------

def test_phone_guarantee_setting_is_validated_returned_and_kept(env):
    assert enable(env)["phoneGuarantee"] == "link"                                    # default
    assert enable(env, version=1, phoneGuarantee="skip")["phoneGuarantee"] == "skip"
    assert enable(env, version=2)["phoneGuarantee"] == "skip"                         # absent: kept
    _, body = call(env, "GET", L)
    assert (body.get("location") or body)["phoneGuarantee"] == "skip"
    status, body = call(env, "PUT", L, body={"enabled": True, "mode": "always", "agentHours": {},
                                                "transferTo": "+46812345678", "version": 3,
                                                "phoneGuarantee": "card"})
    assert status == 400 and body["field"] == "phoneGuarantee"
    assert enable(env, version=3, phoneGuarantee=None)["phoneGuarantee"] == "link"


def test_validate_location_accepts_only_link_and_skip():
    base = {"enabled": False, "mode": "always"}
    assert pa.validate_location({**base, "phoneGuarantee": "skip"})["phoneGuarantee"] == "skip"
    assert "phoneGuarantee" not in pa.validate_location(base)
    with pytest.raises(pa.PhoneAgentError) as exc:
        pa.validate_location({**base, "phoneGuarantee": True})
    assert exc.value.code == "invalid_phone_guarantee"
    assert pa.phone_guarantee({}) == "link" and pa.phone_guarantee({"phoneGuarantee": "skip"}) == "skip"

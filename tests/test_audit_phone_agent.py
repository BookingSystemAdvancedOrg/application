"""Audit #24 and #61: the phone agent finds bookings only by the verified
caller number, and the L6 simulator has a per-session and monthly cap."""

import pytest

from shared.features import phone_agent as pa
from shared.features.phone_agent.tools import TOOLS, Executor
from test_phone_agent_support import (CALLER, L, LOC_A, TENANT_A, TODAY, call, enable, env)

assert env  # the fixture, imported for pytest

OTHER = "+46709998877"


def _executor(e, caller_phone):
    e.invoke.listed[TODAY.isoformat()] = [
        {"reservationId": "theirs", "customerPhone": OTHER, "status": "reserved", "startTime": "19:00",
         "partySize": 2},
        {"reservationId": "mine", "customerPhone": CALLER, "status": "reserved", "startTime": "18:00",
         "partySize": 4}]
    return Executor(tenant_id=TENANT_A, tenant_row={"tenantId": TENANT_A}, location={"locationId": LOC_A},
                    loc_config={}, invoke=e.invoke, state={}, caller_phone=caller_phone, today=TODAY)


@pytest.mark.parametrize("caller", [None, "", "anonymous", "Restricted", "070123"])
def test_hidden_number_cannot_look_up_another_guests_booking(env, caller):
    ex = _executor(env, caller)
    assert ex.run("find_reservations", {"phone": OTHER}) == {"reservations": [], "callerHidden": True}
    assert ex.run("cancel_reservation", {"reservationId": "theirs"}) == {"ok": False, "code": "not_found"}
    assert not [c for c in env.invoke.calls if c[0] == "mark-arrived"]


def test_verified_caller_finds_only_their_own_bookings(env):
    ex = _executor(env, CALLER)
    out = ex.run("find_reservations", {"phone": OTHER})
    assert [r["reservationId"] for r in out["reservations"]] == ["mine"]
    assert ex.run("cancel_reservation", {"reservationId": "theirs"}) == {"ok": False, "code": "not_found"}


def test_find_reservations_offers_no_phone_argument():
    spec = next(t for t in TOOLS if t["name"] == "find_reservations")
    assert "phone" not in spec["input_schema"]["properties"]


def test_simulator_session_is_capped(env, monkeypatch):
    enable(env)
    monkeypatch.setattr(pa, "SIM_TURNS_MAX", 2)
    for n in range(2):
        assert call(env, "POST", L + "/simulate", body={"sessionId": "sess-cap1", "text": f"Hej {n}"})[0] == 200
    status, out = call(env, "POST", L + "/simulate", body={"sessionId": "sess-cap1", "text": "Hej igen"})
    assert status == 409 and out["code"] == "session_full"
    assert call(env, "POST", L + "/simulate", body={"sessionId": "sess-cap2", "text": "Hej"})[0] == 200


def test_simulator_monthly_quota(env, monkeypatch):
    enable(env)
    monkeypatch.setattr(pa, "SIM_QUOTA_PER_MONTH", 2)
    for n in range(2):
        assert call(env, "POST", L + "/simulate", body={"sessionId": f"sess-q{n:04d}", "text": "Hej"})[0] == 200
    status, out = call(env, "POST", L + "/simulate", body={"sessionId": "sess-q9999", "text": "Hej"})
    assert status == 429 and out["code"] == "quota_exceeded"


def test_simulator_repeat_of_the_same_text_is_free(env, monkeypatch):
    enable(env)
    monkeypatch.setattr(pa, "SIM_QUOTA_PER_MONTH", 1)
    body = {"sessionId": "sess-rep1", "text": "Hej"}
    first = call(env, "POST", L + "/simulate", body=body)
    assert first[0] == 200 and call(env, "POST", L + "/simulate", body=body) == first



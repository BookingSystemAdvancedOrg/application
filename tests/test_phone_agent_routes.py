"""Phone agent JWT routes L1-L12 and hooks (feature L, CONTRACTS.md 6.L, 9):
happy path, validation, wrong role, foreign tenant 404, entitlement off,
idempotent retry (ADR-L test plan table)."""

import boto3
import pytest

from shared.features import phone_agent as pa
from test_phone_agent_support import (CALLER, L, LOC_A, LOC_A2, LOC_B, OWNER_B, STAFF_A, TENANT_A,
                                      TENANT_B, call, enable, env, set_feature, webhook)

assert env  # the fixture, imported for pytest


CONFIG = {"agentName": "Sara", "tone": "friendly", "languages": ["sv", "en"], "recording": {"enabled": False},
          "minutesCapPerMonth": 200, "version": 0}


# --- L1, L2 --------------------------------------------------------------------------------------

def test_tenant_view_and_config(env):
    status, body = call(env, "GET", "/tenant/phone-agent")
    assert status == 200 and body["usage"]["includedMinutes"] == 300 and body["usage"]["cap"] == 300
    assert {x["locationId"] for x in body["locations"]} == {LOC_A, LOC_A2}
    status, cfg = call(env, "PUT", "/tenant/phone-agent/config", body=CONFIG)
    assert (status, cfg["agentName"], cfg["minutesCapPerMonth"], cfg["version"]) == (200, "Sara", 200, 1)
    assert call(env, "PUT", "/tenant/phone-agent/config", body=CONFIG)[0] == 200          # same body again
    assert call(env, "PUT", "/tenant/phone-agent/config", body={**CONFIG, "tone": "formal"})[1]["code"] == \
        "changed_retry"                                                                    # stale version
    assert call(env, "GET", "/tenant/phone-agent", as_=STAFF_A)[0] == 403
    assert call(env, "PUT", "/tenant/phone-agent/config", as_=STAFF_A, body=CONFIG)[0] == 403
    assert call(env, "GET", "/tenant/phone-agent", as_=OWNER_B)[1]["locations"][0]["locationId"] == LOC_B


@pytest.mark.parametrize("over, code", [
    ({"recording": {"enabled": True, "retentionDays": 15, "purpose": "Kvalitet"}}, "invalid_retention"),
    ({"recording": {"enabled": True}}, "purpose_required"),
    ({"minutesCapPerMonth": -1}, "invalid_cap"),
    ({"agentName": "x" * 41}, "text_too_long"),
])
def test_config_validation(env, over, code):
    status, body = call(env, "PUT", "/tenant/phone-agent/config", body={**CONFIG, **over})
    assert (status, body["code"]) == (400, code)


def test_entitlement_off_refuses_every_route(env):
    set_feature(env, TENANT_A, False)
    assert call(env, "GET", "/tenant/phone-agent")[1]["error"] == "feature_not_in_plan"
    for method, path in (("GET", L), ("PUT", L), ("POST", L + "/kill-switch"), ("GET", L + "/calls"),
                         ("GET", L + "/callbacks")):
        assert call(env, method, path, body={})[0] == 403


# --- L3, L4, L5 ----------------------------------------------------------------------------------

def test_enable_provisions_once_and_shows_forwarding(env):
    body = enable(env)
    assert body["number"] == "+46100000001" and body["enabled"] and body["version"] == 1
    assert body["forwarding"]["codes"][0]["on"] == "**21*+46100000001#"
    again = enable(env, version=1, greetingExtraSv="Välkommen")
    assert again["number"] == "+46100000001" and len(env.tel.numbers) == 1      # no second number
    assert [c[0] for c in env.tel.calls].count("provision_number") == 1
    agent_user = boto3.resource("dynamodb").Table("test-user").get_item(
        Key={"PK": f"USER#agent:{LOC_A}", "SK": "PROFILE"})["Item"]
    assert agent_user["tenantId"] == TENANT_A and agent_user["locationId"] == LOC_A
    assert ("PhoneNumber.Provisioned", {"tenantId": TENANT_A, "locationId": LOC_A}) in env.published
    status, view = call(env, "GET", L, as_=STAFF_A)
    assert status == 200 and "transferTo" not in view and view["number"] == "+46100000001"
    assert call(env, "PUT", L, body={**{"enabled": True, "mode": "always", "version": 0}})[1]["code"] == \
        "changed_retry"
    # disable keeps the number 30 days and removes the agent's system user
    off = enable(env, version=2, enabled=False)
    assert off["enabled"] is False and off["number"] == "+46100000001"
    assert "Item" not in boto3.resource("dynamodb").Table("test-user").get_item(
        Key={"PK": f"USER#agent:{LOC_A}", "SK": "PROFILE"})


@pytest.mark.parametrize("over, code", [
    ({"agentHours": {"mon": [{"from": "20:00", "to": "18:00"}]}}, "invalid_hours"),
    ({"transferTo": "12345"}, "invalid_phone"),
    ({"maxCallSeconds": 60}, "invalid_max_call"),
    ({"counterOffers": {"nearbyTables": "yes"}}, "invalid_counter_offers"),
    ({"greetingExtraSv": "x" * 201}, "text_too_long"),
])
def test_location_validation(env, over, code):
    status, body = call(env, "PUT", L, body={**{"enabled": False, "mode": "always", "version": 0}, **over})
    assert (status, body["code"]) == (400, code)


def test_location_roles_isolation_and_provider_errors(env):
    assert call(env, "PUT", L, as_=STAFF_A, body={"enabled": False, "mode": "always", "version": 0})[0] == 403
    assert call(env, "GET", L, as_=STAFF_A, location=LOC_A2)[0] == 404
    assert call(env, "GET", L, location=LOC_B)[0] == 404
    env.tel.numbers_left = 0
    status, body = call(env, "PUT", L, body={**{"enabled": True, "mode": "always", "version": 0}})
    assert (status, body["code"]) == (409, "number_unavailable")
    env.tel.numbers_left = None
    env.voice._fail = {"create_agent": 1}
    status, body = call(env, "PUT", L, body={**{"enabled": True, "mode": "always", "version": 0}})
    assert (status, body["code"]) == (502, "voice_provider_unavailable")
    assert enable(env)["number"] == "+46100000001"   # the number from the failed try is reused


def test_counter_offer_toggles_default_on(env):
    body = enable(env)
    assert body["counterOffers"] == {"nearbyTables": True, "phoneOnlyTables": True}
    body = enable(env, version=1, counterOffers={"nearbyTables": False})
    assert body["counterOffers"] == {"nearbyTables": False, "phoneOnlyTables": True}


def test_kill_switch(env):
    enable(env)
    status, body = call(env, "POST", L + "/kill-switch", as_=STAFF_A, body={"on": True, "version": 1})
    assert (status, body["killSwitch"], body["version"]) == (200, True, 2)
    assert call(env, "POST", L + "/kill-switch", as_=STAFF_A, body={"on": True, "version": 1})[0] == 200  # same state
    assert call(env, "POST", L + "/kill-switch", body={"version": 2})[1]["field"] == "on"
    assert call(env, "POST", L + "/kill-switch", as_=STAFF_A, location=LOC_A2, body={"on": True})[0] == 404
    assert call(env, "POST", L + "/kill-switch", location=LOC_B, body={"on": True})[0] == 404


# --- L6 simulate ---------------------------------------------------------------------------------

def test_simulate_is_a_dry_run_and_idempotent(env):
    enable(env)
    env.availability.set("2026-11-03", ["19:00"])
    body = {"sessionId": "sess-0001", "text": "Hej, jag vill boka ett bord för fyra imorgon klockan 19"}
    status, out = call(env, "POST", L + "/simulate", body=body)
    assert status == 200 and out["disclosure"] is True and "AI-assistent" in out["reply"]
    assert out["toolCalls"] == [{"name": "hold_table", "ok": True}]
    assert call(env, "POST", L + "/simulate", body=body)[1] == out                      # same session + text
    call(env, "POST", L + "/simulate", body={"sessionId": "sess-0001", "text": "Ja"})
    status, out = call(env, "POST", L + "/simulate", body={"sessionId": "sess-0001", "text": "Jag heter Anna"})
    assert out["toolCalls"] == [{"name": "create_reservation", "ok": True}] and out["state"]["outcome"] == "booked"
    assert env.invoke.reservations == {}                                                # nothing written
    assert call(env, "POST", L + "/simulate", body={"sessionId": "sess-0002", "text": "x" * 501})[1]["code"] == \
        "text_too_long"
    assert call(env, "POST", L + "/simulate", as_=STAFF_A, body=body)[0] == 403
    assert call(env, "POST", L + "/simulate", location=LOC_B, body=body)[0] == 404


# --- L7-L12 calls and callbacks -----------------------------------------------------------------

def _a_call(e, ref="ref-1"):
    webhook(e, "voice", [{"kind": "call_started", "call_ref": ref, "from": CALLER, "to": "+46100000001"}])
    return pa.call_id_for(ref, pa.now())


def test_calls_list_detail_and_staff_view(env):
    enable(env)
    call_id = _a_call(env)
    status, body = call(env, "GET", L + "/calls", query={"from": "2026-11-02", "to": "2026-11-02"})
    assert status == 200 and [c["callId"] for c in body["calls"]] == [call_id] and body["calls"][0]["from"] == CALLER
    staff = call(env, "GET", L + "/calls", as_=STAFF_A)[1]["calls"][0]
    assert "from" not in staff
    status, detail = call(env, "GET", L + "/calls/{callId}", callId=call_id)
    assert status == 200 and detail["turns"][0]["role"] == "agent" and "AI-assistent" in detail["turns"][0]["text"]
    assert "from" not in call(env, "GET", L + "/calls/{callId}", as_=STAFF_A, callId=call_id)[1]
    assert call(env, "GET", L + "/calls", query={"from": "2026-11-05", "to": "2026-11-01"})[1]["code"] == \
        "invalid_range"
    assert call(env, "GET", L + "/calls/{callId}", as_=OWNER_B, location=LOC_B, callId=call_id)[0] == 404
    assert call(env, "GET", L + "/calls", as_=STAFF_A, location=LOC_A2)[0] == 404
    assert call(env, "DELETE", L + "/calls/{callId}/recording", callId=call_id)[0] == 204
    assert call(env, "DELETE", L + "/calls/{callId}/recording", callId=call_id)[0] == 204       # second 204
    assert call(env, "DELETE", L + "/calls/{callId}/recording", as_=STAFF_A, callId=call_id)[0] == 403


def test_callbacks_close_and_reservation_badge(env):
    enable(env)
    call_id = _a_call(env)
    env.live.add_callback_for(env.live.calls.get_call(TENANT_A, call_id))(name="Anna", phone=CALLER,
                                                                         topic="Allergier")
    status, body = call(env, "GET", L + "/callbacks", as_=STAFF_A)
    (cb,) = body["callbacks"]
    assert cb["topic"] == "Allergier" and cb["status"] == "open"
    assert call(env, "GET", L + "/callbacks", query={"status": "x"})[0] == 400
    close = L + "/callbacks/{callbackId}/close"
    assert call(env, "POST", close, callbackId=cb["callbackId"], body={"version": 9})[1]["code"] == "invalid_state"
    status, done = call(env, "POST", close, as_=STAFF_A, callbackId=cb["callbackId"],
                        body={"version": 1, "note": "Klart"})
    assert (status, done["status"]) == (200, "closed")
    assert call(env, "POST", close, callbackId=cb["callbackId"], body={"version": 1})[1] == done   # second close
    assert call(env, "POST", close, as_=OWNER_B, location=LOC_B, callbackId=cb["callbackId"],
                body={"version": 1})[0] == 404
    assert ("PhoneCallback.Closed", {"tenantId": TENANT_A, "locationId": LOC_A,
                                     "callbackId": cb["callbackId"]}) in env.published
    badge = L + "/reservations/{reservationId}/call"
    assert call(env, "GET", badge, reservationId="r-unknown")[0] == 404


# --- hooks ---------------------------------------------------------------------------------------

def platform(e, op, tenant_id=TENANT_A, body=None):
    return e.api.handler({"hook": "platform", "hookVersion": 1, "tenantId": tenant_id, "op": op, "body": body,
                          "query": {}, "operator": {"sub": "op-1"}})


def test_platform_ops(env):
    enable(env)
    out = platform(env, "phone-agent.tenant.get")
    assert out["ok"] and out["body"]["locations"][0]["number"] == "+46100000001"
    assert platform(env, "phone-agent.tenant.pause", body={"paused": True})["body"] == {"paused": True}
    assert platform(env, "phone-agent.tenant.get")["body"]["paused"] is True
    assert platform(env, "phone-agent.tenant.get", tenant_id="nope")["status"] == 404
    assert platform(env, "phone-agent.settings.put", body={"killSwitch": True, "maxMinutesCap": 100})["ok"]
    assert platform(env, "phone-agent.settings.get")["body"] == {"killSwitch": True, "maxMinutesCap": 100}
    assert platform(env, "phone-agent.settings.put", body={"killSwitch": True, "maxMinutesCap": -5})["code"] == \
        "invalid_cap"
    set_feature(env, TENANT_B, False)
    assert platform(env, "phone-agent.tenant.get", tenant_id=TENANT_B)["status"] == 404   # off -> 404


def test_billing_usage_privacy_and_purge(env):
    enable(env)
    call_id = _a_call(env)
    env.clock.value = env.clock.value.replace(minute=3)
    webhook(env, "voice", [{"kind": "call_ended", "call_ref": "ref-1", "tenantId": TENANT_A, "callId": call_id}])
    usage = env.api.handler({"hook": "billing.usage", "tenantId": TENANT_A, "periodStart": "2026-10-25",
                             "periodEnd": "2026-11-24"})
    assert usage == {"ok": True, "lines": [{"locationId": LOC_A, "minutes": 3}], "includedMinutes": 300}
    exported = env.api.handler({"hook": "privacy.export", "tenantId": TENANT_A, "phone": CALLER})
    assert exported["items"][0]["kind"] == "phone_call"
    assert env.api.handler({"hook": "privacy.export", "tenantId": TENANT_B, "phone": CALLER})["items"] == []
    erased = env.api.handler({"hook": "privacy.erase", "tenantId": TENANT_A, "phone": CALLER})
    assert erased["erased"]["calls"] == 1
    _, detail = call(env, "GET", L + "/calls/{callId}", callId=call_id)
    assert detail.get("from") is None and {t["text"] for t in detail["turns"]} == {"[raderad]"}
    assert detail["billedMinutes"] == 3 and detail["disclosure"]["text"]   # minutes and evidence kept
    out = env.api.handler({"hook": "tenant.purge", "tenantId": TENANT_A, "locationIds": [LOC_A, LOC_A2]})
    assert out["ok"] and out["done"]
    assert env.tel.numbers == {} and env.voice.agents == {}
    assert pa.query_all(pa.tenant_pk(TENANT_A), "") == []
    assert env.api.handler({"hook": "nope", "tenantId": TENANT_A})["code"] == "unknown_hook"

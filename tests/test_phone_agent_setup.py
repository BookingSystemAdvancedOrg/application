"""Phone agent setup checklist (feature L, CONTRACTS.md 6.L L19): the
agentName default, the forwarding codes per operator, the restaurant's own
number never used as transfer or fallback, the setup endpoint that never
calls a provider, and the dev test line."""

import pytest

from shared import tenant as shared_tenant
from shared.features import phone_agent as pa
from shared.telephony_real import forward_action
from tenant_support import LOCATION_TABLE_DEFAULT
from test_phone_agent_support import ALWAYS, L, LOC_A, STAFF_A, TENANT_A, call, enable, env

assert env  # the fixture, imported for pytest

SETUP = L + "/setup"
RESTAURANT = "+46812345678"


def restaurant_phone(e, phone=RESTAURANT):
    e.res.Table(LOCATION_TABLE_DEFAULT).update_item(
        Key={"PK": f"TENANT#{TENANT_A}", "SK": f"LOCATION#{LOC_A}"}, UpdateExpression="SET phoneNumber = :p",
        ExpressionAttributeValues={":p": phone})
    shared_tenant.reset_caches()


def test_agent_name_defaults_to_booqy_and_empty_is_refused(env):
    status, body = call(env, "GET", "/tenant/phone-agent")
    assert (status, body["config"]["agentName"], body["config"]["version"]) == (200, "Booqy", 0)
    cfg = {**body["config"], "agentName": "   "}
    status, err = call(env, "PUT", "/tenant/phone-agent/config", body=cfg)
    assert (status, err["code"], err["field"]) == (400, "agent_name_required", "agentName")


@pytest.mark.parametrize("operator, mode, on, off", [
    ("other", "always", ["**21*{n}#"], ["##21#"]),
    ("telia", "after_hours", ["**004*{n}#"], ["##004#"]),
    ("tre", "overflow", ["**004*{n}#"], ["##004#"]),
    ("telia_fast", "always", ["*21*{n}#"], ["#21#"]),
    ("telia_fast", "after_hours", ["*61*{n}#", "*67*{n}#"], ["#61#", "#67#"]),
    ("vaxel", "after_hours", [], []),
])
def test_forwarding_codes_per_operator(operator, mode, on, off):
    n = "+46100000001"
    out = pa.forwarding({"mode": mode, "number": {"e164": n}, "setup": {"operator": operator}})
    assert out["number"] == n and out["operator"] == operator
    assert [c["on"] for c in out["codes"]] == [x.format(n=n) for x in on]
    assert [c["off"] for c in out["codes"]] == off


def test_forwarding_without_operator_is_other_and_without_number_has_no_codes():
    assert pa.forwarding({"mode": "after_hours", "number": {"e164": "+46100000001"}})["operator"] == "other"
    assert pa.forwarding({"mode": "always"})["codes"] == []


def test_transfer_to_the_forwarded_restaurant_number_is_refused(env):
    restaurant_phone(env)
    status, body = call(env, "PUT", L, body={**ALWAYS, "transferTo": "08-123 456 78"})
    assert (status, body["code"], body["field"]) == (400, "transfer_is_forwarded", "transferTo")


def test_fallback_is_never_the_restaurant_number(env):
    restaurant_phone(env)
    enable(env, transferTo=None)
    (only,) = env.tel.inbound.values()
    assert only["fallback"] is None
    enable(env, version=1, transferTo="+46701234567")
    assert next(iter(env.tel.inbound.values()))["fallback"] == "+46701234567"
    # The real client only adds the failed/busy connects when there is a number.
    assert forward_action("sip:a@b", None) == {"connect": "sip:a@b"}


def test_setup_records_progress_without_calling_providers(env):
    body = enable(env)
    assert body["setup"] == {"operator": None, "forwardedAt": None, "testedAt": None}
    assert body["simulated"] is True
    before = list(env.tel.calls), list(env.voice.calls)
    status, out = call(env, "POST", SETUP, body={"operator": "telia_fast", "version": 1})
    assert (status, out["setup"]["operator"], out["version"]) == (200, "telia_fast", 2)
    assert out["forwarding"]["codes"][0]["on"] == "*21*+46100000001#"
    status, out = call(env, "POST", SETUP, body={"forwarded": True, "version": 2})
    assert status == 200 and out["setup"]["forwardedAt"] and out["setup"]["operator"] == "telia_fast"
    status, out = call(env, "POST", SETUP, body={"tested": True, "version": 3})
    assert status == 200 and out["setup"]["testedAt"]
    status, out = call(env, "POST", SETUP, body={"tested": False, "version": 4})
    assert status == 200 and out["setup"]["testedAt"] is None and out["setup"]["forwardedAt"]
    assert (list(env.tel.calls), list(env.voice.calls)) == before            # no provider was called


def test_setup_validation_version_and_roles(env):
    enable(env)
    assert call(env, "POST", SETUP, body={"operator": "comviq", "version": 1})[1]["code"] == "invalid_operator"
    assert call(env, "POST", SETUP, body={"forwarded": "yes", "version": 1})[1]["field"] == "forwarded"
    assert call(env, "POST", SETUP, body={"operator": "tre", "version": 0})[1]["code"] == "changed_retry"
    assert call(env, "POST", SETUP, as_=STAFF_A, body={"operator": "tre", "version": 1})[0] == 403


def test_changing_mode_to_another_code_asks_to_forward_again(env):
    enable(env, mode="after_hours")
    assert call(env, "POST", SETUP, body={"forwarded": True, "version": 1})[0] == 200
    same = enable(env, version=2, mode="overflow")                    # same **004* code
    assert same["setup"]["forwardedAt"]
    other = enable(env, version=3, mode="always")                     # now **21*
    assert other["setup"]["forwardedAt"] is None


def test_dev_test_line_is_the_forwarding_target(env):
    enable(env, mode="after_hours")
    env.res.Table("test-phone-agent").put_item(Item={
        "PK": f"TENANT#{TENANT_A}", "SK": "NUMBER#+358759899876", "tenantId": TENANT_A, "locationId": LOC_A,
        "locationIds": [LOC_A], "testLine": True})
    assert pa.test_line(TENANT_A, LOC_A) == "+358759899876"
    assert pa.test_line(TENANT_A, "other-location") is None
    status, view = call(env, "GET", L)
    assert status == 200 and view["testNumber"] == "+358759899876"
    assert view["forwarding"]["number"] == "+358759899876"
    assert view["forwarding"]["codes"][0]["on"] == "**004*+358759899876#"


@pytest.mark.parametrize("operator, reset", [
    ("telia", ["##002#"]), ("other", ["##002#"]), ("telia_fast", ["#21#", "#61#", "#67#"]), ("vaxel", []),
])
def test_an_older_forwarding_is_turned_off_first(operator, reset):
    loc = {"mode": "after_hours", "number": {"e164": "+46100000001"}, "setup": {"operator": operator}}
    assert [c["on"] for c in pa.forwarding(loc)["reset"]] == reset
    assert pa.forwarding({"mode": "always"})["reset"] == []                  # no number, nothing to dial


def test_always_then_after_hours_turns_the_old_forwarding_off_first(env):
    """Alltid (**21*) and then När ni har stängt (**004*): **21* would stay on
    and win, so every call would still go to Booqy while the restaurant is open."""
    enable(env, mode="always")
    assert call(env, "POST", SETUP, body={"forwarded": True, "version": 1})[0] == 200
    out = enable(env, version=2, mode="after_hours")
    assert out["setup"]["forwardedAt"] is None                               # asked to forward again
    assert [c["on"] for c in out["forwarding"]["reset"]] == ["##002#"]
    assert [c["on"] for c in out["forwarding"]["codes"]] == ["**004*+46100000001#"]


def test_changing_the_operator_after_forwarding_asks_again(env):
    enable(env, mode="after_hours")
    call(env, "POST", SETUP, body={"operator": "telia", "version": 1})
    call(env, "POST", SETUP, body={"forwarded": True, "version": 2})
    status, same = call(env, "POST", SETUP, body={"tested": True, "version": 3})
    status, same = call(env, "POST", SETUP, body={"operator": "tele2", "version": 4})      # same **004* code
    assert status == 200 and same["setup"]["forwardedAt"] and same["setup"]["testedAt"]
    status, other = call(env, "POST", SETUP, body={"operator": "telia_fast", "version": 5})  # *61* and *67*
    assert status == 200 and other["setup"]["operator"] == "telia_fast"
    assert other["setup"]["forwardedAt"] is None and other["setup"]["testedAt"] is None


def test_a_span_to_2359_covers_the_last_minute():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    loc = {"mode": "after_hours", "agentHours": {"sat": [{"from": "22:00", "to": "23:59"}]}}
    at = datetime(2026, 11, 14, 23, 59, 30, tzinfo=ZoneInfo("Europe/Stockholm"))         # a Saturday
    assert pa.inside_hours(loc, at)
    assert not pa.inside_hours({**loc, "agentHours": {"sat": [{"from": "22:00", "to": "23:30"}]}}, at)

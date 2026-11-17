"""Audit finding 88: guest search narrows to the selected location."""

from tenant_support import LOC_A, LOC_A2, LOC_B

from test_guest_crm import RES_TABLE, booking, call, env, stream  # noqa: F401

SEARCH = "/tenant/guests/search"


def _seed(e):
    stream(e, RES_TABLE, booking("r1", loc=LOC_A, email="anna1@x.se", name="Anna Ett"), seq="1")
    stream(e, RES_TABLE, booking("r2", loc=LOC_A2, email="anna2@x.se", name="Anna Tva"), seq="2")


def test_owner_name_search_without_location_finds_both(env):
    _seed(env)
    status, out = call(env, "POST", SEARCH, body={"name": "anna"})
    assert status == 200 and len(out["guests"]) == 2


def test_owner_name_search_with_location_keeps_only_that_location(env):
    _seed(env)
    status, out = call(env, "POST", SEARCH, body={"name": "anna", "locationId": LOC_A2})
    assert status == 200 and [g["name"] for g in out["guests"]] == ["Anna Tva"]


def test_owner_email_search_with_other_location_finds_nothing(env):
    _seed(env)
    status, out = call(env, "POST", SEARCH, body={"email": "anna1@x.se", "locationId": LOC_A2})
    assert status == 200 and out["guests"] == []
    status, out = call(env, "POST", SEARCH, body={"email": "anna1@x.se", "locationId": LOC_A})
    assert status == 200 and len(out["guests"]) == 1


def test_other_tenants_or_unknown_location_is_404(env):
    _seed(env)
    assert call(env, "POST", SEARCH, body={"name": "anna", "locationId": LOC_B})[0] == 404
    assert call(env, "POST", SEARCH, body={"name": "anna", "locationId": "nope"})[0] == 404


def test_staff_other_location_is_404_own_location_ok(env):
    _seed(env)
    assert call(env, "POST", SEARCH, role="staff_user", body={"name": "anna", "locationId": LOC_A2})[0] == 404
    status, out = call(env, "POST", SEARCH, role="staff_user", body={"name": "anna", "locationId": LOC_A})
    assert status == 200 and [g["name"] for g in out["guests"]] == ["Anna Ett"]


def test_location_id_must_be_string(env):
    assert call(env, "POST", SEARCH, body={"name": "anna", "locationId": 5})[0] == 400

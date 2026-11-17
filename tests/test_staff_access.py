"""Staff permissions per admin area (shared/staff_access.py, owner request
2026-11-16): the logic, the tenant gate, the owner's edit API and every
guarded API route (staff without the area get 403 no_access)."""

import json
import re
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from shared import staff_access as sa
from shared import tenant
from tenant_support import (
    LOC_A,
    OWNER_A_SUB,
    REGION,
    STAFF_A_SUB,
    TENANT_A,
    TENANT_TABLE,
    features_on,
    load_fn,
    seed_tenancy,
    tenant_row,
    with_tenant_claims,
)


# --- the logic -------------------------------------------------------------------


def test_unsaved_tenant_keeps_what_staff_had_before():
    row = tenant_row(TENANT_A)
    assert sa.defaults(row) == list(sa.LEGACY_DEFAULTS)
    assert "personal" not in sa.areas_for(row, "staff_user", "s")
    assert "statistik" in sa.areas_for(row, "staff_user", "s")
    row["insights"] = {"staffAccess": "none"}
    assert "statistik" not in sa.areas_for(row, "staff_user", "s")


def test_owner_always_has_everything():
    row = tenant_row(TENANT_A, staffAccess={"defaults": [], "users": {}})
    assert sa.areas_for(row, "owner_user", OWNER_A_SUB) == list(sa.AREAS)
    assert all(sa.allows(row, "owner_user", OWNER_A_SUB, a) for a in sa.AREAS)


def test_staff_areas_are_group_plus_own_minus_removed():
    row = tenant_row(TENANT_A, staffAccess={
        "defaults": ["bokningar", "meny"],
        "users": {"s1": {"add": ["presentkort"], "remove": ["meny"]}},
    })
    assert sa.areas_for(row, "staff_user", "s1") == ["bokningar", "presentkort"]
    assert sa.areas_for(row, "staff_user", "s2") == ["bokningar", "meny"]
    assert sa.allows(row, "staff_user", "s1", ("meny", "presentkort"))
    assert not sa.allows(row, "staff_user", "s1", "meny")
    assert sa.areas_for(row, None, "s1") == []


def test_unknown_area_raises_instead_of_guessing():
    with pytest.raises(KeyError):
        sa.allows(tenant_row(TENANT_A), "owner_user", "o", "kassa")


def test_stored_garbage_is_ignored():
    row = tenant_row(TENANT_A, staffAccess={"defaults": ["bokningar", "kassa", 3], "users": {"s": "x"}})
    assert sa.areas_for(row, "staff_user", "s") == ["bokningar"]


def test_retired_webbplats_area_is_harmless():
    """The site builder (area webbplats) was removed: a stored grant is
    dropped when read, and an older admin that still sends it is accepted
    with the area dropped instead of a 400."""
    assert "webbplats" not in sa.AREAS
    row = tenant_row(TENANT_A, staffAccess={"defaults": ["bokningar", "webbplats"],
                                            "users": {"s1": {"add": ["webbplats", "meny"]}}})
    assert sa.areas_for(row, "staff_user", "s1") == ["bokningar", "meny"]
    assert sa.owner_view(row)["defaults"] == ["bokningar"]
    new = sa.apply_edit(tenant_row(TENANT_A), {"version": 0, "defaults": ["webbplats", "meny"],
                                               "users": {"s1": {"add": ["webbplats"], "remove": []}}}, "t", "o")
    assert new["defaults"] == ["meny"] and "s1" not in (new.get("users") or {})
    with pytest.raises(ValueError):
        sa.apply_edit(tenant_row(TENANT_A), {"version": 0, "defaults": ["kassa"]}, "t", "o")


def test_apply_edit_validates_bumps_version_and_tidies():
    row = tenant_row(TENANT_A)
    new = sa.apply_edit(row, {"version": 0, "defaults": ["meny", "bokningar"],
                              "users": {"s1": {"add": ["bokningar", "personal"], "remove": ["meny", "gaster"]}}},
                        "2026-11-16T10:00:00Z", OWNER_A_SUB)
    assert new["defaults"] == ["bokningar", "meny"]
    # "bokningar" is already the group's and "gaster" isn't: both dropped.
    assert new["users"] == {"s1": {"add": ["personal"], "remove": ["meny"]}}
    assert new["version"] == 1 and new["updatedBy"] == OWNER_A_SUB
    row["staffAccess"] = new
    cleared = sa.apply_edit(row, {"version": 1, "users": {"s1": None}}, "t", OWNER_A_SUB)
    assert cleared["users"] == {} and cleared["defaults"] == ["bokningar", "meny"]


@pytest.mark.parametrize("body", [
    {"defaults": []},
    {"version": 0, "defaults": ["kassa"]},
    {"version": 0, "defaults": "meny"},
    {"version": 0, "users": {"s": {"add": ["meny"], "remove": ["meny"]}}},
    {"version": 0, "users": {"a#b": {"add": []}}},
    {"version": 0, "users": {"s": {"add": [], "other": []}}},
    {"version": 0, "role": "owner_user"},
    {"version": True},
])
def test_apply_edit_refuses_bad_input(body):
    with pytest.raises(ValueError):
        sa.apply_edit(tenant_row(TENANT_A), body, "t", "o")


def test_apply_edit_refuses_a_stale_version():
    row = tenant_row(TENANT_A, staffAccess={"defaults": [], "users": {}, "version": 3})
    with pytest.raises(sa.Conflict):
        sa.apply_edit(row, {"version": 2, "defaults": []}, "t", "o")


# --- the tenant gate ------------------------------------------------------------------


def _jwt_event(role="staff_user", sub=None):
    return with_tenant_claims({"requestContext": {}}, TENANT_A, role, sub)


def _set_access(**config):
    table = boto3.resource("dynamodb", region_name=REGION).Table(TENANT_TABLE)
    row = table.get_item(Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"})["Item"]
    row["staffAccess"] = {"defaults": [], "users": {}, "version": 1, **config}
    table.put_item(Item=row)


@pytest.fixture
def tenancy(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
        monkeypatch.setenv("USER_TABLE_NAME", "test-user")
        seed_tenancy(monkeypatch, features_a=features_on(*sa_features()))
        yield
        tenant.reset_caches()


def sa_features():
    return ("guestCrm", "giftCards", "events", "marketing", "metaAds", "googleBusiness",
            "socialPosting", "personalliggare", "phoneAgent", "waitlist")


def test_for_jwt_refuses_staff_without_the_area(tenancy):
    _set_access(defaults=["bokningar"])
    with pytest.raises(tenant.TenantError) as exc:
        tenant.for_jwt(_jwt_event(), area="meny")
    assert (exc.value.status, exc.value.error) == (403, "no_access")
    assert tenant.for_jwt(_jwt_event(), area="bokningar").role == "staff_user"
    assert tenant.for_jwt(_jwt_event(), area=("meny", "bokningar")).allows("bokningar")
    assert tenant.for_jwt(_jwt_event("owner_user"), area="meny").allows("meny")


def test_a_fresh_grant_works_at_once_despite_the_cache(tenancy):
    _set_access(defaults=[])
    with pytest.raises(tenant.TenantError):
        tenant.for_jwt(_jwt_event(), area="meny")
    _set_access(defaults=[], users={STAFF_A_SUB: {"add": ["meny"], "remove": []}})
    # The row is cached for a minute; a refusal re-reads it first.
    assert tenant.for_jwt(_jwt_event(), area="meny").allows("meny")


def test_a_personal_removal_beats_the_group(tenancy):
    _set_access(defaults=["meny"], users={STAFF_A_SUB: {"add": [], "remove": ["meny"]}})
    with pytest.raises(tenant.TenantError):
        tenant.for_jwt(_jwt_event(), area="meny")


# --- the owner's API (tenant-account) -----------------------------------------------------


def _tenant_account(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("STRIPE_SECRET_ARN", "arn:aws:secretsmanager:eu-north-1:1:secret:x")
    monkeypatch.setenv("STRIPE_API_VERSION", "2025-01-01")
    monkeypatch.setenv("ADMIN_APP_URL", "https://admin.example")
    return load_fn("tenant-account", "sa_tenant_account")


def _call(app, method, role="owner_user", body=None):
    event = with_tenant_claims({
        "routeKey": f"{method} /tenant",
        "requestContext": {"http": {"method": method, "path": "/tenant"}},
        "body": json.dumps(body) if body is not None else None,
    }, TENANT_A, role)
    response = app.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def test_owner_reads_and_edits_staff_access(tenancy, monkeypatch):
    app = _tenant_account(monkeypatch)
    status, body = _call(app, "GET")
    assert status == 200
    assert body["access"]["areas"] == list(sa.AREAS)
    assert body["staffAccess"]["defaults"] == list(sa.LEGACY_DEFAULTS)
    assert body["staffAccess"]["version"] == 0

    status, body = _call(app, "PATCH", body={"staffAccess": {
        "version": 0, "defaults": ["bokningar"], "users": {STAFF_A_SUB: {"add": ["personal"], "remove": []}}}})
    assert status == 200, body
    assert body["staffAccess"]["defaults"] == ["bokningar"]
    assert body["staffAccess"]["users"] == {STAFF_A_SUB: {"add": ["personal"], "remove": []}}
    assert body["staffAccess"]["version"] == 1

    # Staff see only their own areas, never the owner's view.
    status, body = _call(app, "GET", role="staff_user")
    assert status == 200
    assert body["access"]["areas"] == ["bokningar", "personal"]
    assert body["staffAccess"] is None


def test_two_owners_saving_at_once_get_409(tenancy, monkeypatch):
    app = _tenant_account(monkeypatch)
    assert _call(app, "PATCH", body={"staffAccess": {"version": 0, "defaults": []}})[0] == 200
    status, body = _call(app, "PATCH", body={"staffAccess": {"version": 0, "defaults": ["meny"]}})
    assert (status, body["error"]) == (409, "changed_retry")


def test_staff_can_never_change_permissions(tenancy, monkeypatch):
    app = _tenant_account(monkeypatch)
    _set_access(defaults=list(sa.AREAS))  # even with every area
    status, body = _call(app, "PATCH", role="staff_user", body={"staffAccess": {
        "version": 1, "users": {STAFF_A_SUB: {"add": [], "remove": []}}}})
    assert (status, body["error"]) == (403, "owner_only")


# --- every guarded route ------------------------------------------------------------------

L = {"locationId": LOC_A}
# (function, route key, path parameters, the area(s) that open it)
GUARDED = [
    ("insights", "GET /insights", {}, "statistik"),
    ("get-reservation", "GET /locations/{locationId}/reservations", L, "bokningar"),
    ("get-reservation", "GET /locations/{locationId}/reservations/{reservationId}",
     {**L, "reservationId": "r1"}, "bokningar"),
    ("create-pending-reservation", "POST /locations/{locationId}/reservations/manual", L, "bokningar"),
    ("mark-arrived", "POST /locations/{locationId}/reservations/{reservationId}/status",
     {**L, "reservationId": "r1"}, "bokningar"),
    ("mark-arrived", "PATCH /locations/{locationId}/reservations/{reservationId}",
     {**L, "reservationId": "r1"}, "bokningar"),
    ("block-table", "POST /locations/{locationId}/tables/{tableId}/block", {**L, "tableId": "t1"}, "bokningar"),
    ("waitlist", "GET /locations/{locationId}/waitlist", L, "bokningar"),
    ("waitlist", "POST /locations/{locationId}/waitlist/{entryId}/seat", {**L, "entryId": "e1"}, "bokningar"),
    ("manage-order", "GET /locations/{locationId}/orders", L, "bestallningar"),
    ("manage-order", "PUT /locations/{locationId}/orders/{proxy+}", {**L, "proxy": "o1/status"}, "bestallningar"),
    ("print-jobs", "GET /locations/{locationId}/print", L, "bestallningar"),
    ("print-jobs", "GET /locations/{locationId}/print/{proxy+}", {**L, "proxy": "routing"}, "meny"),
    ("print-jobs", "POST /locations/{locationId}/print/{proxy+}", {**L, "proxy": "jobs"}, "bestallningar"),
    ("catering-requests", "GET /locations/{locationId}/catering/requests", L, "catering"),
    ("catering-offer", "GET /locations/{locationId}/catering/requests/{requestId}",
     {**L, "requestId": "q1"}, "catering"),
    ("catering-offer", "POST /locations/{locationId}/catering/requests/{requestId}/delivered",
     {**L, "requestId": "q1"}, "catering"),
    ("personalliggare", "GET /locations/{locationId}/personalliggare", L, "personalliggare"),
    ("personalliggare", "POST /locations/{locationId}/personalliggare/check-in", L, "personalliggare"),
    ("phone-agent", "GET /locations/{locationId}/phone-agent/calls", L, "telefon"),
    ("phone-agent", "POST /locations/{locationId}/phone-agent/kill-switch", L, "telefon"),
    ("manage-menu", "POST /locations/{locationId}/menu/{proxy+}", {**L, "proxy": "items"}, "meny"),
    ("manage-menu", "PUT /locations/{locationId}/menu/{proxy+}", {**L, "proxy": "items/i1"}, "meny"),
    ("manage-menu", "DELETE /locations/{locationId}/menu/{proxy+}", {**L, "proxy": "items/i1"}, "meny"),
    ("manage-layout-element", "POST /locations/{locationId}/layout-elements/{proxy+}",
     {**L, "proxy": "tables"}, "layout"),
    ("manage-layout-element", "GET /locations/{locationId}/layout-elements/{proxy+}",
     {**L, "proxy": "tables"}, "layout"),
    ("manage-user", "GET /list-users", {}, "personal"),
    ("manage-user", "POST /users/{proxy+}", {"proxy": "invite"}, "personal"),
    ("guest-crm", "GET /tenant/guests", {}, "gaster"),
    ("guest-crm", "POST /tenant/guests/search", {}, "gaster"),
    ("guest-crm", "POST /locations/{locationId}/guests/lookup", L, "bokningar"),
    ("gift-cards", "GET /tenant/gift-cards", {}, "presentkort"),
    ("gift-cards", "GET /tenant/gift-cards/settings", {}, "presentkort"),
    ("events-tickets", "GET /locations/{locationId}/events", L, "evenemang"),
    ("events-tickets", "POST /locations/{locationId}/events/{eventId}/occurrences/{occurrenceId}/check-ins",
     {**L, "eventId": "e1", "occurrenceId": "o1"}, "evenemang"),
    ("marketing", "GET /marketing/{proxy+}", {"proxy": "campaigns"}, "marknadsforing"),
    ("meta-ads", "GET /tenant/meta-ads/connection", {}, "marknadsforing"),
    ("google-business", "GET /tenant/google-business/connection", {}, "marknadsforing"),
    ("social-posting", "GET /tenant/social-posts", {}, "marknadsforing"),
]

_ENV_NAMES = sorted({
    name
    for source in [*(Path(__file__).parents[1] / "functions").glob("*/*.py"),
                   *(Path(__file__).parents[1] / "shared").rglob("*.py")]
    for name in re.findall(r'os\.environ\[\s*"([A-Z0-9_]+)"', source.read_text())
})


@pytest.fixture
def routes(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
        for name in _ENV_NAMES:
            monkeypatch.setenv(name, f"test-{name.lower().replace('_', '-')}")
        monkeypatch.setenv("ENVIRONMENT", "dev")
        monkeypatch.setenv("USER_TABLE_NAME", "test-user")
        seed_tenancy(monkeypatch, features_a=features_on(*sa_features()))
        yield {}
        tenant.reset_caches()


def _route_event(route_key, params):
    method, path = route_key.split(" ", 1)
    raw = path
    for key, value in params.items():
        raw = raw.replace("{" + key + "+}", value).replace("{" + key + "}", value)
    return with_tenant_claims({
        "version": "2.0",
        "routeKey": route_key,
        "rawPath": raw,
        "pathParameters": dict(params) or None,
        "queryStringParameters": None,
        "headers": {"content-type": "application/json"},
        "body": "{}",
        "requestContext": {"http": {"method": method, "path": raw}, "routeKey": route_key},
    }, TENANT_A, "staff_user")


def _invoke(routes, fn, route_key, params):
    module = routes.get(fn) or load_fn(fn, f"sa_{fn.replace('-', '_')}")
    routes[fn] = module
    tenant.reset_caches()
    response = module.handler(_route_event(route_key, params), None)
    body = response.get("body")
    try:
        body = json.loads(body) if isinstance(body, str) and body else {}
    except ValueError:
        body = {}
    return response["statusCode"], body


@pytest.mark.parametrize("fn,route_key,params,area", GUARDED, ids=[f"{g[0]}:{g[1]}" for g in GUARDED])
def test_staff_without_the_area_get_403(routes, fn, route_key, params, area):
    _set_access(defaults=[])
    status, body = _invoke(routes, fn, route_key, params)
    assert (status, body.get("error")) == (403, "no_access"), body


@pytest.mark.parametrize("fn,route_key,params,area", GUARDED, ids=[f"{g[0]}:{g[1]}" for g in GUARDED])
def test_staff_with_the_area_get_past_the_gate(routes, fn, route_key, params, area):
    _set_access(defaults=[], users={STAFF_A_SUB: {"add": [area], "remove": []}})
    status, body = _invoke(routes, fn, route_key, params)
    assert body.get("error") != "no_access", (status, body)


def test_staff_with_personal_never_change_roles_or_delete(routes):
    _set_access(defaults=["personal"])
    for route_key, proxy in (("PUT /users/{proxy+}", "other-sub/group"), ("DELETE /users/{proxy+}", "other-sub")):
        status, body = _invoke(routes, "manage-user", route_key, {"proxy": proxy})
        assert (status, body.get("error")) == (403, "owner_only")


def test_staff_with_personal_see_only_their_own_locations_staff(routes):
    _set_access(defaults=["personal"])
    users = boto3.resource("dynamodb", region_name=REGION).Table("test-user")
    users.put_item(Item={"PK": "USER#other-loc", "SK": "PROFILE", "cognitoSub": "other-loc", "tenantId": TENANT_A,
                         "role": "staff", "locationId": "loc-a-2", "email": "o@a.example", "name": "Other",
                         "status": "active", "createdBy": OWNER_A_SUB, "createdAt": "2026-10-02T10:00:00.000Z"})
    users.put_item(Item={"PK": "USER#same-loc", "SK": "PROFILE", "cognitoSub": "same-loc", "tenantId": TENANT_A,
                         "role": "staff", "locationId": LOC_A, "email": "s@a.example", "name": "Same",
                         "status": "active", "createdBy": OWNER_A_SUB, "createdAt": "2026-10-02T10:00:00.000Z"})
    status, body = _invoke(routes, "manage-user", "GET /list-users", {})
    assert status == 200, body
    assert sorted(u["cognitoSub"] for u in body["items"]) == ["same-loc", STAFF_A_SUB]


def test_help_assistant_follows_the_areas():
    from shared import help_assistant as help

    doc = {"audience": "all", "area": "meny"}
    owner_page = {"audience": "owner", "area": "personal"}
    row = tenant_row(TENANT_A, staffAccess={"defaults": ["bokningar"], "users": {"s1": {"add": ["personal"]}}})
    staff = help.caller_view(row, "staff_user", "s1")
    other = help.caller_view(row, "staff_user", "s2")
    assert not help.visible(doc, **staff)
    assert help.visible(owner_page, **staff) and not help.visible(owner_page, **other)
    assert help.visible(doc, **help.caller_view(row, "owner_user", OWNER_A_SUB))

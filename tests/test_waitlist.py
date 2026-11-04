"""Waitlist (feature F, ADR-F, CONTRACTS.md 6.F): routes F1-F10, the offer
expiry timer, hooks and the two-tenant isolation suite, against moto with the
real availability engine and the real hold/keep/release transactions, so an
offer blocks the table exactly like a guest's table hold."""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from hooks_fake import entitle
from shared import availability as shared_availability
from shared import dynamo as shared_dynamo
from shared import events, manage_link
from shared import reservations as shared_r
from shared import tenant as shared_tenant
from shared.features import waitlist as wl
from tenant_support import (LOC_A, LOC_A2, LOC_B, REGION, STAFF_A_SUB, TENANT_A, TENANT_B, TENANT_TABLE,
                            USER_TABLE_DEFAULT, create_tables, features_on, tenant_row, with_tenant_claims)
from test_reservations import _table, location_item, publish_layout

ROOT = Path(__file__).parents[1] / "functions"
LOCATION_TABLE, OCCUPANCY_TABLE, SNAPSHOT_TABLE = "test-location", "test-occupancy", "test-layout-snapshot"
RESERVATION_TABLE, WAITLIST_TABLE, GROUP = "test-reservation", "test-waitlist", "dev-waitlist"
DAY = "2026-09-20"  # Sunday; the clock stands at 18:00 Stockholm that day
NOW = datetime(2026, 9, 20, 16, 0, tzinfo=timezone.utc)
E1, E2, E3 = "01ENTRYAAAAAAAAAAAAAAAAAAA", "01ENTRYBBBBBBBBBBBBBBBBBBB", "01ENTRYCCCCCCCCCCCCCCCCCCC"


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


def load(name):
    spec = importlib.util.spec_from_file_location(f"wl_{name.replace('-', '_')}", ROOT / name / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeLambda:
    def __init__(self):
        self.calls = []
        self.fail = False

    def invoke(self, **kwargs):
        if self.fail:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "ServiceException"}}, "Invoke")
        self.calls.append(json.loads(kwargs["Payload"]))
        return {"StatusCode": 202}


@pytest.fixture
def env(monkeypatch):
    for key, value in {
        "ENVIRONMENT": "dev", "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": REGION,
        "LOCATION_TABLE_NAME": LOCATION_TABLE, "SLOT_OCCUPANCY_TABLE_NAME": OCCUPANCY_TABLE,
        "PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME": SNAPSHOT_TABLE, "RESERVATION_TABLE_NAME": RESERVATION_TABLE,
        "USER_TABLE_NAME": USER_TABLE_DEFAULT, "WAITLIST_TABLE_NAME": WAITLIST_TABLE,
        "SCHEDULE_GROUP_NAME": GROUP, "SCHEDULER_INVOKE_ROLE_ARN": "arn:aws:iam::123456789012:role/dev-wl-sched",
        "SELF_FUNCTION_ARN": "arn:aws:lambda:eu-north-1:123456789012:function:dev-waitlist:live",
        "NOTIFICATION_FUNCTION_NAME": "dev-notification:live", "SITE_DOMAIN": "dev.booqy.se",
    }.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        manage_link.reset_cache()
        secret = boto3.client("secretsmanager", region_name=REGION).create_secret(
            Name="test/reservations/link-signing-key", SecretString="k" * 64)
        monkeypatch.setenv("RESERVATION_LINK_KEY_SECRET_ARN", secret["ARN"])
        resource = boto3.resource("dynamodb", region_name=REGION)
        for name in (OCCUPANCY_TABLE, SNAPSHOT_TABLE, RESERVATION_TABLE, WAITLIST_TABLE):
            _table(resource, name)
        create_tables(monkeypatch, location_table=LOCATION_TABLE)
        on = features_on("waitlist")
        resource.Table(TENANT_TABLE).put_item(Item=tenant_row(TENANT_A, features=on, slug="bistro-a"))
        resource.Table(TENANT_TABLE).put_item(Item=tenant_row(TENANT_B, features=on, slug="bistro-b"))
        for tenant_id, loc in ((TENANT_A, LOC_A), (TENANT_A, LOC_A2), (TENANT_B, LOC_B)):
            resource.Table(LOCATION_TABLE).put_item(Item=location_item(tenant_id, loc))
            publish_layout(resource, loc)
        resource.Table(USER_TABLE_DEFAULT).put_item(Item={
            "PK": f"USER#{STAFF_A_SUB}", "SK": "PROFILE", "tenantId": TENANT_A, "role": "staff",
            "locationId": LOC_A, "status": "active"})
        boto3.client("scheduler", region_name=REGION).create_schedule_group(Name=GROUP)
        clock = Clock(NOW)
        monkeypatch.setattr(shared_r, "now_utc", clock)
        monkeypatch.setattr(wl, "now", clock)
        monkeypatch.setattr(shared_availability, "_now_epoch", lambda: clock().timestamp())
        published = []
        monkeypatch.setattr(events, "publish", lambda f, t, d: published.append((t, d)) or "id")
        api, create = load("waitlist"), load("create-pending-reservation")
        fake = FakeLambda()
        api._clients["lambda"] = fake
        yield SimpleNamespace(api=api, create=create, res=resource, clock=clock, published=published, sms=fake)
        shared_dynamo._resource = None
        shared_dynamo._client = None
        shared_tenant.reset_caches()
        manage_link.reset_cache()


OWNER_A, OWNER_B, STAFF_A = (TENANT_A, "owner_user"), (TENANT_B, "owner_user"), (TENANT_A, "staff_user")
Q = "/locations/{locationId}/waitlist"


def call(e, method, template, *, as_=OWNER_A, location=LOC_A, entry=None, body=None, query=None):
    params = {"locationId": location}
    if entry:
        params["entryId"] = entry
    event = {"routeKey": f"{method} {template}", "pathParameters": params, "queryStringParameters": query,
             "requestContext": {"http": {"method": method}}, "body": json.dumps(body) if body is not None else None}
    tenant_id, role = as_
    with_tenant_claims(event, tenant_id, role, STAFF_A_SUB if role == "staff_user" else None)
    response = e.api.handler(event, None)
    return response["statusCode"], (json.loads(response["body"]) if response.get("body") else None)


def public(e, method, template, *, location=LOC_A, token=None, body=None):
    params = {"locationId": location} if token is None else {"token": token}
    event = {"routeKey": f"{method} {template}", "pathParameters": params, "headers": {},
             "requestContext": {"http": {"method": method, "sourceIp": "203.0.113.9"}},
             "body": json.dumps(body) if body is not None else None}
    response = e.api.handler(event, None)
    return response["statusCode"], (json.loads(response["body"]) if response.get("body") else None)


def settings(e, *, location=LOC_A, as_=OWNER_A, **over):
    body = {"enabled": True, "holdMinutes": 10, "maxWaiting": 50, "publicJoin": True, "version": 0, **over}
    return call(e, "PUT", Q + "/settings", as_=as_, location=location, body=body)


def walk_in(e, entry=E1, party=2, phone="0701234567", **kw):
    return call(e, "POST", Q, body={"entryId": entry, "name": "Sara", "partySize": party, "phone": phone}, **kw)


def offer(e, entry=E1, tables=("table-a",), start="20:00", version=None, **kw):
    if version is None:
        version = entry_row(e, entry)["version"]
    return call(e, "POST", Q + "/{entryId}/offer", entry=entry,
                body={"tableIds": list(tables), "startTime": start, "version": int(version)}, **kw)


def entry_row(e, entry=E1, location=LOC_A):
    return e.res.Table(WAITLIST_TABLE).get_item(Key=wl.entry_key(location, DAY, entry))["Item"]


def guest_booking(e, tables=("table-a",), start="20:00", party=2):
    payload = {"date": DAY, "startTime": start, "tableIds": list(tables), "partySize": party,
               "name": "Anna", "email": "anna@example.se", "phone": "0709999999", "acceptTerms": True}
    event = {"routeKey": "POST /locations/{locationId}/reservations", "pathParameters": {"locationId": LOC_A},
             "requestContext": {"http": {"method": "POST"}}, "headers": {}, "body": json.dumps(payload)}
    return e.create.handler(event, None)


def schedules():
    return [s["Name"] for s in boto3.client("scheduler").list_schedules(GroupName=GROUP)["Schedules"]]


# --- F6 settings -----------------------------------------------------------------------------

def test_settings_owner_only_validation_version_and_mirror(env):
    status, body = settings(env)
    assert status == 200 and body == {"enabled": True, "holdMinutes": 10, "maxWaiting": 50, "publicJoin": True,
                                      "version": 1}
    loc = env.res.Table(LOCATION_TABLE).get_item(Key={"PK": f"TENANT#{TENANT_A}", "SK": f"LOCATION#{LOC_A}"})["Item"]
    assert loc["waitlist"] == {"publicJoin": True}
    assert settings(env)[1]["code"] == "changed_retry"
    assert settings(env, holdMinutes=3, version=1)[1]["code"] == "invalid_hold"
    assert settings(env, maxWaiting=500, version=1)[1]["code"] == "invalid_max"
    assert settings(env, as_=STAFF_A, version=1)[1]["error"] == "owner_only"
    assert settings(env, location=LOC_B, version=0)[0] == 404


# --- F2 / F1 --------------------------------------------------------------------------------------

def test_walk_in_validation_idempotency_and_list(env):
    assert walk_in(env, party=0)[1]["code"] == "invalid_party_size"
    assert walk_in(env, party=21)[1]["code"] == "invalid_party_size"
    assert walk_in(env, phone="12")[1]["code"] == "invalid_phone"
    status, first = walk_in(env)
    assert status == 201 and first["status"] == "waiting" and first["position"] == 1
    assert first["phone"] == "+46701234567"
    assert walk_in(env)[1]["entryId"] == first["entryId"]  # same entryId -> same entry
    assert walk_in(env, entry=E2)[1]["code"] == "already_queued"
    walk_in(env, entry=E2, phone=None)
    status, body = call(env, "GET", Q, query={"date": DAY})
    assert status == 200 and [x["position"] for x in body["entries"]] == [1, 2]
    assert body["suggestions"][0] == {"entryId": E1, "tableIds": ["table-a"], "startTime": "18:00"}
    assert call(env, "GET", Q, query={"date": "2026-13-01"})[0] == 400
    joined = [n for n in env.sms.calls if n["notice"]["kind"] == "waitlist.joined"]
    assert len(joined) == 1 and joined[0]["notice"]["to"] == "+46701234567"


def test_queue_full(env):
    settings(env, maxWaiting=1)
    walk_in(env)
    assert walk_in(env, entry=E2, phone="0702222222")[1]["code"] == "queue_full"


def test_staff_other_location_and_foreign_tenant_404(env):
    walk_in(env)
    assert walk_in(env, entry=E2, location=LOC_A2, as_=STAFF_A)[0] == 404
    assert call(env, "GET", Q, location=LOC_A2, as_=STAFF_A)[0] == 404
    assert call(env, "GET", Q, location=LOC_B)[0] == 404
    assert call(env, "GET", Q, as_=OWNER_B)[0] == 404
    assert walk_in(env, as_=STAFF_A, entry=E3, phone="0703333333")[0] == 201


def test_entitlement_off_refused(env):
    env.res.Table(TENANT_TABLE).update_item(Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"},
                                            UpdateExpression="SET entitlements.features.waitlist = :f",
                                            ExpressionAttributeValues={":f": False})
    shared_tenant.reset_caches()
    assert call(env, "GET", Q)[1]["error"] == "feature_not_in_plan"
    assert walk_in(env)[1]["error"] == "feature_not_in_plan"
    assert public(env, "POST", "/public/locations/{locationId}/waitlist", body={})[0] == 404


# --- F3 offer / F4 seat / F5 remove ----------------------------------------------------------------

def test_offer_holds_the_table_and_retry_keeps_one_hold(env):
    walk_in(env)
    status, body = offer(env)
    assert status == 200 and body["status"] == "offered" and body["offer"]["tableIds"] == ["table-a"]
    assert body["offer"]["expiresAt"] == "2026-09-20T16:10:00Z"
    # The table is blocked for every other guest by the existing occupancy rows.
    assert guest_booking(env)["statusCode"] == 409
    again = offer(env, version=1)
    assert again[0] == 200 and again[1]["offer"]["reservationId"] == body["offer"]["reservationId"]
    holds = [i for i in env.res.Table(RESERVATION_TABLE).scan()["Items"] if i.get("status") == "held"]
    assert len(holds) == 1 and holds[0]["waitlistEntryId"] == E1
    assert schedules() == [f"waitlist-offer-{E1}"]
    offered = [n for n in env.sms.calls if n["notice"]["kind"] == "waitlist.offered"]
    assert len(offered) == 1 and "#" in offered[0]["notice"]["text"]
    assert entry_row(env)["notice"]["sentAt"]


def test_offer_validation_state_and_foreign(env):
    walk_in(env)
    assert offer(env, tables=("table-x",))[0] == 400
    assert offer(env, version=7)[1]["code"] == "changed_retry"
    assert offer(env, as_=OWNER_B, location=LOC_B)[0] == 404
    assert call(env, "POST", Q + "/{entryId}/offer", entry="nope", body={"tableIds": ["table-a"],
                                                                         "startTime": "18:00", "version": 1})[0] == 404
    guest_booking(env)
    status, body = offer(env)
    assert (status, body["code"]) == (409, "table_taken")


def test_two_offers_on_one_table_exactly_one_wins(env):
    walk_in(env)
    walk_in(env, entry=E2, phone="0702222222")
    assert offer(env)[0] == 200
    status, body = offer(env, entry=E2)
    assert (status, body["code"]) == (409, "table_taken")


def test_seat_from_offer_converts_once(env):
    walk_in(env)
    offer(env)
    status, body = call(env, "POST", Q + "/{entryId}/seat", entry=E1, body={"version": 2})
    assert status == 200 and body["entry"]["status"] == "seated"
    rid = body["reservationId"]
    booking = shared_r.load(LOC_A, rid)
    assert booking["status"] == "arrived" and "holdOnly" not in booking
    slot = env.res.Table(OCCUPANCY_TABLE).get_item(Key=shared_r.slot_key(
        LOC_A, DAY, booking["startTime"], booking["endTime"], "table-a"))["Item"]
    assert "holdExpiresAt" not in slot
    again = call(env, "POST", Q + "/{entryId}/seat", entry=E1, body={"version": 2})
    assert again[1]["reservationId"] == rid
    assert ("Waitlist.Converted", {"tenantId": TENANT_A, "locationId": LOC_A, "entryId": E1, "date": DAY,
                                   "reservationId": rid, "how": "seated"}) in env.published


def test_seat_waiting_entry_directly_and_stale_version(env):
    walk_in(env)
    assert call(env, "POST", Q + "/{entryId}/seat", entry=E1, body={"version": 9})[1]["code"] == "changed_retry"
    status, body = call(env, "POST", Q + "/{entryId}/seat", entry=E1, body={"tableIds": ["table-b"], "version": 1})
    assert status == 200 and shared_r.load(LOC_A, body["reservationId"])["status"] == "arrived"


def test_remove_releases_hold_and_is_idempotent(env):
    walk_in(env)
    offer(env)
    assert call(env, "POST", Q + "/{entryId}/remove", entry=E1, body={"version": 2, "x": 1})[0] == 400
    status, body = call(env, "POST", Q + "/{entryId}/remove", entry=E1, body={"version": 2, "reason": "gick"})
    assert status == 200 and body["status"] == "removed"
    assert guest_booking(env)["statusCode"] == 201  # the table is bookable again
    assert call(env, "POST", Q + "/{entryId}/remove", entry=E1, body={"version": 2})[1]["status"] == "removed"


# --- expiry ---------------------------------------------------------------------------------------

def fire(e, entry=E1):
    return e.api.handler({"waitlistJob": {"action": "expire", "locationId": LOC_A, "date": DAY, "entryId": entry}})


def test_expiry_timer_idempotent_and_resumable(env):
    walk_in(env)
    offer(env)
    assert fire(env) == {"result": "noop"}  # before expiresAt nothing changes
    env.clock.value = NOW + timedelta(minutes=11)
    assert fire(env) == {"result": "expired"}
    assert fire(env) == {"result": "noop"}  # run twice
    assert entry_row(env)["status"] == "expired"
    assert guest_booking(env)["statusCode"] == 201
    assert [n["notice"]["kind"] for n in env.sms.calls].count("waitlist.expired") == 1


def test_timer_after_accept_is_noop(env):
    settings(env)
    status, joined = public(env, "POST", "/public/locations/{locationId}/waitlist",
                            body={"entryId": E1, "name": "Sara", "partySize": 2, "phone": "0701234567"})
    offer(env)
    assert public(env, "POST", "/public/waitlist/{token}/accept", token=joined["token"], body={})[0] == 200
    env.clock.value = NOW + timedelta(minutes=11)
    assert fire(env) == {"result": "noop"}
    assert entry_row(env)["status"] == "booked"


def test_accept_after_expires_is_409_and_table_bookable(env):
    settings(env)
    _, joined = public(env, "POST", "/public/locations/{locationId}/waitlist",
                       body={"entryId": E1, "name": "Sara", "partySize": 2, "phone": "0701234567"})
    offer(env)
    env.clock.value = NOW + timedelta(minutes=10, seconds=1)
    status, body = public(env, "POST", "/public/waitlist/{token}/accept", token=joined["token"], body={})
    assert (status, body["code"]) == (409, "hold_expired")
    assert public(env, "GET", "/public/waitlist/{token}", token=joined["token"])[1]["status"] == "expired"
    assert guest_booking(env)["statusCode"] == 201


def test_walk_in_booking_wins_race_after_hold_expired(env):
    settings(env)
    _, joined = public(env, "POST", "/public/locations/{locationId}/waitlist",
                       body={"entryId": E1, "name": "Sara", "partySize": 2, "phone": "0701234567"})
    offer(env)
    env.clock.value = NOW + timedelta(minutes=10, seconds=1)
    assert guest_booking(env)["statusCode"] == 201  # took over the expired hold
    env.clock.value = NOW + timedelta(minutes=5)  # even a skewed reader cannot convert a taken table
    status, body = public(env, "POST", "/public/waitlist/{token}/accept", token=joined["token"], body={})
    assert (status, body["code"]) == (409, "hold_expired")


# --- public F7-F10 --------------------------------------------------------------------------------

def test_public_join_status_accept_leave(env):
    join = "/public/locations/{locationId}/waitlist"
    body = {"entryId": E1, "name": "Sara", "partySize": 2, "phone": "0701234567", "wantFrom": "18:00"}
    assert public(env, "POST", join, body=body)[0] == 404  # publicJoin off
    settings(env)
    assert public(env, "POST", join, body={**body, "phone": "1"})[1]["code"] == "invalid_phone"
    assert public(env, "POST", join, body={**body, "x": 1})[0] == 400
    status, joined = public(env, "POST", join, body=body)
    assert status == 201 and joined["position"] == 1
    assert public(env, "POST", join, body=body)[1]["token"] == joined["token"]  # same entryId -> same token
    token = joined["token"]
    status, view = public(env, "GET", "/public/waitlist/{token}", token=token)
    assert (status, view["status"], view["position"]) == (200, "waiting", 1)
    assert "Sara" not in json.dumps(view) and "+4670" not in json.dumps(view)
    assert public(env, "POST", "/public/waitlist/{token}/accept", token=token, body={})[1]["code"] == "invalid_state"
    offer(env)
    view = public(env, "GET", "/public/waitlist/{token}", token=token)[1]
    assert view["offer"] == {"startTime": "20:00", "expiresAt": "2026-09-20T16:10:00Z"}
    status, accepted = public(env, "POST", "/public/waitlist/{token}/accept", token=token, body={})
    assert status == 200 and shared_r.load(LOC_A, accepted["reservationId"])["status"] == "reserved"
    again = public(env, "POST", "/public/waitlist/{token}/accept", token=token, body={})[1]
    assert again["reservationId"] == accepted["reservationId"]
    assert public(env, "POST", "/public/waitlist/{token}/leave", token=token, body={})[0] == 204
    assert entry_row(env)["status"] == "booked"  # leaving after booking changes nothing


def test_public_leave_idempotent_and_bad_tokens(env):
    settings(env)
    _, joined = public(env, "POST", "/public/locations/{locationId}/waitlist",
                       body={"entryId": E1, "name": "Sara", "partySize": 2, "phone": "0701234567"})
    assert public(env, "POST", "/public/waitlist/{token}/leave", token=joined["token"], body={})[0] == 204
    assert public(env, "POST", "/public/waitlist/{token}/leave", token=joined["token"], body={})[0] == 204
    assert entry_row(env)["status"] == "left"
    assert public(env, "GET", "/public/waitlist/{token}", token="garbage")[0] == 404
    body, mac = joined["token"].split(".")
    assert public(env, "GET", "/public/waitlist/{token}", token=f"{body}.{mac[:-2]}xx")[0] == 404
    # a token for B's location signed over A's data does not exist
    forged = wl.token_for(LOC_B, DAY, E1)
    assert public(env, "GET", "/public/waitlist/{token}", token=forged)[0] == 404


def test_public_join_inactive_tenant_404(env):
    env.res.Table(TENANT_TABLE).update_item(Key={"PK": f"TENANT#{TENANT_B}", "SK": "PROFILE"},
                                            UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"},
                                            ExpressionAttributeValues={":s": "suspended"})
    shared_tenant.reset_caches()
    status, _ = public(env, "POST", "/public/locations/{locationId}/waitlist", location=LOC_B,
                       body={"entryId": E1, "name": "Sara", "partySize": 2, "phone": "0701234567"})
    assert status == 404


def test_sms_invoke_failure_leaves_no_sent_at(env):
    env.sms.fail = True
    walk_in(env)
    row = entry_row(env)
    assert row["notice"]["kind"] == "joined" and "sentAt" not in row["notice"]


# --- hooks and isolation ---------------------------------------------------------------------------

def test_privacy_hooks_and_purge(env):
    walk_in(env)
    walk_in(env, entry=E2, location=LOC_A2, phone="0701234567")
    answer = env.api.handler({"hook": "privacy.export", "tenantId": TENANT_A, "phone": "+46701234567"})
    assert answer["ok"] and len(answer["items"]) == 2
    assert env.api.handler({"hook": "privacy.export", "tenantId": TENANT_B, "phone": "+46701234567"})["items"] == []
    erased = env.api.handler({"hook": "privacy.erase", "tenantId": TENANT_A, "phone": "070-123 45 67"})
    assert erased["erased"] == {"waitlistEntries": 2}
    settings(env)
    walk_in(env, entry=E3, phone="0703333333")
    purged = env.api.handler({"hook": "tenant.purge", "tenantId": TENANT_A, "locationIds": [LOC_A, LOC_A2],
                              "round": 1})
    assert purged["done"] and purged["counts"]["items"] == 2
    assert env.res.Table(WAITLIST_TABLE).scan()["Items"] == []
    assert env.api.handler({"hook": "x", "tenantId": TENANT_A})["code"] == "unknown_hook"


def test_two_tenant_isolation(env):
    walk_in(env)
    call(env, "POST", Q, as_=OWNER_B, location=LOC_B,
         body={"entryId": E1, "name": "Bob", "partySize": 2, "phone": "0701234567"})
    a = call(env, "GET", Q)[1]["entries"]
    b = call(env, "GET", Q, as_=OWNER_B, location=LOC_B)[1]["entries"]
    assert [x["name"] for x in a] == ["Sara"] and [x["name"] for x in b] == ["Bob"]
    for path in ("/offer", "/seat", "/remove"):
        assert call(env, "POST", Q + "/{entryId}" + path, as_=OWNER_B, location=LOC_A, entry=E1,
                    body={"version": 1})[0] == 404
    # same entryId at two tenants: separate rows, separate tokens
    assert wl.token_for(LOC_A, DAY, E1) != wl.token_for(LOC_B, DAY, E1)


def test_entitlement_switch_back_on(env):
    entitle(TENANT_A, "waitlist")
    assert walk_in(env)[0] == 201


def test_public_join_turnstile_failure_is_turnstile_failed(env, monkeypatch):
    from shared import turnstile
    settings(env)
    monkeypatch.setenv("TURNSTILE_REQUIRED", "true")

    def refuse(token, remote_ip=None, action=None):
        raise ValueError("turnstile verification failed")
    monkeypatch.setattr(turnstile, "verify", refuse)
    body = {"entryId": E1, "name": "Sara", "partySize": 2, "phone": "0701234567", "turnstileToken": "bad"}
    status, answer = public(env, "POST", "/public/locations/{locationId}/waitlist", body=body)
    assert status == 400 and answer["code"] == "turnstile_failed" and answer["field"] == "turnstileToken"

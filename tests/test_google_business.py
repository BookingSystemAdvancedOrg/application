"""Google Business Profile (feature D, ADR-D, CONTRACTS.md 6.D): routes
D1-D11, hooks, the hourly sync job, the review request flow and the
two-tenant isolation suite. Google is the simulator."""

import importlib.util
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from hooks_fake import entitle
from shared import consent, events, external_client
from shared import google_business as port
from shared import tenant as shared_tenant
from shared.features import google_business as gbp
from tenant_support import (LOC_A, LOC_A2, LOC_B, REGION, TENANT_A, TENANT_B, features_on, location_row,
                            seed_tenancy, tenant_row, with_tenant_claims)

ROOT = Path(__file__).parents[1] / "functions"
GBP_TABLE, RES_TABLE, ORDER_TABLE, CONSENT_TABLE = "test-gbp", "test-reservation", "test-order", "test-consent"
GROUP = "dev-google-business"
HOURS = {d: [{"opensAt": "11:00", "closesAt": "22:00"}] for d in
         ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday")}
HOURS["sunday"] = []


def load(name):
    spec = importlib.util.spec_from_file_location(f"gbp_{name.replace('-', '_')}", ROOT / name / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create(ddb, name, gsis=(), extra=()):
    ddb.create_table(
        TableName=name, BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("PK", "SK", *extra)],
        **({"GlobalSecondaryIndexes": list(gsis)} if gsis else {}))


def _today():
    return datetime.now(timezone.utc).date()


@pytest.fixture
def env(monkeypatch):
    with mock_aws():
        for key, value in {"AWS_DEFAULT_REGION": REGION, "ENVIRONMENT": "dev", "GOOGLE_BUSINESS_TABLE_NAME": GBP_TABLE,
                           "RESERVATION_TABLE_NAME": RES_TABLE, "ORDER_TABLE_NAME": ORDER_TABLE,
                           "CONSENT_TABLE_NAME": CONSENT_TABLE, "SCHEDULE_GROUP_NAME": GROUP,
                           "SCHEDULER_INVOKE_ROLE_ARN": "arn:aws:iam::123456789012:role/dev-gbp-scheduler",
                           "JOBS_FUNCTION_ARN": "arn:aws:lambda:eu-north-1:123456789012:function:"
                                                "dev-google-business-jobs:live",
                           "GOOGLE_REDIRECT_URI": "https://admin.example/installningar?google=1",
                           "MARKETING_LINK_BASE": "https://api.example", "MARKETING_LINK_KEY_SECRET_ARN": "mkt-key",
                           "NO_REPLY_EMAIL_ADDRESS": "no-reply@mail.example",
                           "MAIL_OUTBOX_BUCKET_NAME": "test-outbox"}.items():
            monkeypatch.setenv(key, value)
        special = [{"date": (_today() + timedelta(days=3)).isoformat(), "kind": "closed", "note": "Personalfest"},
                   {"date": (_today() + timedelta(days=5)).isoformat(), "kind": "hours",
                    "intervals": [{"opensAt": "12:00", "closesAt": "15:00"}]},
                   {"date": (_today() + timedelta(days=6)).isoformat(), "kind": "no_online"},
                   {"date": (_today() + timedelta(days=400)).isoformat(), "kind": "closed"}]
        loc_extra = {"businessHours": HOURS, "specialDays": special, "timezone": "Europe/Stockholm"}
        seed_tenancy(monkeypatch,
                     tenant_a=tenant_row(TENANT_A, features=features_on("googleBusiness"), legalName="Bistro A AB"),
                     tenant_b=tenant_row(TENANT_B, features=features_on("googleBusiness")),
                     locations_a=[location_row(TENANT_A, LOC_A, **loc_extra), location_row(TENANT_A, LOC_A2,
                                                                                            **loc_extra)],
                     locations_b=[location_row(TENANT_B, LOC_B, **loc_extra)])
        ddb = boto3.client("dynamodb", region_name=REGION)
        _create(ddb, GBP_TABLE, extra=("GSI1PK", "GSI1SK"), gsis=[{
            "IndexName": "byLinked", "Projection": {"ProjectionType": "ALL"},
            "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"},
                          {"AttributeName": "GSI1SK", "KeyType": "RANGE"}]}])
        for name in (RES_TABLE, ORDER_TABLE, CONSENT_TABLE):
            _create(ddb, name)
        boto3.client("s3", region_name=REGION).create_bucket(
            Bucket="test-outbox", CreateBucketConfiguration={"LocationConstraint": REGION})
        boto3.client("secretsmanager", region_name=REGION).create_secret(Name="mkt-key", SecretString="k" * 40)
        boto3.client("scheduler", region_name=REGION).create_schedule_group(Name=GROUP)
        from shared import marketing as mkt
        mkt.reset_key_cache()
        google = port.SimulatedGoogleBusiness()
        gbp.reset_client(google)
        published = []
        monkeypatch.setattr(events, "publish", lambda f, t, d: published.append((t, d)) or "id")
        api, jobs = load("google-business"), load("google-business-jobs")
        res = boto3.resource("dynamodb", region_name=REGION)
        yield SimpleNamespace(api=api, jobs=jobs, google=google, published=published, res=res)
        gbp.reset_client()


def call(e, method, template, *, tenant=TENANT_A, role="owner_user", body=None, params=None):
    event = {"routeKey": f"{method} {template}", "pathParameters": dict(params or {}),
             "queryStringParameters": None, "body": json.dumps(body) if body is not None else None}
    with_tenant_claims(event, tenant, role)
    response = e.api.handler(event, None)
    return response["statusCode"], (json.loads(response["body"]) if response.get("body") else None)


def connect(e, tenant=TENANT_A):
    url = call(e, "POST", "/tenant/google-business/connection/start", tenant=tenant, body={})[1]["authorizeUrl"]
    state = re.search(r"state=([^&]+)", url).group(1)
    status, body = call(e, "POST", "/tenant/google-business/connection/callback", tenant=tenant,
                        body={"code": "simulated", "state": state})
    assert status == 200, body
    return state


LOC = "/locations/{locationId}/google-business"
LINK = LOC + "/link"
SYNC = LOC + "/sync"
SETTINGS = LOC + "/review-settings"


def link(e, tenant=TENANT_A, loc=LOC_A, google="locations/sim-1"):
    return call(e, "PUT", LINK, tenant=tenant, params={"locationId": loc}, body={"googleLocation": google})


def settings_body(**over):
    return {"enabled": True, "sources": ["booking", "order"], "channels": ["email"], "delayMinutes": 120,
            "minDaysBetween": 90, "version": 0, **over}


def turn_off(tenant_id=TENANT_A):
    boto3.resource("dynamodb", region_name=REGION).Table("test-tenant").update_item(
        Key={"PK": f"TENANT#{tenant_id}", "SK": "PROFILE"},
        UpdateExpression="SET entitlements.features.googleBusiness = :f", ExpressionAttributeValues={":f": False})
    shared_tenant.reset_caches()


# --- D1-D4 -----------------------------------------------------------------------------

def test_connection_flow_and_roles(env):
    assert call(env, "GET", "/tenant/google-business/connection") == (200, {"status": "disconnected"})
    state = connect(env)
    status, body = call(env, "GET", "/tenant/google-business/connection")
    assert (status, body) == (200, {"status": "connected", "googleAccount": "accounts/sim"})
    # reused state: single use
    assert call(env, "POST", "/tenant/google-business/connection/callback",
                body={"code": "simulated", "state": state})[0] == 400
    assert ("GoogleConnection.Connected", {"tenantId": TENANT_A}) in env.published
    for method, path in (("GET", "/tenant/google-business/connection"),
                         ("POST", "/tenant/google-business/connection/start"),
                         ("DELETE", "/tenant/google-business/connection")):
        status, body = call(env, method, path, role="staff_user", body={} if method == "POST" else None)
        assert (status, body["error"]) == (403, "owner_only")
    secret = boto3.client("secretsmanager").get_secret_value(SecretId=gbp.secret_name(TENANT_A))["SecretString"]
    assert "sim-refresh" in secret


def test_start_rejects_unknown_field_and_each_call_new_state(env):
    assert call(env, "POST", "/tenant/google-business/connection/start", body={"x": 1})[0] == 400
    one = call(env, "POST", "/tenant/google-business/connection/start", body={})[1]["authorizeUrl"]
    two = call(env, "POST", "/tenant/google-business/connection/start", body={})[1]["authorizeUrl"]
    assert one != two


def test_callback_bad_and_foreign_state(env):
    assert call(env, "POST", "/tenant/google-business/connection/callback",
                body={"code": "simulated", "state": "short"})[1]["code"] == "invalid_state"
    url = call(env, "POST", "/tenant/google-business/connection/start", tenant=TENANT_B, body={})[1]["authorizeUrl"]
    b_state = re.search(r"state=([^&]+)", url).group(1)
    status, body = call(env, "POST", "/tenant/google-business/connection/callback",
                        body={"code": "simulated", "state": b_state})
    assert (status, body["code"]) == (400, "invalid_state")


def test_entitlement_off_refuses_every_route(env):
    turn_off()
    for method, path, params in (("GET", "/tenant/google-business/connection", None),
                                 ("POST", "/tenant/google-business/connection/start", None),
                                 ("GET", "/tenant/google-business/locations", None),
                                 ("GET", LOC, {"locationId": LOC_A}), ("PUT", SETTINGS, {"locationId": LOC_A})):
        status, body = call(env, method, path, params=params, body={} if method != "GET" else None)
        assert (status, body["error"]) == (403, "feature_not_in_plan")


def test_disconnect_removes_secret_links_and_schedules_and_is_idempotent(env):
    connect(env)
    assert link(env)[0] == 200
    call(env, "PUT", SETTINGS, params={"locationId": LOC_A}, body=settings_body())
    env.jobs.schedule_review(visit())
    assert call(env, "DELETE", "/tenant/google-business/connection")[0] == 204
    assert call(env, "GET", "/tenant/google-business/connection")[1] == {"status": "disconnected"}
    assert gbp.get_link(TENANT_A, LOC_A) is None
    assert gbp.get_refresh_token(TENANT_A) is None
    job = env.res.Table(GBP_TABLE).get_item(Key=gbp.revjob_key(LOC_A, "booking", "res-1"))["Item"]
    assert (job["status"], job["reason"]) == ("skipped", "disabled")
    assert boto3.client("scheduler").list_schedules(GroupName=GROUP)["Schedules"] == []
    assert call(env, "DELETE", "/tenant/google-business/connection")[0] == 204
    assert ("GoogleConnection.Disconnected", {"tenantId": TENANT_A, "reason": "owner"}) in env.published


# --- D5-D9 -----------------------------------------------------------------------------

def test_locations_needs_connection_and_marks_links(env):
    status, body = call(env, "GET", "/tenant/google-business/locations")
    assert (status, body["code"]) == (409, "not_connected")
    connect(env)
    link(env)
    locations = call(env, "GET", "/tenant/google-business/locations")[1]["locations"]
    assert [loc.get("linkedTo") for loc in locations] == [LOC_A, None]
    assert call(env, "GET", "/tenant/google-business/locations", role="staff_user")[0] == 403


def test_link_happy_retry_unknown_already_linked_and_foreign(env):
    connect(env)
    status, body = link(env)
    assert status == 200 and body["googleLocation"] == "locations/sim-1" and body["syncStatus"] == "pending"
    assert link(env) == (200, body)  # same link again
    status, body = link(env, google="locations/nope")
    assert status == 404
    status, body = link(env, loc=LOC_A2)  # sim-1 is A's LOC_A
    assert (status, body["code"]) == (409, "already_linked")
    assert link(env, loc=LOC_B)[0] == 404
    assert call(env, "PUT", LINK, role="staff_user", params={"locationId": LOC_A},
                body={"googleLocation": "locations/sim-2"})[0] == 403
    # relink to another Google location needs the version
    status, body = link(env, google="locations/sim-2")
    assert (status, body["code"]) == (409, "changed_retry")
    status, body = call(env, "PUT", LINK, params={"locationId": LOC_A},
                        body={"googleLocation": "locations/sim-2", "version": 1})
    assert status == 200 and body["version"] == 2
    assert link(env, loc=LOC_A2, google="locations/sim-1")[0] == 200  # sim-1 freed


def test_link_not_connected(env):
    status, body = link(env)
    assert (status, body["code"]) == (409, "not_connected")


def test_unlink_is_idempotent_and_foreign_404(env):
    connect(env)
    link(env)
    assert call(env, "DELETE", LINK, params={"locationId": LOC_A})[0] == 204
    assert call(env, "DELETE", LINK, params={"locationId": LOC_A})[0] == 204
    assert call(env, "DELETE", LINK, params={"locationId": LOC_B})[0] == 404
    assert link(env, loc=LOC_A2)[0] == 200


def test_location_view_preview_and_foreign(env):
    status, body = call(env, "GET", LOC, params={"locationId": LOC_A})
    assert status == 200 and "link" not in body
    assert body["reviewSettings"]["enabled"] is False
    assert body["stats"] == {"sent30d": 0, "skipped30d": 0, "clicks30d": 0}
    special = body["preview"]["specialHours"]["specialHourPeriods"]
    assert [p["closed"] for p in special] == [True, False]
    assert call(env, "GET", LOC, params={"locationId": LOC_B})[0] == 404
    assert call(env, "GET", LOC, role="staff_user", params={"locationId": LOC_A})[0] == 403


def test_sync_now_pushes_once_then_hash_skips(env):
    status, body = call(env, "POST", SYNC, params={"locationId": LOC_A}, body={})
    assert (status, body["code"]) == (409, "not_linked")
    connect(env)
    link(env)
    status, body = call(env, "POST", SYNC, params={"locationId": LOC_A}, body={})
    assert status == 200 and body["changed"] is True and body["syncStatus"] == "ok"
    pushed = env.google.hours["locations/sim-1"]
    assert pushed["regular"]["periods"][0] == {"openDay": "MONDAY", "openTime": {"hours": 11},
                                               "closeDay": "MONDAY", "closeTime": {"hours": 22}}
    calls = len([c for c in env.google.calls if c[0] == "patch_hours"])
    status, body = call(env, "POST", SYNC, params={"locationId": LOC_A}, body={})
    assert status == 200 and body["changed"] is False
    assert len([c for c in env.google.calls if c[0] == "patch_hours"]) == calls
    assert call(env, "POST", SYNC, params={"locationId": LOC_B}, body={})[0] == 404
    assert call(env, "POST", SYNC, role="staff_user", params={"locationId": LOC_A}, body={})[0] == 403


def test_sync_google_down_is_502_with_retry_after_and_recorded(env):
    connect(env)
    link(env)
    env.google._fail["patch_hours"] = 1
    status, body = call(env, "POST", SYNC, params={"locationId": LOC_A}, body={})
    assert (status, body["code"], body["retryAfter"]) == (502, "google_unavailable", 60)
    assert gbp.get_link(TENANT_A, LOC_A)["syncStatus"] == "error"
    assert any(t == "GoogleHours.SyncFailed" for t, _ in env.published)
    assert call(env, "POST", SYNC, params={"locationId": LOC_A}, body={})[1]["changed"] is True


# --- D10 -------------------------------------------------------------------------------

@pytest.mark.parametrize("over,code", [({"delayMinutes": 10}, "invalid_delay"),
                                       ({"minDaysBetween": 400}, "invalid_days"),
                                       ({"channels": ["fax"]}, "invalid_channel"),
                                       ({"channels": []}, "invalid_channel")])
def test_review_settings_validation(env, over, code):
    status, body = call(env, "PUT", SETTINGS, params={"locationId": LOC_A}, body=settings_body(**over))
    assert (status, body["code"]) == (400, code)


def test_review_settings_version_roles_foreign(env):
    status, body = call(env, "PUT", SETTINGS, params={"locationId": LOC_A}, body=settings_body())
    assert status == 200 and body["version"] == 1 and body["enabled"] is True
    status, body = call(env, "PUT", SETTINGS, params={"locationId": LOC_A}, body=settings_body())
    assert (status, body["code"]) == (409, "changed_retry")
    assert call(env, "PUT", SETTINGS, params={"locationId": LOC_B}, body=settings_body())[0] == 404
    assert call(env, "PUT", SETTINGS, role="staff_user", params={"locationId": LOC_A}, body=settings_body())[0] == 403
    assert call(env, "PUT", SETTINGS, params={"locationId": LOC_A}, body={**settings_body(), "x": 1})[0] == 400


# --- mapping ---------------------------------------------------------------------------

def test_mapping_closed_hours_no_online_and_twelve_month_cut():
    today = date(2026, 11, 4)
    location = {"businessHours": {**HOURS, "friday": [{"opensAt": "11:00", "closesAt": "14:00"},
                                                      {"opensAt": "17:00", "closesAt": "23:59"}]},
                "specialDays": [{"date": "2026-11-01", "kind": "closed"},
                                {"date": "2026-12-24", "kind": "closed", "note": "Julafton"},
                                {"date": "2026-12-31", "kind": "hours",
                                 "intervals": [{"opensAt": "18:00", "closesAt": "23:30"}]},
                                {"date": "2027-01-05", "kind": "no_online"},
                                {"date": "2027-11-04", "kind": "closed"}, {"date": "2027-11-05", "kind": "closed"}]}
    mapped = gbp.map_hours(location, today)
    friday = [p for p in mapped["regularHours"]["periods"] if p["openDay"] == "FRIDAY"]
    assert friday[1]["closeTime"] == {"hours": 23, "minutes": 59}
    assert not [p for p in mapped["regularHours"]["periods"] if p["openDay"] == "SUNDAY"]
    special = mapped["specialHours"]["specialHourPeriods"]
    assert [(p["startDate"]["month"], p["startDate"]["day"], p["closed"]) for p in special] == [
        (12, 24, True), (12, 31, False), (11, 4, True)]
    assert "Julafton" not in json.dumps(mapped)
    assert gbp.hours_hash(mapped) == gbp.hours_hash(gbp.map_hours(location, today))


# --- hourly job ------------------------------------------------------------------------

def test_hourly_job_idempotent_and_continues_after_failure(env):
    connect(env)
    connect(env, tenant=TENANT_B)
    link(env)
    link(env, tenant=TENANT_B, loc=LOC_B, google="locations/sim-2")
    env.google._fail["patch_hours"] = 1  # first location fails, the other still syncs
    counts = env.jobs.run_sync()
    assert counts["checked"] == 2 and counts["failed"] == 1 and counts["changed"] == 1
    counts = env.jobs.run_sync()  # resumes: only the failed one is pushed
    assert counts["changed"] == 1 and counts["failed"] == 0
    before = len(env.google.calls)
    assert env.jobs.run_sync()["changed"] == 0
    assert not [c for c in env.google.calls[before:] if c[0] == "patch_hours"]


def test_hourly_job_skips_tenant_without_entitlement(env):
    connect(env)
    link(env)
    turn_off()
    assert env.jobs.run_sync() == {"checked": 1, "changed": 0, "failed": 0, "skipped": 1}


# --- review flow -----------------------------------------------------------------------

EMAIL = "gast@example.com"


def visit(kind="booking", source_id="res-1", tenant=TENANT_A, loc=LOC_A, email=EMAIL, status="arrived"):
    table = RES_TABLE if kind == "booking" else ORDER_TABLE
    prefix = "RESERVATION#" if kind == "booking" else "ORDER#"
    row = {"PK": f"LOCATION#{loc}", "SK": f"{prefix}{source_id}", "locationId": loc, "status": status,
           "customerEmail": email, "customerName": "Sara"}
    if kind == "order":
        row["paymentStatus"] = "paid"
    boto3.resource("dynamodb", region_name=REGION).Table(table).put_item(Item=row)
    return {"tenantId": tenant, "locationId": loc, "kind": kind, "sourceId": source_id,
            "guestKey": consent.subject(tenant, "email", email), "channels": ["email"],
            "contactRef": {"table": "reservation" if kind == "booking" else "order", "pk": row["PK"], "sk": row["SK"]},
            "returning": False, "at": "2026-11-04T10:00:00.000Z"}


def grant(tenant=TENANT_A, email=EMAIL):
    consent.grant(boto3.resource("dynamodb", region_name=REGION).Table(CONSENT_TABLE), tenant_id=tenant,
                  restaurant="Bistro", version=consent.CURRENT, channels=["email"], email=email,
                  location_id=LOC_A, source="booking", ref="res-1")


def enable(e, tenant=TENANT_A, loc=LOC_A, **over):
    connect(e, tenant=tenant)
    google = "locations/sim-1" if tenant == TENANT_A else "locations/sim-2"
    assert link(e, tenant=tenant, loc=loc, google=google)[0] == 200
    assert call(e, "PUT", SETTINGS, tenant=tenant, params={"locationId": loc}, body=settings_body(**over))[0] == 200


def fire(e, kind="booking", source_id="res-1", tenant=TENANT_A, loc=LOC_A):
    return e.jobs.handler({"reviewJob": {"tenantId": tenant, "locationId": loc, "kind": kind,
                                         "sourceId": source_id}})["result"]


def outbox():
    s3 = boto3.client("s3")
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket="test-outbox").get("Contents") or []]
    return [json.loads(s3.get_object(Bucket="test-outbox", Key=k)["Body"].read()) for k in keys]


def bus_event(detail):
    return {"source": "booqy.guest-crm", "detail-type": "GuestVisit.Recorded", "detail": detail}


def test_review_sent_once_with_consent_and_withdraw_link(env):
    enable(env)
    grant()
    assert env.jobs.handler(bus_event(visit()))["result"] == "scheduled"
    assert env.jobs.handler(bus_event(visit()))["result"] == "duplicate"  # replayed event: one schedule
    schedules = boto3.client("scheduler").list_schedules(GroupName=GROUP)["Schedules"]
    assert [s["Name"] for s in schedules] == ["gbp-review-booking-res-1"]
    assert fire(env) == "sent"
    assert fire(env) == "done"  # re-fired schedule never sends twice
    mails = outbox()
    assert len(mails) == 1 and mails[0]["to"] == EMAIL
    assert "https://example.invalid/review/sim-1" in mails[0]["text"]
    assert "/m/w/" in mails[0]["listUnsubscribe"]
    assert "betyg" not in mails[0]["text"].lower()  # same text for all, no rating question
    assert ("ReviewRequest.Sent", {"tenantId": TENANT_A, "locationId": LOC_A, "kind": "booking",
                                   "sourceId": "res-1", "channel": "email"}) in env.published
    view = call(env, "GET", LOC, params={"locationId": LOC_A})[1]
    assert view["stats"]["sent30d"] == 1


def test_review_sms_goes_through_notification_not_sns(env, monkeypatch):
    monkeypatch.setenv("NOTIFICATION_FUNCTION_NAME", "dev-notification:live")
    invoked = []
    env.jobs._clients["lambda"] = SimpleNamespace(invoke=lambda **kw: invoked.append(kw) or {"StatusCode": 202})
    monkeypatch.setattr(boto3, "client", lambda name, *a, **k: pytest.fail("direct SNS") if name == "sns"
                        else real_client(name, *a, **k))
    enable(env, channels=["sms"])
    phone = "+46701234567"
    detail = visit(email="")
    boto3.resource("dynamodb", region_name=REGION).Table(RES_TABLE).update_item(
        Key={"PK": f"LOCATION#{LOC_A}", "SK": "RESERVATION#res-1"}, UpdateExpression="SET customerPhone = :p",
        ExpressionAttributeValues={":p": phone})
    consent.grant(boto3.resource("dynamodb", region_name=REGION).Table(CONSENT_TABLE), tenant_id=TENANT_A,
                  restaurant="Bistro", version=consent.CURRENT, channels=["sms"], phone=phone, location_id=LOC_A,
                  source="booking", ref="res-1")
    env.jobs.schedule_review({**detail, "guestKey": consent.subject(TENANT_A, "sms", phone), "channels": ["sms"]})
    assert fire(env) == "sent"
    assert len(invoked) == 1 and invoked[0]["FunctionName"] == "dev-notification:live"
    assert invoked[0]["InvocationType"] == "Event"
    notice = json.loads(invoked[0]["Payload"])["notice"]
    assert notice["kind"] == "google-business.review-request" and notice["channel"] == "sms"
    assert notice["to"] == phone and notice["tenantId"] == TENANT_A and notice["locationId"] == LOC_A
    assert "https://example.invalid/review/sim-1" in notice["text"] and "/m/w/" in notice["text"]
    assert outbox() == []


real_client = boto3.client


def test_review_sms_invoke_failure_releases_the_job_for_a_retry(env, monkeypatch):
    monkeypatch.setenv("NOTIFICATION_FUNCTION_NAME", "dev-notification:live")
    invoked = []

    def invoke(**kw):
        invoked.append(kw)
        if len(invoked) == 1:
            raise ClientError({"Error": {"Code": "TooManyRequestsException", "Message": "x"}}, "Invoke")
        return {"StatusCode": 202}

    env.jobs._clients["lambda"] = SimpleNamespace(invoke=invoke)
    enable(env, channels=["sms"])
    phone = "+46701234567"
    detail = visit(email="")
    boto3.resource("dynamodb", region_name=REGION).Table(RES_TABLE).update_item(
        Key={"PK": f"LOCATION#{LOC_A}", "SK": "RESERVATION#res-1"}, UpdateExpression="SET customerPhone = :p",
        ExpressionAttributeValues={":p": phone})
    consent.grant(boto3.resource("dynamodb", region_name=REGION).Table(CONSENT_TABLE), tenant_id=TENANT_A,
                  restaurant="Bistro", version=consent.CURRENT, channels=["sms"], phone=phone, location_id=LOC_A,
                  source="booking", ref="res-1")
    env.jobs.schedule_review({**detail, "guestKey": consent.subject(TENANT_A, "sms", phone),
                              "channels": ["sms"]})
    with pytest.raises(ClientError):
        fire(env)
    assert not any(t == "ReviewRequest.Sent" for t, _ in env.published)
    assert fire(env) == "sent"  # the retry is neither "done" nor "skipped:recent"
    assert len(invoked) == 2
    assert fire(env) == "done"


def test_review_skipped_without_consent(env):
    enable(env)
    env.jobs.schedule_review(visit())
    assert fire(env) == "skipped:no_consent"
    assert outbox() == []


def test_review_skipped_when_recent(env):
    enable(env)
    grant()
    env.jobs.schedule_review(visit())
    env.jobs.schedule_review(visit(kind="order", source_id="ord-1"))
    assert fire(env) == "sent"
    assert fire(env, kind="order", source_id="ord-1") == "skipped:recent"
    assert len(outbox()) == 1


def test_review_skipped_for_erased_guest_and_no_show(env):
    enable(env)
    grant()
    env.jobs.schedule_review(visit())
    env.res.Table(RES_TABLE).update_item(Key={"PK": f"LOCATION#{LOC_A}", "SK": "RESERVATION#res-1"},
                                         UpdateExpression="SET anonymisedAt = :a REMOVE customerEmail",
                                         ExpressionAttributeValues={":a": "2026-11-04T12:00:00Z"})
    assert fire(env) == "skipped:no_contact"
    env.jobs.schedule_review(visit(source_id="res-2"))
    env.res.Table(RES_TABLE).update_item(Key={"PK": f"LOCATION#{LOC_A}", "SK": "RESERVATION#res-2"},
                                         UpdateExpression="SET #s = :n", ExpressionAttributeNames={"#s": "status"},
                                         ExpressionAttributeValues={":n": "no_show"})
    assert fire(env, source_id="res-2") == "skipped:no_show"


def test_review_disabled_settings_and_off_tenant_do_nothing(env):
    connect(env)
    assert env.jobs.schedule_review(visit()) == "disabled"
    enable_settings = settings_body()
    call(env, "PUT", SETTINGS, params={"locationId": LOC_A}, body=enable_settings)
    turn_off()
    assert env.jobs.schedule_review(visit()) == "ignored"
    entitle(TENANT_A, "googleBusiness")
    assert env.jobs.schedule_review(visit()) == "scheduled"
    turn_off()
    assert fire(env) == "skipped:disabled"


def test_review_event_for_other_tenants_location_is_ignored(env):
    enable(env)
    forged = visit(loc=LOC_B)
    forged["tenantId"] = TENANT_A
    assert env.jobs.schedule_review(forged) == "ignored"
    bad_ref = visit()
    bad_ref["contactRef"]["pk"] = f"LOCATION#{LOC_B}"
    assert env.jobs.schedule_review(bad_ref) == "ignored"


def test_review_schedule_resumes_after_failed_create(env, monkeypatch):
    enable(env)
    real = gbp.create_schedule
    monkeypatch.setattr(gbp, "create_schedule", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        env.jobs.schedule_review(visit())
    monkeypatch.setattr(gbp, "create_schedule", real)
    assert env.jobs.schedule_review(visit()) == "scheduled"  # EventBridge retry creates it


def test_same_guest_at_two_tenants_has_two_revsent_keys(env):
    enable(env)
    enable(env, tenant=TENANT_B, loc=LOC_B)
    grant()
    grant(tenant=TENANT_B)
    env.jobs.schedule_review(visit())
    env.jobs.schedule_review(visit(tenant=TENANT_B, loc=LOC_B, source_id="res-b"))
    assert fire(env) == "sent"
    assert fire(env, tenant=TENANT_B, loc=LOC_B, source_id="res-b") == "sent"
    keys = [i["PK"] for i in env.res.Table(GBP_TABLE).scan()["Items"] if i["SK"].startswith("REVSENT#")]
    assert sorted(keys) == sorted([f"TENANT#{TENANT_A}", f"TENANT#{TENANT_B}"])


# --- hooks -----------------------------------------------------------------------------

def test_platform_hook_d11(env):
    connect(env)
    link(env)
    answer = env.api.handler({"hook": "platform", "op": "google-business.tenant.get", "tenantId": TENANT_A})
    assert answer["ok"] and answer["body"]["status"] == "connected"
    assert [lnk["locationId"] for lnk in answer["body"]["links"]] == [LOC_A]
    missing = env.api.handler({"hook": "platform", "op": "google-business.tenant.get", "tenantId": "nope"})
    assert missing["status"] == 404
    assert env.api.handler({"hook": "platform", "op": "x", "tenantId": TENANT_A})["code"] == "unknown_op"


def test_platform_hook_d11_never_connected(env, monkeypatch):
    """Never connected: status disconnected, no links, and no location table
    read (the role has no Query on it), so no FunctionError and no 503."""
    monkeypatch.setattr(shared_tenant, "list_locations", lambda *_: pytest.fail("location table read"))
    answer = env.api.handler({"hook": "platform", "op": "google-business.tenant.get", "tenantId": TENANT_A})
    assert answer == {"ok": True, "status": 200, "body": {"status": "disconnected", "links": []}}


def test_privacy_export_and_erase(env):
    enable(env)
    grant()
    env.jobs.schedule_review(visit())
    fire(env)
    env.jobs.schedule_review(visit(source_id="res-2"))
    subject = consent.subject(TENANT_A, "email", EMAIL)
    exported = env.api.handler({"hook": "privacy.export", "tenantId": TENANT_A, "subjects": {"email": subject}})
    assert exported["ok"] and {i["kind"] for i in exported["items"]} == {"review_request_last_sent", "review_request"}
    assert EMAIL not in json.dumps(exported)
    erased = env.api.handler({"hook": "privacy.erase", "tenantId": TENANT_A, "subjects": {"email": subject}})
    assert erased["erased"] == {"reviewSent": 1, "reviewJobs": 2}
    # the pending job's schedule is gone (a fired one deletes itself: ActionAfterCompletion=DELETE)
    names = [s["Name"] for s in boto3.client("scheduler").list_schedules(GroupName=GROUP)["Schedules"]]
    assert "gbp-review-booking-res-2" not in names
    # B's same subject is untouched (own keys per tenant)
    assert env.api.handler({"hook": "privacy.erase", "tenantId": TENANT_B,
                            "subjects": {"email": subject}})["erased"] == {"reviewSent": 0, "reviewJobs": 0}


def test_tenant_purge_removes_rows_secret_and_schedules(env):
    enable(env)
    enable(env, tenant=TENANT_B, loc=LOC_B)
    env.jobs.schedule_review(visit())
    answer = env.api.handler({"hook": "tenant.purge", "tenantId": TENANT_A, "locationIds": [LOC_A, LOC_A2],
                              "round": 1})
    assert answer["ok"] and answer["done"]
    rows = env.res.Table(GBP_TABLE).scan()["Items"]
    assert rows and all(r.get("tenantId") == TENANT_B for r in rows)
    assert gbp.get_refresh_token(TENANT_A) is None and gbp.get_refresh_token(TENANT_B)
    assert boto3.client("scheduler").list_schedules(GroupName=GROUP)["Schedules"] == []


def test_unknown_hook_and_event(env):
    assert env.api.handler({"hook": "nope", "tenantId": TENANT_A})["code"] == "unknown_hook"
    assert env.api.handler({"foo": 1})["code"] == "unknown_event"
    assert env.jobs.handler({"foo": 1}) == {"result": "ignored"}


# --- isolation and client -------------------------------------------------------------

def test_two_tenant_isolation(env):
    enable(env)
    enable(env, tenant=TENANT_B, loc=LOC_B)
    for method, path, body in (("GET", LOC, None), ("PUT", LINK, {"googleLocation": "locations/sim-2"}),
                               ("POST", SYNC, {}), ("PUT", SETTINGS, settings_body(version=1))):
        assert call(env, method, path, params={"locationId": LOC_B}, body=body)[0] == 404
    assert gbp.get_link(TENANT_A, LOC_B) is None
    secret_reads = []
    real = gbp.get_refresh_token

    def gbp_get(tenant_id):
        secret_reads.append(tenant_id)
        return real(tenant_id)
    gbp.get_refresh_token = gbp_get
    try:
        call(env, "POST", SYNC, params={"locationId": LOC_A}, body={})
    finally:
        gbp.get_refresh_token = real
    assert secret_reads == [TENANT_A]


def test_simulator_refused_in_prod(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.delenv("GOOGLE_BUSINESS_CLIENT", raising=False)
    with pytest.raises(RuntimeError):
        external_client.mode("google_business")


def test_schedule_name_is_valid_for_long_ids():
    name = gbp.schedule_name("order", "x" * 80)
    assert len(name) <= 64 and re.fullmatch(r"[0-9A-Za-z_.-]+", name)
    assert gbp.schedule_name("booking", "res-1") == "gbp-review-booking-res-1"

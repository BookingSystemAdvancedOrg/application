"""Guest CRM (feature C, ADR-C, CONTRACTS.md 6.C): routes C1-C11, the
stream sync, hooks, erase and the two-tenant isolation suite."""

import importlib.util
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from boto3.dynamodb.types import TypeSerializer
from moto import mock_aws

from shared import consent, events
from shared import marketing as mkt
from shared import tenant as shared_tenant
from shared.features import guest_crm as crm
from tenant_support import (LOC_A, LOC_A2, LOC_B, REGION, TENANT_A, TENANT_B,
                            features_on, seed_tenancy, tenant_row, with_tenant_claims)

ROOT = Path(__file__).parents[1] / "functions"
CRM_TABLE, RES_TABLE, ORD_TABLE, CAT_TABLE = "test-guest-crm", "test-reservation", "test-order", "test-catering"
CONSENT_TABLE, MKT_TABLE = "test-consent", "test-marketing"
SEG = "01SEGMENTAAAAAAAAAAAAAAAAA"
EXP = "01EXPORTAAAAAAAAAAAAAAAAAA"
RUN = "01RUNAAAAAAAAAAAAAAAAAAAAA"


def load(name):
    spec = importlib.util.spec_from_file_location(f"crm_{name.replace('-', '_')}", ROOT / name / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create(ddb, name, gsis=(), extra=()):
    ddb.create_table(
        TableName=name, BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("PK", "SK", *extra)],
        **({"GlobalSecondaryIndexes": list(gsis)} if gsis else {}))


@pytest.fixture
def env(monkeypatch):
    with mock_aws():
        for key, value in {"AWS_DEFAULT_REGION": REGION, "ENVIRONMENT": "dev", "GUEST_CRM_TABLE_NAME": CRM_TABLE,
                           "RESERVATION_TABLE_NAME": RES_TABLE, "ORDER_TABLE_NAME": ORD_TABLE,
                           "CATERING_TABLE_NAME": CAT_TABLE, "CONSENT_TABLE_NAME": CONSENT_TABLE,
                           "MARKETING_TABLE_NAME": MKT_TABLE, "USER_TABLE_NAME": "test-user",
                           "AWS_LAMBDA_FUNCTION_NAME": "dev-guest-crm"}.items():
            monkeypatch.setenv(key, value)
        seed_tenancy(monkeypatch, features_a=features_on("guestCrm"), features_b=features_on("guestCrm"))
        ddb = boto3.client("dynamodb", region_name=REGION)
        _create(ddb, CRM_TABLE, extra=("GSI1PK", "GSI1SK"), gsis=[{
            "IndexName": crm.GSI, "Projection": {"ProjectionType": "ALL"},
            "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"},
                          {"AttributeName": "GSI1SK", "KeyType": "RANGE"}]}])
        for name in (RES_TABLE, ORD_TABLE, CAT_TABLE, MKT_TABLE):
            _create(ddb, name)
        _create(ddb, CONSENT_TABLE, extra=("tenantId", "at"), gsis=[{
            "IndexName": "byTenant", "Projection": {"ProjectionType": "ALL"},
            "KeySchema": [{"AttributeName": "tenantId", "KeyType": "HASH"},
                          {"AttributeName": "at", "KeyType": "RANGE"}]}])
        published = []
        monkeypatch.setattr(events, "publish", lambda f, t, d: published.append((f, t, d)) or "id")
        invoked = []
        app = load("guest-crm")
        monkeypatch.setattr(app, "_invoke_self", lambda payload: invoked.append(payload))
        yield SimpleNamespace(app=app, sync=load("guest-crm-sync"), published=published, invoked=invoked,
                              res=boto3.resource("dynamodb", region_name=REGION))


# --- helpers ---------------------------------------------------------------------------

def call(e, method, path, *, tenant=TENANT_A, role="owner_user", body=None, query=None, params=None):
    params = dict(params or {})
    template = path
    for name, value in params.items():  # route keys carry the template, as API Gateway sends them
        template = template.replace(f"/{value}", f"/{{{name}}}")
    event = {"routeKey": f"{method} {template}", "pathParameters": params,
             "queryStringParameters": query, "body": json.dumps(body) if body is not None else None}
    with_tenant_claims(event, tenant, role)
    response = e.app.handler(event, None)
    return response["statusCode"], (json.loads(response["body"]) if response.get("body") else None)


def booking(rid, *, loc=LOC_A, email="anna@example.se", phone=None, status="arrived", when="2026-10-01T18:00:00Z",
            name="Anna Andersson", party=2):
    day = when[:10]
    item = {"PK": f"LOCATION#{loc}", "SK": f"RESERVATION#{day}#{rid}", "reservationId": rid, "locationId": loc,
            "date": day, "bookedFor": when, "status": status, "customerName": name, "partySize": party}
    if email:
        item["customerEmail"] = email
    if phone:
        item["customerPhone"] = phone
    return item


def order(oid, *, loc=LOC_A, email="anna@example.se", status="completed", total=25000, when="2026-10-02T12:00:00Z"):
    return {"PK": f"LOCATION#{loc}", "SK": f"ORDER#{when[:10]}#{oid}", "orderId": oid, "locationId": loc,
            "date": when[:10], "createdAt": when, "status": status, "paymentStatus": "paid",
            "totalAmount": total, "customerName": "Anna Andersson", "customerEmail": email}


_ser = TypeSerializer()


def stream(e, table_name, item, event_name="MODIFY", seq="1"):
    """Writes the source row and delivers it as a stream record."""
    e.res.Table(table_name).put_item(Item=item)
    record = {"eventID": seq, "eventName": event_name,
              "eventSourceARN": f"arn:aws:dynamodb:{REGION}:123456789012:table/{table_name}/stream/2026",
              "dynamodb": {"SequenceNumber": seq, "NewImage": {k: _ser.serialize(v) for k, v in item.items()}}}
    return e.sync.handler({"Records": [record]}, None)


def profile_of(e, tenant_id, email):
    gid = crm.guest_for(tenant_id, consent.EMAIL, consent.subject(tenant_id, consent.EMAIL, email))
    return crm.get_profile(tenant_id, gid) if gid else None


def all_items(e, tenant_id):
    return e.res.Table(CRM_TABLE).query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("PK").eq(f"TENANT#{tenant_id}"))["Items"]


def guest_crm_off(e, tenant_id=TENANT_A):
    e.res.Table("test-tenant").put_item(Item=tenant_row(tenant_id, features=features_on()))
    shared_tenant.reset_caches()


def subscribe(e, tenant_id, email, *, granted=True):
    sid = mkt.subscriber_id(tenant_id, email)
    e.res.Table(MKT_TABLE).put_item(Item={**mkt.sub_key(tenant_id, sid), "status": "subscribed"})
    if granted:
        consent.grant(e.res.Table(CONSENT_TABLE), tenant_id=tenant_id, restaurant="R", version=consent.CURRENT,
                      channels=[consent.EMAIL], email=email, source="booking")
    return sid


# --- sync -----------------------------------------------------------------------------

def test_arrived_booking_creates_a_profile_without_plaintext_contact(env):
    assert stream(env, RES_TABLE, booking("r1", phone="+46701111111")) == {"batchItemFailures": []}
    profile = profile_of(env, TENANT_A, "anna@example.se")
    assert profile["visits"] == 1 and profile["name"] == "Anna Andersson"
    assert profile["contactRef"] == {"table": "reservation", "pk": f"LOCATION#{LOC_A}",
                                     "sk": "RESERVATION#2026-10-01#r1"}
    dump = json.dumps(all_items(env, TENANT_A), default=str)
    assert "anna@example.se" not in dump and "+46701111111" not in dump and "46701111111" not in dump
    (feature, kind, detail), = env.published
    assert (feature, kind) == ("guest-crm", "GuestVisit.Recorded")
    assert detail["returning"] is False and detail["contactRef"]["table"] == "reservation"
    assert detail["guestKey"] == consent.subject(TENANT_A, consent.EMAIL, "anna@example.se")
    assert sorted(detail["channels"]) == ["email", "sms"]
    assert "anna" not in json.dumps(detail).lower()


def test_replay_and_arrived_undo_arrived_count_once(env):
    stream(env, RES_TABLE, booking("r1"), seq="1")
    stream(env, RES_TABLE, booking("r1"), seq="2")
    stream(env, RES_TABLE, booking("r1", status="reserved"), seq="3")
    stream(env, RES_TABLE, booking("r1"), seq="4")
    assert profile_of(env, TENANT_A, "anna@example.se")["visits"] == 1
    assert len(env.published) == 1


def test_not_counted_statuses_are_ignored(env):
    stream(env, RES_TABLE, booking("r1", status="reserved"))
    stream(env, ORD_TABLE, {**order("o1", status="pending_payment"), "paymentStatus": "processing"})
    assert profile_of(env, TENANT_A, "anna@example.se") is None and env.published == []


def test_no_show_is_counted_separately_and_emits_nothing(env):
    stream(env, RES_TABLE, booking("r1", status="no_show"))
    profile = profile_of(env, TENANT_A, "anna@example.se")
    assert profile["noShows"] == 1 and profile["visits"] == 0 and env.published == []


def test_second_visit_is_returning_and_order_spend_adds_up(env):
    stream(env, RES_TABLE, booking("r1"))
    stream(env, ORD_TABLE, order("o1"))
    profile = profile_of(env, TENANT_A, "anna@example.se")
    assert profile["visits"] == 1 and profile["orders"] == 1 and profile["spendOre"]["orders"] == 25000
    assert env.published[-1][2]["returning"] is True


def test_email_and_phone_on_two_profiles_merge_into_the_older_idempotently(env):
    stream(env, RES_TABLE, booking("r1", email="anna@example.se", when="2026-09-01T18:00:00Z"))
    stream(env, RES_TABLE, booking("r2", email=None, phone="+46702222222", when="2026-09-05T18:00:00Z"))
    first = profile_of(env, TENANT_A, "anna@example.se")
    stream(env, RES_TABLE, booking("r3", email="anna@example.se", phone="+46702222222"))
    stream(env, RES_TABLE, booking("r3", email="anna@example.se", phone="+46702222222"), seq="9")
    merged = profile_of(env, TENANT_A, "anna@example.se")
    assert merged["guestId"] == first["guestId"] and merged["visits"] == 3
    by_phone = crm.guest_for(TENANT_A, consent.SMS, consent.subject(TENANT_A, consent.SMS, "+46702222222"))
    assert by_phone == first["guestId"]
    profiles = [i for i in all_items(env, TENANT_A) if i["SK"].startswith("PROFILE#")]
    assert len(profiles) == 1


def test_tenant_without_guest_crm_writes_nothing(env):
    guest_crm_off(env)
    stream(env, RES_TABLE, booking("r1"))
    assert all_items(env, TENANT_A) == [] and env.published == []


def test_google_business_only_tenant_gets_events_but_no_profile(env):
    env.res.Table("test-tenant").put_item(Item=tenant_row(TENANT_A, features=features_on("googleBusiness")))
    shared_tenant.reset_caches()
    stream(env, RES_TABLE, booking("r1"))
    assert all_items(env, TENANT_A) == []
    assert env.published[0][1] == "GuestVisit.Recorded" and env.published[0][2]["returning"] is False


def test_a_failing_record_is_reported_and_others_still_apply(env, monkeypatch):
    calls = []
    real = crm.record_source

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real(*a, **k)
    monkeypatch.setattr(crm, "record_source", flaky)
    item = booking("r1")
    env.res.Table(RES_TABLE).put_item(Item=item)
    rec = {"eventID": "1", "eventName": "MODIFY",
           "eventSourceARN": f"arn:aws:dynamodb:{REGION}:1:table/{RES_TABLE}/stream/x",
           "dynamodb": {"SequenceNumber": "11", "NewImage": {k: _ser.serialize(v) for k, v in item.items()}}}
    rec2 = {**rec, "eventID": "2", "dynamodb": {**rec["dynamodb"], "SequenceNumber": "12"}}
    assert env.sync.handler({"Records": [rec, rec2]}, None) == {"batchItemFailures": [{"itemIdentifier": "11"}]}
    assert profile_of(env, TENANT_A, "anna@example.se")["visits"] == 1


# --- C1 list --------------------------------------------------------------------------

def test_c1_list_newest_first_with_cursor(env, monkeypatch):
    monkeypatch.setattr(env.app, "PAGE", 1)
    stream(env, RES_TABLE, booking("r1", email="a@x.se", when="2026-09-01T18:00:00Z"))
    stream(env, RES_TABLE, booking("r2", email="b@x.se", when="2026-10-01T18:00:00Z", name="Bo"))
    status, body = call(env, "GET", "/tenant/guests")
    assert status == 200 and [g["name"] for g in body["guests"]] == ["Bo"] and body["cursor"]
    status, body2 = call(env, "GET", "/tenant/guests", query={"cursor": body["cursor"]})
    assert status == 200 and [g["name"] for g in body2["guests"]] == ["Anna Andersson"]


def test_c1_bad_cursor_and_foreign_cursor_are_400(env):
    assert call(env, "GET", "/tenant/guests", query={"cursor": "!!"})[0] == 400
    import base64
    foreign = base64.urlsafe_b64encode(json.dumps({"PK": f"TENANT#{TENANT_B}", "SK": "PROFILE#x",
                                                   "GSI1PK": f"TENANT#{TENANT_B}#GUESTS",
                                                   "GSI1SK": "x"}).encode()).decode()
    assert call(env, "GET", "/tenant/guests", query={"cursor": foreign})[0] == 400


def test_c1_staff_sees_only_own_location(env):
    stream(env, RES_TABLE, booking("r1", email="a@x.se", loc=LOC_A))
    stream(env, RES_TABLE, booking("r2", email="b@x.se", loc=LOC_A2, name="Bo"))
    status, body = call(env, "GET", "/tenant/guests", role="staff_user")
    assert status == 200 and [g["name"] for g in body["guests"]] == ["Anna Andersson"]
    assert call(env, "GET", "/tenant/guests", role="staff_user", query={"locationId": LOC_A2})[0] == 404
    assert call(env, "GET", "/tenant/guests", query={"locationId": LOC_B})[0] == 404


def test_c1_location_filter_fills_the_page_past_filtered_out_guests(env, monkeypatch):
    monkeypatch.setattr(env.app, "PAGE", 2)
    stream(env, RES_TABLE, booking("r1", email="a1@x.se", loc=LOC_A, when="2026-09-01T18:00:00Z", name="A1"))
    stream(env, RES_TABLE, booking("r2", email="a2@x.se", loc=LOC_A, when="2026-09-02T18:00:00Z", name="A2"))
    stream(env, RES_TABLE, booking("r3", email="a3@x.se", loc=LOC_A, when="2026-09-03T18:00:00Z", name="A3"))
    for n in range(4):  # the newest guests are all at the other location
        stream(env, RES_TABLE, booking(f"o{n}", email=f"o{n}@x.se", loc=LOC_A2, when=f"2026-10-0{n + 1}T18:00:00Z",
                                       name=f"O{n}"))
    status, body = call(env, "GET", "/tenant/guests", query={"locationId": LOC_A})
    assert status == 200 and [g["name"] for g in body["guests"]] == ["A3", "A2"] and body["cursor"]
    status, body = call(env, "GET", "/tenant/guests", query={"locationId": LOC_A, "cursor": body["cursor"]})
    assert status == 200 and [g["name"] for g in body["guests"]] == ["A1"]
    assert body["cursor"] is None


def test_c1_off_is_403(env):
    guest_crm_off(env)
    status, body = call(env, "GET", "/tenant/guests")
    assert status == 403 and body["error"] == "feature_not_in_plan"


# --- C2 search ------------------------------------------------------------------------

def test_c2_search_by_email_phone_and_name(env):
    stream(env, RES_TABLE, booking("r1", phone="+46701111111"))
    for body in ({"email": "ANNA@example.se "}, {"phone": "+46 70 111 11 11"}, {"name": "ann"}):
        status, out = call(env, "POST", "/tenant/guests/search", body=body)
        assert status == 200 and len(out["guests"]) == 1, body


def test_c2_two_keys_is_400_and_unknown_field_400(env):
    assert call(env, "POST", "/tenant/guests/search", body={"email": "a@x.se", "name": "Anna"})[0] == 400
    assert call(env, "POST", "/tenant/guests/search", body={"x": 1})[0] == 400


def test_c2_other_tenants_email_finds_nothing_and_staff_is_scoped(env):
    stream(env, RES_TABLE, booking("r1", loc=LOC_B, email="b@x.se"))
    assert call(env, "POST", "/tenant/guests/search", body={"email": "b@x.se"})[1]["guests"] == []
    stream(env, RES_TABLE, booking("r2", loc=LOC_A2, email="c@x.se"))
    assert call(env, "POST", "/tenant/guests/search", role="staff_user", body={"email": "c@x.se"})[1]["guests"] == []


def test_c2_off_is_403(env):
    guest_crm_off(env)
    assert call(env, "POST", "/tenant/guests/search", body={"name": "Anna"})[0] == 403


# --- C3 profile -----------------------------------------------------------------------

def test_c3_owner_gets_history_contact_consent_and_amounts(env):
    stream(env, RES_TABLE, booking("r1", phone="+46701111111"))
    stream(env, ORD_TABLE, order("o1"))
    subscribe(env, TENANT_A, "anna@example.se")
    gid = profile_of(env, TENANT_A, "anna@example.se")["guestId"]
    status, body = call(env, "GET", f"/tenant/guests/{gid}", params={"guestId": gid})
    assert status == 200 and body["spendSek"]["orders"] == 250
    assert [h["kind"] for h in body["history"]] == ["order", "booking"]
    assert body["history"][0]["amountOre"] == 25000
    assert body["contact"] == {"email": "anna@example.se"}
    assert body["consent"] == {"email": "granted", "sms": "none"}


def test_c3_staff_no_amounts_and_other_location_404(env):
    stream(env, RES_TABLE, booking("r1"))
    gid = profile_of(env, TENANT_A, "anna@example.se")["guestId"]
    status, body = call(env, "GET", f"/tenant/guests/{gid}", role="staff_user", params={"guestId": gid})
    assert status == 200 and "spendSek" not in body and "amountOre" not in body["history"][0]
    stream(env, RES_TABLE, booking("r2", loc=LOC_A2, email="z@x.se"))
    other = profile_of(env, TENANT_A, "z@x.se")["guestId"]
    assert call(env, "GET", f"/tenant/guests/{other}", role="staff_user", params={"guestId": other})[0] == 404


def test_c3_contact_disappears_once_the_source_row_is_anonymised(env):
    stream(env, RES_TABLE, booking("r1"))
    gid = profile_of(env, TENANT_A, "anna@example.se")["guestId"]
    env.res.Table(RES_TABLE).update_item(Key={"PK": f"LOCATION#{LOC_A}", "SK": "RESERVATION#2026-10-01#r1"},
                                         UpdateExpression="REMOVE customerEmail")
    assert call(env, "GET", f"/tenant/guests/{gid}", params={"guestId": gid})[1]["contact"] == {}


def test_c3_foreign_and_unknown_guest_404_and_off_403(env):
    stream(env, RES_TABLE, booking("r1", loc=LOC_B))
    gid_b = profile_of(env, TENANT_B, "anna@example.se")["guestId"]
    assert call(env, "GET", f"/tenant/guests/{gid_b}", params={"guestId": gid_b})[0] == 404
    assert call(env, "GET", "/tenant/guests/nope", params={"guestId": "nope"})[0] == 404
    guest_crm_off(env)
    assert call(env, "GET", f"/tenant/guests/{gid_b}", params={"guestId": gid_b})[0] == 403


# --- C4 patch -------------------------------------------------------------------------

def test_c4_patch_and_stale_version(env):
    stream(env, RES_TABLE, booking("r1"))
    gid = profile_of(env, TENANT_A, "anna@example.se")["guestId"]
    status, body = call(env, "PATCH", f"/tenant/guests/{gid}", params={"guestId": gid},
                        body={"note": "Fönsterbord", "tags": ["stamgäst"], "birthday": "02-29",
                              "segmentsExcluded": True, "version": 1})
    assert status == 200 and body["note"] == "Fönsterbord" and body["version"] == 2 and body["birthday"] == "02-29"
    status, body = call(env, "PATCH", f"/tenant/guests/{gid}", params={"guestId": gid},
                        body={"note": "x", "version": 1})
    assert status == 409 and body["error"] == "changed_retry"


def test_c4_validation_role_foreign_off(env):
    stream(env, RES_TABLE, booking("r1"))
    stream(env, RES_TABLE, booking("r9", loc=LOC_B))
    gid = profile_of(env, TENANT_A, "anna@example.se")["guestId"]
    gid_b = profile_of(env, TENANT_B, "anna@example.se")["guestId"]
    p = {"guestId": gid}
    assert call(env, "PATCH", f"/tenant/guests/{gid}", params=p, body={"birthday": "02-30", "version": 1})[1][
        "error"] == "invalid_birthday"
    assert call(env, "PATCH", f"/tenant/guests/{gid}", params=p, body={"note": "x" * 301, "version": 1})[1][
        "error"] == "note_too_long"
    assert call(env, "PATCH", f"/tenant/guests/{gid}", params=p, role="staff_user",
                body={"note": "x", "version": 1})[0] == 403
    assert call(env, "PATCH", f"/tenant/guests/{gid_b}", params={"guestId": gid_b},
                body={"note": "x", "version": 1})[0] == 404
    assert crm.get_profile(TENANT_B, gid_b).get("note") is None
    guest_crm_off(env)
    assert call(env, "PATCH", f"/tenant/guests/{gid}", params=p, body={"note": "x", "version": 1})[0] == 403


# --- C5 lookup ------------------------------------------------------------------------

def test_c5_new_and_returning(env):
    stream(env, RES_TABLE, booking("r1"))
    status, body = call(env, "POST", f"/locations/{LOC_A}/guests/lookup", params={"locationId": LOC_A},
                        body={"contacts": [{"key": "r1", "email": "anna@example.se"},
                                           {"key": "r2", "phone": "+46709999999"}]})
    assert status == 200 and body["results"]["r1"]["status"] == "returning"
    assert body["results"]["r1"]["visits"] == 1 and body["results"]["r2"] == {"status": "new", "visits": 0}


def test_c5_limits_scope_and_off(env):
    p = {"locationId": LOC_A}
    many = [{"key": str(i), "email": f"{i}@x.se"} for i in range(101)]
    assert call(env, "POST", f"/locations/{LOC_A}/guests/lookup", params=p, body={"contacts": many})[1][
        "error"] == "too_many_contacts"
    assert call(env, "POST", f"/locations/{LOC_A2}/guests/lookup", params={"locationId": LOC_A2},
                role="staff_user", body={"contacts": []})[0] == 404
    assert call(env, "POST", f"/locations/{LOC_B}/guests/lookup", params={"locationId": LOC_B},
                body={"contacts": []})[0] == 404
    guest_crm_off(env)
    assert call(env, "POST", f"/locations/{LOC_A}/guests/lookup", params=p, body={"contacts": []})[0] == 403


def test_c5_does_not_see_other_tenants_guest(env):
    stream(env, RES_TABLE, booking("r1", loc=LOC_B))
    body = call(env, "POST", f"/locations/{LOC_A}/guests/lookup", params={"locationId": LOC_A},
                body={"contacts": [{"key": "k", "email": "anna@example.se"}]})[1]
    assert body["results"]["k"]["status"] == "new"


# --- C6-C10 segments ------------------------------------------------------------------

def seg_path(segment_id=SEG, suffix=""):
    return f"/tenant/guest-segments/{{segmentId}}{suffix}", {"segmentId": segment_id}


def put_seg(e, rules, *, name="Stamgäster", version=None, tenant=TENANT_A, segment_id=SEG, role="owner_user"):
    path, params = seg_path(segment_id)
    body = {"name": name, "rules": rules, **({"version": version} if version is not None else {})}
    return call(e, "PUT", path, params=params, body=body, tenant=tenant, role=role)


def test_c6_lists_segments_and_templates_owner_only(env):
    put_seg(env, {"minVisits": 2})
    status, body = call(env, "GET", "/tenant/guest-segments")
    assert status == 200 and [s["segmentId"] for s in body["segments"]] == [SEG] and len(body["templates"]) == 4
    assert call(env, "GET", "/tenant/guest-segments", role="staff_user")[0] == 403
    assert call(env, "GET", "/tenant/guest-segments", tenant=TENANT_B)[1]["segments"] == []
    guest_crm_off(env)
    assert call(env, "GET", "/tenant/guest-segments")[0] == 403


def test_c7_create_retry_update_and_conflicts(env):
    status, body = put_seg(env, {"lapsedDays": 90})
    assert status == 200 and body["version"] == 1
    assert put_seg(env, {"lapsedDays": 90}) == (200, body)               # retried create
    assert put_seg(env, {"lapsedDays": 60})[0] == 409                   # changed without version
    status, body = put_seg(env, {"lapsedDays": 60}, version=1)
    assert status == 200 and body["version"] == 2 and body["rules"] == {"lapsedDays": 60}
    assert put_seg(env, {"lapsedDays": 30}, version=1)[1]["error"] == "changed_retry"


def test_c7_validation_role_foreign_off(env):
    assert put_seg(env, {"lapsedDays": 3})[1]["error"] == "invalid_rules"
    assert put_seg(env, {"locationId": LOC_B})[1]["error"] == "invalid_rules"
    assert put_seg(env, {"bogus": 1})[0] == 400
    assert put_seg(env, {"minVisits": 2}, role="staff_user")[0] == 403
    put_seg(env, {"minVisits": 2}, tenant=TENANT_B)
    status, body = put_seg(env, {"minVisits": 3}, version=1)    # same id in A is A's own new segment
    assert status == 200 and body["version"] == 1
    assert env.res.Table(CRM_TABLE).get_item(Key=crm.segment_key(TENANT_B, SEG))["Item"]["rules"] == {"minVisits": 2}
    guest_crm_off(env)
    assert put_seg(env, {"minVisits": 2})[0] == 403


def test_c8_delete_twice_is_204_and_foreign_untouched(env):
    put_seg(env, {"minVisits": 2}, tenant=TENANT_B)
    path, params = seg_path()
    assert call(env, "DELETE", path, params=params)[0] == 204
    assert call(env, "DELETE", path, params=params)[0] == 204
    assert env.res.Table(CRM_TABLE).get_item(Key=crm.segment_key(TENANT_B, SEG)).get("Item")
    assert call(env, "DELETE", path, params=params, role="staff_user")[0] == 403
    guest_crm_off(env)
    assert call(env, "DELETE", path, params=params)[0] == 403


def _seed_segment_guests(env):
    stream(env, RES_TABLE, booking("r1", email="a@x.se", when="2026-01-01T18:00:00Z"))
    stream(env, RES_TABLE, booking("r2", email="b@x.se", when="2026-01-02T18:00:00Z"))
    stream(env, RES_TABLE, booking("r3", email="c@x.se", when="2026-01-03T18:00:00Z"))
    stream(env, RES_TABLE, booking("r4", email="d@x.se", when="2026-01-04T18:00:00Z"))
    subscribe(env, TENANT_A, "a@x.se")                          # reachable
    subscribe(env, TENANT_A, "b@x.se", granted=False)           # subscribed but no consent
    sid_c = subscribe(env, TENANT_A, "c@x.se")
    consent.withdraw(env.res.Table(CONSENT_TABLE), tenant_id=TENANT_A, channel=consent.EMAIL,
                     identifier="c@x.se", source="admin")       # withdrawn
    gid_d = subscribe(env, TENANT_A, "d@x.se") and profile_of(env, TENANT_A, "d@x.se")["guestId"]
    call(env, "PATCH", f"/tenant/guests/{gid_d}", params={"guestId": gid_d},
         body={"segmentsExcluded": True, "version": 1})         # objected
    return sid_c


def test_c9_preview_counts_total_and_reachable(env):
    _seed_segment_guests(env)
    put_seg(env, {"lapsedDays": 30})
    path, params = seg_path(suffix="/preview")
    status, body = call(env, "GET", path, params=params)
    assert status == 200 and body["total"] == 3 and body["reachable"] == 1 and len(body["sample"]) == 3
    assert call(env, "GET", path, params=params, role="staff_user")[0] == 403
    assert call(env, "GET", path, params=params, tenant=TENANT_B)[0] == 404
    guest_crm_off(env)
    assert call(env, "GET", path, params=params)[0] == 403


def test_c10_export_holds_only_consented_subscribers_and_is_never_recomputed(env):
    _seed_segment_guests(env)
    put_seg(env, {"lapsedDays": 30})
    path, params = seg_path(suffix="/exports")
    status, body = call(env, "POST", path, params=params, body={"exportId": EXP})
    assert (status, body) == (200, {"exportId": EXP, "count": 1})
    stream(env, RES_TABLE, booking("r5", email="e@x.se", when="2026-01-05T18:00:00Z"))
    subscribe(env, TENANT_A, "e@x.se")
    assert call(env, "POST", path, params=params, body={"exportId": EXP})[1]["count"] == 1
    assert [p[1] for p in env.published].count("GuestSegment.Exported") == 1
    page = env.app.handler({"hook": "marketing.segment", "hookVersion": 1, "tenantId": TENANT_A, "op": "page",
                            "exportId": EXP})
    assert page == {"ok": True, "subscriberIds": [mkt.subscriber_id(TENANT_A, "a@x.se")]}
    assert env.app.handler({"hook": "marketing.segment", "hookVersion": 1, "tenantId": TENANT_A,
                            "op": "describe", "exportId": EXP}) == {"ok": True, "count": 1}
    assert env.app.handler({"hook": "marketing.segment", "hookVersion": 1, "tenantId": TENANT_B,
                            "op": "describe", "exportId": EXP})["ok"] is False


def test_c10_validation_role_foreign_off(env):
    put_seg(env, {"minVisits": 1})
    path, params = seg_path(suffix="/exports")
    assert call(env, "POST", path, params=params, body={"exportId": EXP, "x": 1})[0] == 400
    assert call(env, "POST", path, params=params, body={"exportId": "a"})[0] == 400
    assert call(env, "POST", path, params=params, role="staff_user", body={"exportId": EXP})[0] == 403
    assert call(env, "POST", path, params=params, tenant=TENANT_B, body={"exportId": EXP})[0] == 404
    guest_crm_off(env)
    assert call(env, "POST", path, params=params, body={"exportId": EXP})[0] == 403


def test_segment_rules_birthday_spend_location(env):
    stream(env, ORD_TABLE, order("o1", email="rich@x.se", total=300000, loc=LOC_A2))
    gid = profile_of(env, TENANT_A, "rich@x.se")["guestId"]
    today = crm.now().date()
    soon = today.fromordinal(today.toordinal() + 3)
    crm.patch_profile(TENANT_A, gid, {"birthday": soon.strftime("%m-%d"), "version": 1})
    profile = crm.get_profile(TENANT_A, gid)
    assert crm.matches(TENANT_A, profile, {"birthdayWithinDays": 7, "minSpendSek": 2000, "locationId": LOC_A2},
                       today, None)
    assert not crm.matches(TENANT_A, profile, {"locationId": LOC_A}, today, None)
    assert not crm.matches(TENANT_A, profile, {"minSpendSek": 3001}, today, None)


# --- C11 rebuild ----------------------------------------------------------------------

def test_c11_rebuild_is_idempotent_resumable_and_exclusive(env):
    for n in range(5):
        env.res.Table(RES_TABLE).put_item(Item=booking(f"r{n}", email=f"g{n}@x.se"))
    env.res.Table(ORD_TABLE).put_item(Item=order("o1", email="g0@x.se"))
    env.res.Table(RES_TABLE).put_item(Item=booking("rb", loc=LOC_B, email="b@x.se"))
    status, body = call(env, "POST", "/tenant/guests/rebuild", body={"runId": RUN})
    assert (status, body) == (202, {"status": "running"})
    assert call(env, "POST", "/tenant/guests/rebuild", body={"runId": RUN}) == (202, {"status": "running"})
    assert call(env, "POST", "/tenant/guests/rebuild", body={"runId": "01OTHERAAAAAAAAAAAAAAAAAAA"})[1][
        "error"] == "rebuild_running"
    assert len(env.invoked) == 1

    ticks = iter([60_000, 60_000, 1_000] + [60_000] * 100)  # runs out of time once, re-invokes itself
    ctx = SimpleNamespace(get_remaining_time_in_millis=lambda: next(ticks))
    assert env.app.handler(env.invoked[0], ctx)["status"] == "continued"
    assert env.app.handler(env.invoked[-1], SimpleNamespace(get_remaining_time_in_millis=lambda: 60_000))[
        "status"] == "done"
    assert profile_of(env, TENANT_A, "g0@x.se")["orders"] == 1
    assert all(profile_of(env, TENANT_A, f"g{n}@x.se")["visits"] == 1 for n in range(5))
    assert profile_of(env, TENANT_A, "b@x.se") is None and all_items(env, TENANT_B) == []
    # running the finished job again changes nothing
    env.res.Table(CRM_TABLE).update_item(Key=crm.backfill_key(TENANT_A), UpdateExpression="SET #s = :r",
                                         ExpressionAttributeNames={"#s": "status"},
                                         ExpressionAttributeValues={":r": "running"})
    env.app.handler(env.invoked[-1], SimpleNamespace(get_remaining_time_in_millis=lambda: 60_000))
    assert all(profile_of(env, TENANT_A, f"g{n}@x.se")["visits"] == 1 for n in range(5))


def test_c11_a_stuck_run_can_be_replaced_after_30_minutes(env):
    assert call(env, "POST", "/tenant/guests/rebuild", body={"runId": RUN})[0] == 202
    other = "01OTHERAAAAAAAAAAAAAAAAAAA"
    assert call(env, "POST", "/tenant/guests/rebuild", body={"runId": other})[1]["error"] == "rebuild_running"
    old = crm.iso(crm.now() - timedelta(minutes=31))
    env.res.Table(CRM_TABLE).update_item(Key=crm.backfill_key(TENANT_A), UpdateExpression="SET updatedAt = :o",
                                         ExpressionAttributeValues={":o": old})
    assert call(env, "POST", "/tenant/guests/rebuild", body={"runId": other}) == (202, {"status": "running"})
    assert env.app.handler(env.invoked[0], None) == {"status": "ignored"}  # the dead run stops if it wakes
    assert env.app.handler(env.invoked[-1], None)["status"] == "done"


def test_c11_a_row_without_updated_at_is_stale_by_started_at(env):
    old = crm.iso(crm.now() - timedelta(minutes=31))
    env.res.Table(CRM_TABLE).put_item(Item={**crm.backfill_key(TENANT_A), "tenantId": TENANT_A, "runId": RUN,
                                            "status": "running", "startedAt": old})
    assert call(env, "POST", "/tenant/guests/rebuild", body={"runId": "01OTHERAAAAAAAAAAAAAAAAAAA"})[0] == 202


def test_c11_cursor_save_refreshes_updated_at(env):
    call(env, "POST", "/tenant/guests/rebuild", body={"runId": RUN})
    env.res.Table(CRM_TABLE).update_item(Key=crm.backfill_key(TENANT_A), UpdateExpression="SET updatedAt = :o",
                                         ExpressionAttributeValues={":o": "2000-01-01T00:00:00.000Z"})
    env.app.handler(env.invoked[0], None)
    assert crm.table().get_item(Key=crm.backfill_key(TENANT_A))["Item"]["updatedAt"] > "2026"


def test_self_invoke_targets_the_live_alias(monkeypatch):
    calls = []
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "dev-guest-crm")
    app = load("guest-crm")
    monkeypatch.setattr(app.boto3, "client", lambda name: SimpleNamespace(invoke=lambda **kw: calls.append(kw)))
    app._invoke_self({"guestCrmJob": {"tenantId": TENANT_A, "runId": RUN}})
    assert calls[0]["FunctionName"] == "dev-guest-crm:live" and calls[0]["InvocationType"] == "Event"


def test_c11_validation_role_off(env):
    assert call(env, "POST", "/tenant/guests/rebuild", body={"runId": "x"})[0] == 400
    assert call(env, "POST", "/tenant/guests/rebuild", role="staff_user", body={"runId": RUN})[0] == 403
    guest_crm_off(env)
    assert call(env, "POST", "/tenant/guests/rebuild", body={"runId": RUN})[0] == 403


def test_a_job_payload_for_an_unknown_run_is_ignored(env):
    assert env.app.handler({"guestCrmJob": {"tenantId": TENANT_A, "runId": RUN}}, None) == {"status": "ignored"}


# --- hooks: privacy, purge ------------------------------------------------------------

def _subjects(tenant_id, email=None, phone=None):
    return {c: s for c, s in crm.identities(tenant_id, email, phone)}


def test_privacy_export_returns_profile_and_history(env):
    stream(env, RES_TABLE, booking("r1"))
    answer = env.app.handler({"hook": "privacy.export", "hookVersion": 1, "tenantId": TENANT_A,
                              "email": "anna@example.se", "subjects": _subjects(TENANT_A, "anna@example.se")})
    assert answer["ok"] and answer["items"][0]["profile"]["visits"] == 1
    assert answer["items"][0]["history"][0]["sourceId"] == "r1"
    json.dumps(answer)  # plain JSON (no Decimal)


def test_erase_leaves_no_row_with_the_guests_ids_and_removes_export_entries(env):
    _seed_segment_guests(env)
    put_seg(env, {"lapsedDays": 30})
    path, params = seg_path(suffix="/exports")
    call(env, "POST", path, params=params, body={"exportId": EXP})
    stream(env, RES_TABLE, booking("r9", email="a@x.se", phone="+46703333333"))
    gid = profile_of(env, TENANT_A, "a@x.se")["guestId"]
    subjects = _subjects(TENANT_A, "a@x.se", "+46703333333")
    answer = env.app.handler({"hook": "privacy.erase", "hookVersion": 1, "tenantId": TENANT_A, "email": "a@x.se",
                              "subjects": subjects})
    assert answer["ok"] and answer["erased"]["profiles"] == 1 and answer["erased"]["exportEntries"] == 1
    dump = json.dumps(all_items(env, TENANT_A), default=str)
    for value in (gid, *subjects.values(), mkt.subscriber_id(TENANT_A, "a@x.se")):
        assert value not in dump
    assert ("guest-crm", "GuestProfile.Erased", {"tenantId": TENANT_A, "guestId": gid}) in env.published
    again = env.app.handler({"hook": "privacy.erase", "hookVersion": 1, "tenantId": TENANT_A,
                             "subjects": subjects})
    assert again["ok"] and again["erased"]["profiles"] == 0


def test_erase_in_one_tenant_leaves_the_other_untouched(env):
    stream(env, RES_TABLE, booking("r1"))
    stream(env, RES_TABLE, booking("r2", loc=LOC_B))
    env.app.handler({"hook": "privacy.erase", "hookVersion": 1, "tenantId": TENANT_A,
                     "subjects": _subjects(TENANT_A, "anna@example.se")})
    assert profile_of(env, TENANT_A, "anna@example.se") is None
    assert profile_of(env, TENANT_B, "anna@example.se")["visits"] == 1


def test_purge_deletes_the_partition_in_rounds(env, monkeypatch):
    for n in range(3):
        stream(env, RES_TABLE, booking(f"r{n}", email=f"g{n}@x.se"))
    stream(env, RES_TABLE, booking("rb", loc=LOC_B))
    real = crm.purge
    monkeypatch.setattr(crm, "purge", lambda t: real(t, budget=4))
    rounds = 0
    while True:
        rounds += 1
        answer = env.app.handler({"hook": "tenant.purge", "hookVersion": 1, "tenantId": TENANT_A,
                                  "locationIds": [LOC_A], "round": rounds})
        if answer["done"]:
            break
    assert rounds > 1 and all_items(env, TENANT_A) == [] and all_items(env, TENANT_B) != []


def test_unknown_hook_and_missing_tenant_are_refused(env):
    assert env.app.handler({"hook": "nope", "tenantId": TENANT_A})["ok"] is False
    assert env.app.handler({"hook": "privacy.export"})["ok"] is False
    assert env.app.handler({"something": 1})["ok"] is False


def test_api_event_is_never_read_as_a_hook(env):
    event = with_tenant_claims({"routeKey": "GET /tenant/guests", "hook": "tenant.purge", "tenantId": TENANT_B},
                               TENANT_A)
    assert env.app.handler(event, None)["statusCode"] == 200


# --- two-tenant isolation -------------------------------------------------------------

def test_two_tenant_isolation(env):
    """The same email at TENANT_A and TENANT_B gives two unrelated profiles;
    A's routes never return B's profile, segment or export."""
    stream(env, RES_TABLE, booking("ra", loc=LOC_A))
    stream(env, RES_TABLE, booking("rb", loc=LOC_B))
    a, b = profile_of(env, TENANT_A, "anna@example.se"), profile_of(env, TENANT_B, "anna@example.se")
    assert a["guestId"] != b["guestId"] and a["identities"] != b["identities"]
    put_seg(env, {"minVisits": 1}, tenant=TENANT_B)
    path, params = seg_path(suffix="/exports")
    call(env, "POST", path, params=params, tenant=TENANT_B, body={"exportId": EXP})
    gid_b = b["guestId"]
    assert call(env, "GET", f"/tenant/guests/{gid_b}", params={"guestId": gid_b})[0] == 404
    assert [g["guestId"] for g in call(env, "GET", "/tenant/guests")[1]["guests"]] == [a["guestId"]]
    assert call(env, "GET", "/tenant/guest-segments")[1]["segments"] == []
    p_path, p_params = seg_path(suffix="/preview")
    assert call(env, "GET", p_path, params=p_params)[0] == 404
    assert call(env, "POST", path, params=params, body={"exportId": EXP})[0] == 404
    assert env.app.handler({"hook": "marketing.segment", "tenantId": TENANT_A, "op": "page",
                            "exportId": EXP})["ok"] is False
    for item in all_items(env, TENANT_A):
        assert item.get("tenantId") in (None, TENANT_A)

"""Personalliggare (feature H, ADR-H, CONTRACTS.md 6.H): routes H1-H8, the
platform hook (H9 forward), tenant.purge, chain verification and the
two-tenant isolation suite, against moto with both tables as Terraform
creates them (state table + append-only ledger with the byDay index).

moto cannot enforce DynamoDB resource policies, so the ledger's Deny policy is
asserted in the Terraform module and verified for real only on AWS
(STATUS.md "unverified"); here a test asserts the code never calls an Update,
Delete or BatchWrite on the ledger table."""

import importlib.util
import json
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import boto3
import botocore.client
import pytest
from moto import mock_aws

from shared import dynamo as shared_dynamo
from shared import events
from shared import tenant as shared_tenant
from shared.features import personalliggare as pl
from shared.hash_chain import entry_hash
from tenant_support import (LOC_A, LOC_A2, LOC_B, OWNER_A_SUB, REGION, STAFF_A_SUB, TENANT_A, TENANT_B,
                            TENANT_TABLE, USER_TABLE_DEFAULT, features_on, seed_tenancy, tenant_row,
                            with_tenant_claims)

ROOT = Path(__file__).parents[1] / "functions"
STATE, LEDGER = "test-personalliggare", "test-personalliggare-ledger"
NOW = datetime(2026, 11, 5, 8, 0, tzinfo=timezone.utc)  # 09:00 in Stockholm
DAY = "2026-11-05"
P_OWNER, P_STAFF = "01PERSONOWNERAAAAAAAAAAAA", "01PERSONSTAFFAAAAAAAAAAAA"
P_HELPER, P_B = "01PERSONHELPERAAAAAAAAAAA", "01PERSONBBBBBBBBBBBBBBBBB"


def pnr(base9):
    """A valid 10-digit personnummer from its first nine digits (Luhn)."""
    total = 0
    for i, ch in enumerate(base9):
        n = int(ch) * (2 if i % 2 == 0 else 1)
        total += n - 9 if n > 9 else n
    return f"{base9[:6]}-{base9[6:]}{(10 - total % 10) % 10}"


PNR_OWNER, PNR_STAFF, PNR_HELPER, PNR_B = pnr("811228987"), pnr("900101001"), pnr("850709123"), pnr("770303555")


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


def load():
    spec = importlib.util.spec_from_file_location("pl_app", ROOT / "personalliggare" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tables(ddb):
    for name in (STATE, LEDGER):
        args = dict(TableName=name, BillingMode="PAY_PER_REQUEST",
                    KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
                    AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                          {"AttributeName": "SK", "AttributeType": "S"}])
        if name == LEDGER:
            args["AttributeDefinitions"] += [{"AttributeName": "GSI1PK", "AttributeType": "S"},
                                             {"AttributeName": "GSI1SK", "AttributeType": "S"}]
            args["GlobalSecondaryIndexes"] = [{"IndexName": "byDay", "Projection": {"ProjectionType": "ALL"},
                                               "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"},
                                                             {"AttributeName": "GSI1SK", "KeyType": "RANGE"}]}]
        ddb.create_table(**args)


@pytest.fixture
def env(monkeypatch):
    for key, value in {"ENVIRONMENT": "dev", "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                       "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": REGION, "STATE_TABLE_NAME": STATE,
                       "LEDGER_TABLE_NAME": LEDGER, "USER_TABLE_NAME": USER_TABLE_DEFAULT}.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        on = features_on("personalliggare")
        seed_tenancy(monkeypatch,
                     tenant_a=tenant_row(TENANT_A, features=on, legalName="Bistro A AB", orgNumber="556677-8899"),
                     tenant_b=tenant_row(TENANT_B, features=on))
        res = boto3.resource("dynamodb", region_name=REGION)
        res.Table("test-location").update_item(Key={"PK": f"TENANT#{TENANT_A}", "SK": f"LOCATION#{LOC_A}"},
                                               UpdateExpression="SET address = :a",
                                               ExpressionAttributeValues={":a": "Storgatan 1, Stockholm"})
        _tables(boto3.client("dynamodb", region_name=REGION))
        clock = Clock(NOW)
        monkeypatch.setattr(pl, "now", clock)
        published = []
        monkeypatch.setattr(events, "publish", lambda f, t, d: published.append((t, d)) or "id")
        api = load()
        e = SimpleNamespace(api=api, res=res, clock=clock, published=published)
        _persons(e)
        yield e
        shared_dynamo._resource = None
        shared_dynamo._client = None
        shared_tenant.reset_caches()


OWNER_A, OWNER_B, STAFF_A = (TENANT_A, "owner_user"), (TENANT_B, "owner_user"), (TENANT_A, "staff_user")
BASE = "/locations/{locationId}/personalliggare"
_n = [0]


def rid():
    _n[0] += 1
    return f"01REC{_n[0]:021d}"


def call(e, method, template, *, as_=OWNER_A, location=LOC_A, body=None, query=None, person=None):
    params = {"locationId": location}
    if person:
        params["personId"] = person
    event = {"routeKey": f"{method} {template}", "pathParameters": params, "queryStringParameters": query,
             "requestContext": {"http": {"method": method}}, "body": json.dumps(body) if body is not None else None}
    tenant_id, role = as_
    with_tenant_claims(event, tenant_id, role, STAFF_A_SUB if role == "staff_user" else None)
    response = e.api.handler(event, None)
    return response["statusCode"], (json.loads(response["body"]) if response.get("body") else None)


def put_person(e, person_id, *, as_=OWNER_A, **over):
    body = {"name": "Anna Andersson", "personalIdentity": PNR_HELPER, "locationIds": [LOC_A], "active": True,
            "version": 0, **over}
    return call(e, "PUT", "/tenant/personalliggare/persons/{personId}", as_=as_, person=person_id, body=body)


def _persons(e):
    assert put_person(e, P_OWNER, name="Olle Ägare", personalIdentity=PNR_OWNER, userSub=OWNER_A_SUB,
                      locationIds=[])[0] == 201
    assert put_person(e, P_STAFF, name="Sara Personal", personalIdentity=PNR_STAFF, userSub=STAFF_A_SUB)[0] == 201
    assert put_person(e, P_HELPER, name="Hugo Hjälpreda", personalIdentity=PNR_HELPER)[0] == 201
    assert put_person(e, P_B, as_=OWNER_B, name="Bertil B", personalIdentity=PNR_B, locationIds=[LOC_B])[0] == 201


def check(e, kind, *, as_=OWNER_A, location=LOC_A, person=None, record_id=None):
    body = {"recordId": record_id or rid()}
    if person:
        body["personId"] = person
    return call(e, "POST", BASE + f"/{kind}", as_=as_, location=location, body=body)


def correction(e, *, as_=OWNER_A, location=LOC_A, record_id=None, **over):
    body = {"recordId": record_id or rid(), "personId": P_HELPER, "kind": "check_in",
            "eventTime": "2026-11-05T06:30:00Z", "reason": "Glömde checka in", **over}
    return call(e, "POST", BASE + "/corrections", as_=as_, location=location, body=body)


def ledger(e, location=LOC_A):
    return pl.chain(location)


def turn_off(e, tenant_id=TENANT_A):
    e.res.Table(TENANT_TABLE).update_item(Key={"PK": f"TENANT#{tenant_id}", "SK": "PROFILE"},
                                          UpdateExpression="SET entitlements.features.personalliggare = :f",
                                          ExpressionAttributeValues={":f": False})
    shared_tenant.reset_caches()


# --- personal identity -----------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (pnr("811228987"), "19811228-" + pnr("811228987")[-4:]),
    ("19" + pnr("811228987"), "19811228-" + pnr("811228987")[-4:]),
    (pnr("811228987").replace("-", ""), "19811228-" + pnr("811228987")[-4:]),
    (pnr("701063239"), "19701063-" + pnr("701063239")[-4:]),  # samordningsnummer, day 63 = 3rd
    (pnr("100101123"), "20100101-" + pnr("100101123")[-4:]),
    (pnr("100101123").replace("-", "+"), "19100101-" + pnr("100101123")[-4:]),  # "+" = 100 years or older
])
def test_personal_identity_normalises(value, expected):
    assert pl.normalize_personal_identity(value, NOW) == expected


@pytest.mark.parametrize("value", ["811228-9875", "811328-9874", "abc", "", None, 811228, "19811232-1234",
                                   "991231-123"])
def test_personal_identity_rejects(value):
    with pytest.raises(pl.LedgerError) as err:
        pl.normalize_personal_identity(value, NOW)
    assert err.value.code == "invalid_personal_identity"


# --- H7, H8 persons -----------------------------------------------------------------------------

def test_persons_list_is_owner_only_and_tenant_scoped(env):
    status, body = call(env, "GET", "/tenant/personalliggare/persons")
    assert status == 200
    assert {p["personId"] for p in body["persons"]} == {P_OWNER, P_STAFF, P_HELPER}
    assert all(p["personalIdentity"] for p in body["persons"])
    assert call(env, "GET", "/tenant/personalliggare/persons", as_=STAFF_A)[0] == 403
    status, body = call(env, "GET", "/tenant/personalliggare/persons", as_=OWNER_B)
    assert [p["personId"] for p in body["persons"]] == [P_B]


def test_put_person_validation_version_and_isolation(env):
    status, body = put_person(env, "01PERSONNEWAAAAAAAAAAAAAA", personalIdentity="811228-9875")
    assert (status, body["code"]) == (400, "invalid_personal_identity")
    assert put_person(env, "01PERSONNEWAAAAAAAAAAAAAA", as_=STAFF_A)[0] == 403
    # same body again -> same person, no version bump
    status, body = put_person(env, P_HELPER, name="Hugo Hjälpreda")
    assert (status, body["version"]) == (200, 1)
    # stale version -> 409
    status, body = put_person(env, P_HELPER, name="Hugo H", version=0)
    assert (status, body["code"]) == (409, "changed_retry")
    status, body = put_person(env, P_HELPER, name="Hugo H", version=1)
    assert (status, body["version"], body["name"]) == (200, 2, "Hugo H")
    # tenant B's person id from tenant A -> 404 (the key is tenant-scoped: A creates its own)
    assert put_person(env, P_B, as_=OWNER_A, version=1)[0] == 409
    assert call(env, "GET", "/tenant/personalliggare/persons", as_=OWNER_B)[1]["persons"][0]["name"] == "Bertil B"
    # foreign location and foreign user are 404
    assert put_person(env, "01PERSONNEWAAAAAAAAAAAAAA", locationIds=[LOC_B])[0] == 404
    assert put_person(env, "01PERSONNEWAAAAAAAAAAAAAA", userSub="owner-b-sub")[0] == 404
    # one person per login
    status, body = put_person(env, "01PERSONNEWAAAAAAAAAAAAAA", userSub=STAFF_A_SUB)
    assert (status, body["code"]) == (409, "user_already_linked")
    status, body = put_person(env, "01PERSONNEWAAAAAAAAAAAAAA", unknown=1)
    assert status == 400 and "unsupported fields" in body["error"]


def test_persons_entitlement_off(env):
    turn_off(env)
    assert call(env, "GET", "/tenant/personalliggare/persons")[0] == 403
    assert put_person(env, P_HELPER, version=1)[0] == 403


# --- H2, H3 check-in/out --------------------------------------------------------------------------

def test_staff_checks_in_and_out_and_the_chain_holds(env):
    status, rec = check(env, "check-in", as_=STAFF_A)
    assert status == 201
    assert (rec["seq"], rec["kind"], rec["personId"], rec["recordedBy"]["role"]) == (1, "check_in", P_STAFF, "staff")
    assert "personalIdentity" not in rec
    status, day = call(env, "GET", BASE, as_=STAFF_A, query={"day": DAY})
    assert [p["personId"] for p in day["present"]] == [P_STAFF]
    assert day["me"]["present"] is True and "personalIdentity" not in json.dumps(day)
    assert check(env, "check-in", as_=STAFF_A)[1]["code"] == "already_checked_in"
    env.clock.value = NOW + timedelta(hours=8)
    status, rec = check(env, "check-out", as_=STAFF_A)
    assert (status, rec["seq"], rec["kind"]) == (201, 2, "check_out")
    assert check(env, "check-out", as_=STAFF_A)[1]["code"] == "not_checked_in"
    records = ledger(env)
    assert [int(r["seq"]) for r in records] == [1, 2]
    assert records[1]["prevHash"] == records[0]["hash"]
    assert all(entry_hash(r) == r["hash"] for r in records)
    assert records[0]["employer"] == {"legalName": "Bistro A AB", "orgNumber": "556677-8899"}
    assert records[0]["premises"]["address"] == "Storgatan 1, Stockholm"
    assert records[0]["personalIdentity"].endswith(PNR_STAFF[-4:])
    assert pl.verify(records, pl.get_head(LOC_A)) is None
    assert [t for t, _ in env.published] == ["Personalliggare.CheckedIn", "Personalliggare.CheckedOut"]
    for _, detail in env.published:  # no name or identity number in events
        assert set(detail) == {"tenantId", "locationId", "seq", "personId", "eventTime"}


def test_owner_records_for_anyone_staff_only_for_themselves(env):
    status, rec = check(env, "check-in", person=P_HELPER)
    assert (status, rec["personId"], rec["recordedBy"]["role"]) == (201, P_HELPER, "owner")
    status, body = check(env, "check-in", as_=STAFF_A, person=P_HELPER)
    assert (status, body["code"]) == (403, "owner_only")
    assert check(env, "check-in", as_=STAFF_A, person=P_STAFF)[0] == 201


def test_no_person_for_user(env):
    env.res.Table(STATE).delete_item(Key=pl.user_key(TENANT_A, STAFF_A_SUB))
    status, body = check(env, "check-in", as_=STAFF_A)
    assert (status, body["code"]) == (409, "no_person_for_user")


def test_idempotent_retry_returns_the_stored_record(env):
    record_id = rid()
    first = check(env, "check-in", person=P_HELPER, record_id=record_id)
    again = check(env, "check-in", person=P_HELPER, record_id=record_id)
    assert first[0] == 201 and again[0] == 200 and first[1] == again[1]
    assert len(ledger(env)) == 1 and len(env.published) == 1


def test_check_in_foreign_tenant_and_location(env):
    assert check(env, "check-in", location=LOC_B)[0] == 404  # B's location
    assert check(env, "check-in", person=P_B)[0] == 404  # B's person
    assert check(env, "check-in", as_=STAFF_A, location=LOC_A2)[0] == 404  # staff of another location
    assert check(env, "check-in", person=P_HELPER, location=LOC_A2)[0] == 404  # helper not assigned there
    assert check(env, "check-in", person=P_OWNER, location=LOC_A2)[0] == 201  # empty list = everywhere


def test_check_in_validation_and_entitlement(env):
    status, body = call(env, "POST", BASE + "/check-in", body={"recordId": "short"})
    assert status == 400
    status, body = call(env, "POST", BASE + "/check-in", body={"recordId": rid(), "eventTime": "x"})
    assert status == 400 and "unsupported fields" in body["error"]  # never a backdated check-in
    turn_off(env)
    assert check(env, "check-in")[1]["error"] == "feature_not_in_plan"
    assert check(env, "check-out")[0] == 403


def test_twenty_concurrent_check_ins_give_contiguous_seqs(env, monkeypatch):
    for i in range(20):
        assert put_person(env, f"01PERSONCONC{i:013d}", personalIdentity=PNR_HELPER, name=f"P {i}")[0] == 201
    gate = threading.Barrier(20)
    real_head = pl.get_head
    results = []

    first_read = threading.local()

    def racing_head(location_id):
        head = real_head(location_id)
        if not getattr(first_read, "done", False):
            first_read.done = True
            gate.wait(timeout=10)  # first round: all twenty read the same (empty) head
        return head

    monkeypatch.setattr(pl, "get_head", racing_head)
    monkeypatch.setattr(pl, "MAX_RETRIES", 25)
    # moto is not thread-safe: one AWS call at a time, the races stay between the calls.
    lock = threading.RLock()
    real_call = botocore.client.BaseClient._make_api_call

    def one_at_a_time(self, *args, **kwargs):
        with lock:
            return real_call(self, *args, **kwargs)

    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", one_at_a_time)

    ids = [rid() for _ in range(20)]

    def one(i):
        results.append(check(env, "check-in", person=f"01PERSONCONC{i:013d}", record_id=ids[i]))

    threads = [threading.Thread(target=one, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(s for s, _ in results) == [201] * 20
    records = ledger(env)
    assert [int(r["seq"]) for r in records] == list(range(1, 21))
    assert pl.verify(records, real_head(LOC_A)) is None


def test_lost_races_end_in_changed_retry(env, monkeypatch):
    monkeypatch.setattr(pl, "get_head", lambda location_id: {})  # always stale once a record exists
    assert check(env, "check-in", person=P_HELPER)[0] == 201
    status, body = check(env, "check-in", person=P_OWNER)
    assert (status, body["code"]) == (409, "changed_retry")


# --- H4 corrections -------------------------------------------------------------------------------

def test_correction_of_a_time_shows_original_beside_it(env):
    _, original = check(env, "check-in", person=P_HELPER)
    status, rec = correction(env, correctsSeq=original["seq"], eventTime="2026-11-05T07:15:00Z",
                             reason="Kom 08:15, appen var nere")
    assert (status, rec["kind"], rec["correctsSeq"]) == (201, "correction", 1)
    assert rec["corrected"] == {"kind": "check_in", "eventTime": "2026-11-05T07:15:00Z"}
    _, day = call(env, "GET", BASE, query={"day": DAY})
    row = day["records"][0]
    assert (row["seq"], row["eventTime"], row["reason"]) == (1, "2026-11-05T07:15:00Z", "Kom 08:15, appen var nere")
    assert row["original"]["eventTime"] == "2026-11-05T08:00:00Z"
    assert len(day["records"]) == 1  # the correction is applied, not listed twice
    assert len(ledger(env)) == 2  # nothing edited: two records
    assert env.published[-1] == ("Personalliggare.Corrected", {
        "tenantId": TENANT_A, "locationId": LOC_A, "seq": 2, "personId": P_HELPER, "correctsSeq": 1,
        "kind": "check_in"})


def test_correction_must_name_the_person_of_the_record_it_corrects(env):
    _, original = check(env, "check-in", person=P_HELPER)
    status, body = correction(env, correctsSeq=original["seq"], personId=P_STAFF)
    assert (status, body["code"], body["field"]) == (400, "wrong_person", "correctsSeq")
    assert len(ledger(env)) == 1 and len(env.published) == 1  # nothing appended
    assert correction(env, correctsSeq=original["seq"], personId=P_HELPER)[0] == 201


def test_late_registration_moves_presence(env):
    status, rec = correction(env)
    assert status == 201 and "correctsSeq" not in rec
    _, day = call(env, "GET", BASE, query={"day": DAY})
    assert [p["personId"] for p in day["present"]] == [P_HELPER]
    assert day["records"][0]["late"] is True
    assert correction(env, kind="check_out", eventTime="2026-11-05T07:50:00Z")[0] == 201
    assert call(env, "GET", BASE, query={"day": DAY})[1]["present"] == []
    assert env.published[0][1]["correctsSeq"] == 0


@pytest.mark.parametrize("over, code", [
    ({"reason": "kort"}, "reason_required"),
    ({"reason": None}, "reason_required"),
    ({"reason": "x" * 301}, "reason_required"),
    ({"eventTime": "2026-11-05T09:00:00Z"}, "invalid_event_time"),  # future
    ({"eventTime": "2023-12-31T12:00:00Z"}, "invalid_event_time"),  # before the retention window
    ({"eventTime": "2026-11-05 06:00"}, "invalid_event_time"),
])
def test_correction_validation(env, over, code):
    status, body = correction(env, **over)
    assert (status, body["code"]) == (400, code)


def test_correction_roles_isolation_idempotency_and_entitlement(env):
    assert correction(env, as_=STAFF_A)[1]["code"] == "owner_only"
    assert correction(env, correctsSeq=99)[0] == 404
    check(env, "check-in", as_=OWNER_B, location=LOC_B, person=P_B)
    assert correction(env, correctsSeq=1)[0] == 404  # seq 1 exists only on LOC_B
    assert correction(env, location=LOC_B)[0] == 404
    assert correction(env, personId=P_B)[0] == 404
    record_id = rid()
    first, again = correction(env, record_id=record_id), correction(env, record_id=record_id)
    assert (first[0], again[0], first[1]) == (201, 200, again[1])
    turn_off(env)
    assert correction(env)[0] == 403


# --- H1 / H5 views ------------------------------------------------------------------------------------

def test_day_view_validation_roles_and_isolation(env):
    assert call(env, "GET", BASE, query={"day": "2026-13-01"})[0] == 400
    assert call(env, "GET", BASE, as_=STAFF_A, location=LOC_A2)[0] == 404
    assert call(env, "GET", BASE, location=LOC_B)[0] == 404
    turn_off(env)
    assert call(env, "GET", BASE)[0] == 403


def test_inspection_shows_identity_and_appends_a_note(env):
    check(env, "check-in", person=P_HELPER)
    status, view = call(env, "GET", BASE + "/inspection", as_=STAFF_A, query={"day": DAY})
    assert status == 200
    assert view["present"][0]["personalIdentity"] == pl.normalize_personal_identity(PNR_HELPER, NOW)
    assert view["records"][0]["personalIdentity"]
    assert view["employer"]["orgNumber"] == "556677-8899"
    call(env, "GET", BASE + "/inspection", query={"day": DAY})
    notes = [r for r in ledger(env) if r["kind"] == "system_note"]
    assert [n["note"] for n in notes] == ["inspection_view", "inspection_view"]  # one per call, by design
    assert [n["recordedBy"]["role"] for n in notes] == ["staff", "owner"]
    assert pl.verify(ledger(env), pl.get_head(LOC_A)) is None
    _, day = call(env, "GET", BASE, query={"day": DAY})
    assert [r["kind"] for r in day["records"]] == ["check_in"]  # notes are not shifts


def test_inspection_validation_isolation_entitlement(env):
    assert call(env, "GET", BASE + "/inspection", query={"day": "x"})[0] == 400
    assert call(env, "GET", BASE + "/inspection", as_=STAFF_A, location=LOC_A2)[0] == 404
    assert call(env, "GET", BASE + "/inspection", location=LOC_B)[0] == 404
    turn_off(env)
    assert call(env, "GET", BASE + "/inspection")[0] == 403


# --- H6 export ------------------------------------------------------------------------------------------

def test_export_csv_and_json_with_chain_result(env):
    check(env, "check-in", person=P_HELPER)
    env.clock.value = NOW + timedelta(hours=4)
    check(env, "check-out", person=P_HELPER)
    status, out = call(env, "GET", BASE + "/export", query={"from": DAY, "to": DAY, "format": "csv"})
    assert status == 200 and out["chainOk"] is True and "firstBadSeq" not in out and out["count"] == 2
    lines = out["content"].strip().split("\r\n")
    assert lines[0].startswith("seq,kind,eventTime") and len(lines) == 3
    assert pl.normalize_personal_identity(PNR_HELPER, NOW) in lines[1]
    status, out = call(env, "GET", BASE + "/export", query={"from": DAY, "to": DAY, "format": "json"})
    assert [r["kind"] for r in out["records"]] == ["check_in", "check_out"]


def test_export_detects_a_tampered_record_and_a_gap(env):
    for _ in range(3):
        check(env, "check-in", person=P_HELPER)
        check(env, "check-out", person=P_HELPER)
    # Tampering is only possible around the Deny policy (moto does not enforce it).
    env.res.Table(LEDGER).update_item(Key={"PK": f"LOCATION#{LOC_A}", "SK": pl.seq_sk(3)},
                                      UpdateExpression="SET #n = :n", ExpressionAttributeNames={"#n": "name"},
                                      ExpressionAttributeValues={":n": "Någon annan"})
    out = call(env, "GET", BASE + "/export", query={"from": DAY, "to": DAY})[1]
    assert (out["chainOk"], out["firstBadSeq"]) == (False, 3)
    records = ledger(env)
    assert pl.verify([r for r in records if int(r["seq"]) != 2]) == 2  # a missing record
    assert pl.verify(records[:4], pl.get_head(LOC_A)) is not None  # a cut tail


def test_export_roles_range_isolation(env):
    assert call(env, "GET", BASE + "/export", as_=STAFF_A, query={"from": DAY, "to": DAY})[0] == 403
    assert call(env, "GET", BASE + "/export", query={"from": DAY, "to": "2026-11-01"})[1]["code"] == "invalid_range"
    assert call(env, "GET", BASE + "/export", query={"from": "2025-01-01", "to": "2026-11-05"})[0] == 400
    assert call(env, "GET", BASE + "/export", query={"from": DAY, "to": DAY, "format": "xml"})[0] == 400
    assert call(env, "GET", BASE + "/export", location=LOC_B, query={"from": DAY, "to": DAY})[0] == 404


def test_export_works_with_the_feature_off_while_records_exist(env):
    turn_off(env)
    status, body = call(env, "GET", BASE + "/export", query={"from": DAY, "to": DAY})
    assert (status, body["error"]) == (403, "feature_not_in_plan")  # no records: refused
    env.res.Table(TENANT_TABLE).update_item(Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"},
                                            UpdateExpression="SET entitlements.features.personalliggare = :t",
                                            ExpressionAttributeValues={":t": True})
    shared_tenant.reset_caches()
    check(env, "check-in", person=P_HELPER)
    turn_off(env)
    status, body = call(env, "GET", BASE + "/export", query={"from": DAY, "to": DAY})
    assert status == 200 and body["count"] == 1  # the deliberate exception (ADR-H)
    assert check(env, "check-out", person=P_HELPER)[0] == 403  # everything else stays off


# --- hooks ---------------------------------------------------------------------------------------------

def platform(e, tenant_id, query):
    return e.api.handler({"hook": "platform", "hookVersion": 1, "tenantId": tenant_id,
                          "op": "personalliggare.export", "body": None, "query": query,
                          "operator": {"sub": "op", "email": "op@booqy.se"}})


def test_platform_export_and_purge_leaves_the_ledger(env):
    check(env, "check-in", person=P_HELPER)
    out = platform(env, TENANT_A, {"locationId": LOC_A, "from": DAY, "to": DAY})
    assert out["ok"] and out["status"] == 200 and out["body"]["chainOk"] and "content" in out["body"]
    assert platform(env, TENANT_B, {"locationId": LOC_A, "from": DAY, "to": DAY})["status"] == 404
    assert platform(env, TENANT_A, {"locationId": LOC_B, "from": DAY, "to": DAY})["status"] == 404
    assert platform(env, TENANT_A, {"locationId": LOC_A, "from": DAY, "to": "2026-01-01"})["status"] == 400
    assert env.api.handler({"hook": "platform", "tenantId": TENANT_A, "op": "x"})["code"] == "unknown_op"
    result = env.api.handler({"hook": "tenant.purge", "hookVersion": 1, "tenantId": TENANT_A,
                              "locationIds": [LOC_A, LOC_A2], "round": 1})
    assert result["ok"] and result["done"] and result["counts"]["stateItems"] > 0
    assert pl.list_persons(TENANT_A) == []
    assert pl.query_all(pl.state_table(), pl.tenant_pk(TENANT_A), "") == []
    assert [r["SK"] for r in pl.query_all(pl.state_table(), pl.location_pk(LOC_A), "")] == ["HEAD"]
    assert int(pl.get_head(LOC_A)["seq"]) == 1  # the head stays with the ledger it points at (no PII)
    assert len(ledger(env)) == 1  # never purged
    assert [p["personId"] for p in pl.list_persons(TENANT_B)] == [P_B]  # B untouched
    # offboarded (state gone): the operator export still works from the ledger alone
    assert platform(env, TENANT_A, {"locationId": LOC_A, "from": DAY, "to": DAY})["status"] == 200


def test_purge_then_re_onboard_then_check_in_continues_the_chain(env):
    _, first = check(env, "check-in", person=P_HELPER)
    env.clock.value = NOW + timedelta(hours=8)
    assert check(env, "check-out", person=P_HELPER)[0] == 201
    assert env.api.handler({"hook": "tenant.purge", "tenantId": TENANT_A, "locationIds": [LOC_A, LOC_A2]})["done"]
    assert check(env, "check-in", person=P_HELPER)[0] == 404  # the person is gone with the state
    # the tenant comes back: persons are created again, the ledger carries on
    assert put_person(env, P_HELPER)[0] == 201
    env.clock.value = NOW + timedelta(days=1)
    status, rec = check(env, "check-in", person=P_HELPER)
    assert (status, rec["seq"]) == (201, 3)
    records = ledger(env)
    assert [int(r["seq"]) for r in records] == [1, 2, 3]
    assert records[2]["prevHash"] == records[1]["hash"] and int(records[0]["seq"]) == first["seq"]
    assert pl.verify(records, pl.get_head(LOC_A)) is None
    _, day = call(env, "GET", BASE, query={"day": "2026-11-06"})
    assert [p["personId"] for p in day["present"]] == [P_HELPER]


def test_unknown_hook_and_route(env):
    assert env.api.handler({"hook": "privacy.export", "tenantId": TENANT_A})["code"] == "unknown_hook"
    assert env.api.handler({"x": 1})["code"] == "unknown_event"
    assert call(env, "DELETE", BASE)[0] == 404


def test_the_ledger_is_only_put_get_and_queried(env, monkeypatch):
    """The code never asks for an Update/Delete/BatchWrite on the ledger (the
    Deny resource policy would refuse it in AWS)."""
    real = pl.ledger_table
    seen = []

    class Spy:
        def __init__(self, tbl):
            self._tbl = tbl

        def __getattr__(self, name):
            seen.append(name)
            return getattr(self._tbl, name)

    monkeypatch.setattr(pl, "ledger_table", lambda: Spy(real()))
    sent = []
    real_client = shared_dynamo.client()
    orig = real_client.transact_write_items

    def spy_transact(**kwargs):
        sent.extend(kwargs["TransactItems"])
        return orig(**kwargs)

    monkeypatch.setattr(real_client, "transact_write_items", spy_transact)
    check(env, "check-in", person=P_HELPER)
    correction(env, correctsSeq=1)
    call(env, "GET", BASE + "/inspection")
    call(env, "GET", BASE + "/export", query={"from": DAY, "to": DAY})
    env.api.handler({"hook": "tenant.purge", "tenantId": TENANT_A, "locationIds": [LOC_A]})
    assert set(seen) <= {"get_item", "query"}
    for item in sent:
        (op, spec), = item.items()
        if spec["TableName"] == LEDGER:
            assert op == "Put"


def test_ttl_is_end_of_second_calendar_year():
    assert pl.ttl_for(NOW) == int(datetime(2029, 1, 1, tzinfo=pl.TZ).timestamp())
    assert isinstance(pl.ttl_for(NOW), int) and not isinstance(pl.ttl_for(NOW), Decimal)

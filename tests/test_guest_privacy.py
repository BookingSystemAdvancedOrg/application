"""guest-privacy: owners edit the privacy notice and withdraw a guest's
marketing consent; site-config serves the notice and the consent wording."""

import importlib.util
import json
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from shared import consent, privacy_notice
from shared import marketing as m
from shared import tenant as shared_tenant
from tenant_support import (ALL_FEATURES, LOC_A, REGION, TENANT_A, location_row, seed_tenancy, tenant_row,
                            with_tenant_claims)
from test_consent import consent_table

ROOT = Path(__file__).parents[1] / "functions"


def load(name):
    spec = importlib.util.spec_from_file_location(f"gp_{name.replace('-', '_')}", ROOT / name / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def env(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
        monkeypatch.setenv("ENVIRONMENT", "dev")
        seed_tenancy(monkeypatch, tenant_a=tenant_row(
            TENANT_A, name="Bistro Linnea", legalName="Linnea AB", orgNumber="556000-1111",
            contactEmail="hej@linnea.se", contactPhone="+4681234567", slug="linnea",
            address={"street": "Storgatan 1", "postalCode": "11122", "city": "Stockholm"},
            features={**ALL_FEATURES, "marketing": True}),
            locations_a=[location_row(TENANT_A, LOC_A)])
        monkeypatch.setenv("CONSENT_TABLE_NAME", "test-consent")
        monkeypatch.setenv("MARKETING_TABLE_NAME", "test-marketing")
        res = boto3.resource("dynamodb", region_name=REGION)
        log = consent_table(res)
        res.create_table(TableName="test-marketing", BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                                    {"AttributeName": "SK", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                               {"AttributeName": "SK", "AttributeType": "S"}])
        shared_tenant.reset_caches()
        yield {"fn": load("guest-privacy"), "log": log, "marketing": res.Table("test-marketing")}
        shared_tenant.reset_caches()


def call(env, method, path, body=None, query=None, role="owner_user"):
    event = {"routeKey": f"{method} {path}", "requestContext": {"http": {"method": method, "path": path}},
             "queryStringParameters": query, "body": json.dumps(body) if body is not None else None}
    out = env["fn"].handler(with_tenant_claims(event, TENANT_A, role), None)
    return out["statusCode"], json.loads(out["body"]) if out.get("body") else None


def test_staff_cannot_use_it(env):
    assert call(env, "GET", "/privacy/notice", role="staff_user")[0] == 403


def test_template_is_prefilled_with_the_restaurant(env):
    status, body = call(env, "GET", "/privacy/notice")
    assert status == 200 and body["notice"]["custom"] is False
    text = body["template"]
    for part in ("Linnea AB", "556000-1111", "Storgatan 1", "hej@linnea.se", "+4681234567", "Erbjudanden",
                 "13 månader", "7 år"):
        assert part in text
    assert "\u2014" not in text  # house style: no em dash in copy


def test_save_versions_and_reset(env):
    status, body = call(env, "PUT", "/privacy/notice", {"body": "## Vår policy\nText."})
    assert status == 200 and body["notice"] == {**body["notice"], "body": "## Vår policy\nText.", "version": 1,
                                                "custom": True}
    assert call(env, "PUT", "/privacy/notice", {"body": "## Ny\nText 2."})[1]["notice"]["version"] == 2
    assert call(env, "GET", "/privacy/notice")[1]["notice"]["body"] == "## Ny\nText 2."
    status, body = call(env, "DELETE", "/privacy/notice")
    assert status == 200 and body["notice"]["custom"] is False


@pytest.mark.parametrize("raw", ["", "   ", "x" * (privacy_notice.MAX_LENGTH + 1), "<script>alert(1)</script>", 5])
def test_bad_notice_is_refused(env, raw):
    assert call(env, "PUT", "/privacy/notice", {"body": raw})[0] == 400


def test_site_config_serves_the_saved_notice(env, monkeypatch):
    call(env, "PUT", "/privacy/notice", {"body": "## Egen text"})
    boto3.resource("dynamodb", region_name=REGION).Table("test-tenant").put_item(
        Item={"PK": "SLUG#linnea", "SK": "TENANT", "tenantId": TENANT_A})
    site = load("tenant-site-config")
    out = site.handler({"routeKey": "GET /site-config", "queryStringParameters": {"slug": "linnea"},
                        "requestContext": {"http": {"method": "GET"}}}, None)
    privacy = json.loads(out["body"])["privacy"]
    assert privacy["notice"]["body"] == "## Egen text"
    assert privacy["retention"]["bookingMonths"] == 13
    assert privacy["marketingConsent"]["label"] == consent.render(consent.CURRENT, "Bistro Linnea")


def test_lookup_and_withdraw_on_the_guests_behalf(env):
    consent.grant(env["log"], tenant_id=TENANT_A, restaurant="Bistro Linnea", version=consent.CURRENT,
                  channels=["email", "sms"], email="anna@example.se", phone="+46701234567", source="booking")
    env["marketing"].put_item(Item={**m.sub_key(TENANT_A, m.subscriber_id(TENANT_A, "anna@example.se")),
                                    "status": "subscribed", "email": "anna@example.se"})
    status, body = call(env, "GET", "/privacy/consents", query={"email": "Anna@example.se", "phone": "070-123 45 67"})
    assert status == 200 and body["email"]["state"] == "granted" and body["sms"]["state"] == "granted"
    assert body["email"]["history"][0]["text"] == consent.render(consent.CURRENT, "Bistro Linnea")
    status, body = call(env, "POST", "/privacy/consents/withdraw",
                        {"email": "anna@example.se", "phone": "0701234567", "channels": ["email"]})
    assert status == 200 and body["withdrawn"] == ["email"]
    assert body["email"]["state"] == "withdrawn" and body["sms"]["state"] == "granted"
    assert body["email"]["history"][0]["by"] == "restaurant"
    assert len(body["email"]["history"]) == 2  # the grant is still there: nothing overwritten
    sub = env["marketing"].get_item(Key=m.sub_key(TENANT_A, m.subscriber_id(TENANT_A, "anna@example.se")))["Item"]
    assert sub["status"] == "unsubscribed"
    # a second withdrawal adds nothing
    assert call(env, "POST", "/privacy/consents/withdraw",
                {"email": "anna@example.se", "channels": ["email"]})[1]["withdrawn"] == []


def test_withdraw_unsubscribes_a_subscriber_without_a_grant_on_record(env):
    env["marketing"].put_item(Item={**m.sub_key(TENANT_A, m.subscriber_id(TENANT_A, "old@example.se")),
                                    "status": "subscribed", "email": "old@example.se"})
    status, body = call(env, "POST", "/privacy/consents/withdraw", {"email": "old@example.se", "channels": ["email"]})
    assert status == 200 and body["withdrawn"] == []
    sub = env["marketing"].get_item(Key=m.sub_key(TENANT_A, m.subscriber_id(TENANT_A, "old@example.se")))["Item"]
    assert sub["status"] == "unsubscribed" and sub["unsubscribedVia"] == "restaurant"


def test_lookup_needs_a_contact_detail(env):
    assert call(env, "GET", "/privacy/consents", query={})[0] == 400
    assert call(env, "POST", "/privacy/consents/withdraw", {"email": "a@b.se", "channels": ["fax"]})[0] == 400


# --- export and erasure ---------------------------------------------------------------------

from decimal import Decimal  # noqa: E402

from shared import marketing_offer as offer  # noqa: E402
from tenant_support import LOC_B  # noqa: E402

ANNA = {"customerName": "Anna Gäst", "customerEmail": "Anna@Example.se", "customerPhone": "+46701234567",
        "notes": "nötallergi"}


@pytest.fixture
def guest(env, monkeypatch):
    res = boto3.resource("dynamodb", region_name=REGION)
    tables = {}
    for kind, name in (("reservation", "test-reservation"), ("order", "test-order"), ("catering", "test-catering"),
                       ("history", "test-history")):
        res.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                                    {"AttributeName": "SK", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                               {"AttributeName": "SK", "AttributeType": "S"}])
        tables[kind] = res.Table(name)
    monkeypatch.setenv("RESERVATION_TABLE_NAME", "test-reservation")
    monkeypatch.setenv("ORDER_TABLE_NAME", "test-order")
    monkeypatch.setenv("CATERING_TABLE_NAME", "test-catering")
    monkeypatch.setenv("CATERING_REQUEST_HISTORY_TABLE_NAME", "test-history")
    for sk, entry in (("AUDIT#2025-01-01T10:00:00Z#1", {"action": "requested", "by": "guest", "ip": "1.2.3.4"}),
                      ("AUDIT#2025-01-02T10:00:00Z#2", {"action": "offer_sent", "by": "staff-sub", "version": 1}),
                      ("AUDIT#2025-01-03T10:00:00Z#3", {"action": "signed", "by": "customer", "ip": "1.2.3.4",
                                                        "method": "bankid", "evidence": "s3/key"})):
        tables["history"].put_item(Item={"PK": "REQUEST#q1", "SK": sk, "at": sk[6:26], **entry})
    pk = f"LOCATION#{LOC_A}"
    r = tables["reservation"]
    r.put_item(Item={"PK": pk, "SK": "RESERVATION#2025-01-10#old", "reservationId": "old", "date": "2025-01-10",
                     "status": "arrived", "manageTokenHash": "h", **ANNA})
    r.put_item(Item={"PK": pk, "SK": "RESERVATION#2999-01-10#next", "reservationId": "next", "date": "2999-01-10",
                     "status": "reserved", **ANNA})
    r.put_item(Item={"PK": pk, "SK": "RESERVATION#2025-01-10#other", "reservationId": "other",
                     "date": "2025-01-10", "status": "arrived", "customerName": "Bo", "customerEmail": "bo@x.se"})
    r.put_item(Item={"PK": f"LOCATION#{LOC_B}", "SK": "RESERVATION#2025-01-10#b", "reservationId": "b",
                     "date": "2025-01-10", "status": "arrived", **ANNA})  # another restaurant's guest record
    tables["order"].put_item(Item={"PK": pk, "SK": "ORDER#2025-02-01#o1", "orderId": "o1", "date": "2025-02-01",
                                   "status": "completed", "paymentStatus": "paid", "totalAmount": 12900,
                                   "lines": [{"name": "Soppa", "qty": 1}], **ANNA, "customerEmail": "anna@example.se"})
    tables["catering"].put_item(Item={"PK": pk, "SK": "REQUEST#q1", "requestId": "q1", "status": "closed",
                                      "customerType": "company", "companyName": "Firma AB",
                                      "deliveryAddress": {"street": "G 1"},
                                      "signature": {"name": "Anna Gäst", "personalNumberMasked": "19800101-****",
                                                    "signedAt": "2025-01-03T10:00:00Z", "evidenceKey": "s3/key"},
                                      "invoiceNumber": "F-1", **ANNA})
    consent.grant(env["log"], tenant_id=TENANT_A, restaurant="Bistro Linnea", version=consent.CURRENT,
                  channels=["email"], email="anna@example.se", source="booking")
    mk = env["marketing"]
    mk.put_item(Item={**m.sub_key(TENANT_A, m.subscriber_id(TENANT_A, "anna@example.se")),
                      "email": "anna@example.se", "status": "subscribed", "locations": {LOC_A}})
    mk.put_item(Item={**offer.email_key(TENANT_A, offer.owner_key(TENANT_A, "anna@example.se")), "code": "ABCD-EFGH"})
    mk.put_item(Item={**offer.code_key(TENANT_A, "ABCD-EFGH"), "code": "ABCD-EFGH", "status": "issued",
                      "percent": Decimal(10)})
    return tables


def test_export_lists_everything_about_the_guest_in_this_restaurant(env, guest):
    status, body = call(env, "GET", "/privacy/guests", query={"email": "ANNA@example.se "})
    assert status == 200
    assert sorted(b["reservationId"] for b in body["bookings"]) == ["next", "old"]  # not LOC_B, not Bo
    assert "manageTokenHash" not in body["bookings"][0] and "PK" not in body["bookings"][0]
    assert [o["orderId"] for o in body["orders"]] == ["o1"]
    (req,) = body["cateringRequests"]
    assert req["requestId"] == "q1"
    assert req["signature"] == {"name": "Anna Gäst", "personalNumberMasked": "19800101-****",
                                "signedAt": "2025-01-03T10:00:00Z"}  # no evidence key
    assert [e["action"] for e in req["events"]] == ["signed", "requested"]  # the guest's own, not staff's
    assert req["events"][0] == {"action": "signed", "at": "2025-01-03T10:00:00Z", "ip": "1.2.3.4", "method": "bankid"}
    assert body["subscriber"]["status"] == "subscribed" and body["subscriber"]["locations"] == [LOC_A]
    assert body["welcomeOfferCode"] == {"code": "ABCD-EFGH", "percent": 10, "status": "issued"}
    assert body["consents"]["email"]["state"] == "granted"
    by_phone = call(env, "GET", "/privacy/guests", query={"phone": "070-123 45 67"})[1]
    assert len(by_phone["bookings"]) == 2


def test_export_needs_owner_and_a_contact(env, guest):
    assert call(env, "GET", "/privacy/guests", query={"email": "a@b.se"}, role="staff_user")[0] == 403
    assert call(env, "GET", "/privacy/guests", query={})[0] == 400


def test_erase_anonymises_keeps_receipts_and_coming_bookings(env, guest):
    status, body = call(env, "POST", "/privacy/guests/erase", {"email": "anna@example.se", "phone": "0701234567"})
    assert status == 200
    assert body["consentWithdrawn"] == ["email"] and body["marketingRowsDeleted"] == 3 and body["anonymised"] == 3
    assert body["kept"] == [{"kind": "booking", "locationId": LOC_A, "status": "reserved", "date": "2999-01-10",
                             "id": "next"}]
    pk = f"LOCATION#{LOC_A}"
    old = guest["reservation"].get_item(Key={"PK": pk, "SK": "RESERVATION#2025-01-10#old"})["Item"]
    assert old["customerName"] == "Anonymiserad" and "customerEmail" not in old and "notes" not in old
    nxt = guest["reservation"].get_item(Key={"PK": pk, "SK": "RESERVATION#2999-01-10#next"})["Item"]
    assert nxt["customerEmail"] == "Anna@Example.se"
    other = guest["reservation"].get_item(Key={"PK": f"LOCATION#{LOC_B}", "SK": "RESERVATION#2025-01-10#b"})["Item"]
    assert other["customerEmail"] == "Anna@Example.se"  # another restaurant's data is never touched
    order = guest["order"].get_item(Key={"PK": pk, "SK": "ORDER#2025-02-01#o1"})["Item"]
    assert "customerPhone" not in order and order["totalAmount"] == 12900 and order["lines"]
    req = guest["catering"].get_item(Key={"PK": pk, "SK": "REQUEST#q1"})["Item"]
    assert "customerEmail" not in req and req["companyName"] == "Firma AB" and req["invoiceNumber"] == "F-1"
    assert req["signature"]["name"] == "Anna Gäst" and req["deliveryAddress"] == {"street": "G 1"}
    assert env["marketing"].scan()["Items"] == []
    assert not consent.may_send(env["log"], TENANT_A, "email", "anna@example.se")
    latest = consent.latest(env["log"], TENANT_A, consent.subject(TENANT_A, "email", "anna@example.se"))
    assert latest["source"] == "erasure" and "identifier" not in latest  # the address is not written again
    # Again: nothing new to anonymise, nothing breaks.
    again = call(env, "POST", "/privacy/guests/erase", {"email": "anna@example.se"})[1]
    assert again["anonymised"] == 0 and again["marketingRowsDeleted"] == 0


def test_erase_keeps_the_block_of_a_bounced_subscriber(env, guest):
    key = m.sub_key(TENANT_A, m.subscriber_id(TENANT_A, "anna@example.se"))
    env["marketing"].update_item(Key=key, UpdateExpression="SET #s = :b, suppressedAt = :t",
                                 ExpressionAttributeNames={"#s": "status"},
                                 ExpressionAttributeValues={":b": "bounced", ":t": "2026-10-01T00:00:00Z"})
    call(env, "POST", "/privacy/guests/erase", {"email": "anna@example.se"})
    row = env["marketing"].get_item(Key=key)["Item"]
    assert set(row) == {"PK", "SK", "status", "suppressedAt", "anonymisedAt"} and row["status"] == "bounced"


def test_erase_keeps_a_code_used_at_checkout(env, guest):
    env["marketing"].update_item(Key=offer.code_key(TENANT_A, "ABCD-EFGH"), UpdateExpression="SET #s = :r",
                                 ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":r": "redeemed"})
    assert call(env, "POST", "/privacy/guests/erase", {"email": "anna@example.se"})[1]["marketingRowsDeleted"] == 2
    assert [i["SK"] for i in env["marketing"].scan()["Items"]] == ["CODE#ABCD-EFGH"]

"""functions/accounting: the account mapping (owner only, version check,
reset by sending the defaults) and the export of a month with orders, a
Kassa receipt, a card guarantee fee, a catering invoice and Stripe
balance transactions, as SIE, CSV and JSON. DynamoDB is moto; Stripe's
balance transaction list is a fake. Tenant isolation: another tenant's
location answers 404, staff 403."""

import base64
import json
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

from shared import accounting as acc
from shared import dynamo as shared_dynamo
from shared import stripe_client
from tenant_support import (LOC_A, LOC_B, OWNER_A_SUB, REGION, STAFF_A_SUB, TENANT_A, TENANT_B, TENANT_TABLE,
                            create_tables, tenant_claims, tenant_row)
from test_card_payments import load_fn, location_item
from test_accounting import parse_sie

ACCOUNT = "acct_roma"
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
TABLES = {
    "ORDER_TABLE_NAME": "test-order", "RESERVATION_TABLE_NAME": "test-reservation",
    "CATERING_REQUESTS_TABLE_NAME": "test-catering", "CATERING_REQUEST_HISTORY_TABLE_NAME": "test-catering-history",
    "KASSA_TABLE_NAME": "test-kassa",
}
REQ = "c" * 32
REG = "e" * 32


def _table(res, name, extra=(), gsis=()):
    res.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("PK", "SK", *extra)],
        BillingMode="PAY_PER_REQUEST", **({"GlobalSecondaryIndexes": list(gsis)} if gsis else {}))


class FakeBalance:
    def __init__(self):
        self.calls = []
        start = int(datetime(2026, 10, 20, 9, tzinfo=timezone.utc).timestamp())
        self.txns = [
            {"id": "txn_1", "type": "charge", "amount": 34900, "fee": 610, "currency": "sek", "created": start},
            {"id": "txn_2", "type": "charge", "amount": 40000, "fee": 700, "currency": "sek", "created": start + 9},
            {"id": "txn_3", "type": "payout", "amount": -50000, "fee": 0, "currency": "sek", "created": start + 99},
            {"id": "txn_4", "type": "charge", "amount": 5300, "fee": 0, "currency": "sek", "created": start + 86400},
        ]

    def __call__(self, method, path, params=None, *, account=None, idempotency_key=None):
        assert method == "GET" and path == "/v1/balance_transactions" and account == ACCOUNT
        self.calls.append(params)
        after = params.get("starting_after")
        rest = self.txns[[t["id"] for t in self.txns].index(after) + 1:] if after else self.txns
        rest = [t for t in rest if params["created"]["gte"] <= t["created"] < params["created"]["lt"]]
        return {"data": rest[:2], "has_more": len(rest) > 2}


@pytest.fixture
def env(monkeypatch):
    for key, value in {"ENVIRONMENT": "dev", "AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t",
                       "AWS_SESSION_TOKEN": "t", "AWS_DEFAULT_REGION": REGION, "USER_TABLE_NAME": "test-user",
                       "KASSA_JOURNAL_TABLE_NAME": "test-kassa-journal", **TABLES}.items():
        monkeypatch.setenv(key, value)
    fake = FakeBalance()
    monkeypatch.setattr(stripe_client, "request", fake)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        res = boto3.resource("dynamodb", region_name=REGION)
        for name in TABLES.values():
            _table(res, name)
        _table(res, "test-kassa-journal", ("regDay",), [
            {"IndexName": "byDay", "Projection": {"ProjectionType": "ALL"},
             "KeySchema": [{"AttributeName": "regDay", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}]}])
        create_tables(monkeypatch)
        res.Table(TENANT_TABLE).put_item(Item=tenant_row(TENANT_A, name="Roma", stripe={"accountId": ACCOUNT},
                                                         legalName="Rökeri Åkesson AB", orgNumber="556677-8899"))
        res.Table(TENANT_TABLE).put_item(Item=tenant_row(TENANT_B, orgNumber="556000-0001"))
        res.Table("test-location").put_item(Item=location_item(TENANT_A, LOC_A))
        res.Table("test-location").put_item(Item=location_item(TENANT_B, LOC_B))
        pk = f"LOCATION#{LOC_A}"
        res.Table("test-order").put_item(Item={
            "PK": pk, "SK": f"ORDER#2026-10-20#{'a' * 32}", "orderId": "a" * 32, "paymentStatus": "paid",
            "confirmedAt": "2026-10-20T09:00:00Z", "totalAmount": 34900,
            "vat": [{"rate": 12, "gross": 22400, "net": 20000, "vat": 2400},
                    {"rate": 25, "gross": 12500, "net": 10000, "vat": 2500}]})
        res.Table("test-reservation").put_item(Item={
            "PK": pk, "SK": f"RESERVATION#2026-10-20#{'b' * 32}",
            "payment": {"status": "succeeded", "amount": 40000, "settledAt": "2026-10-20T09:00:09Z"}})
        res.Table("test-kassa").put_item(Item={"PK": pk, "SK": f"REGISTER#{REG}", "registerId": REG, "label": "K1"})
        res.Table("test-kassa-journal").put_item(Item={
            "PK": f"REG#{REG}", "SK": "J#000000000002", "jseq": 2, "regDay": f"REG#{REG}#2026-10-21",
            "type": "kassakvitto", "local": "2026-10-21T19:00:00",
            "data": {"total": 5300, "vat": [{"rate": 6, "gross": 5300, "net": 5000, "vat": 300}],
                     "payments": [{"method": "Kort", "amount": 5300}]}})
        res.Table("test-catering").put_item(Item={
            "PK": pk, "SK": f"REQUEST#{REQ}", "requestId": REQ, "locationId": LOC_A, "status": "signed",
            "stripeInvoiceId": "in_1", "signedAt": "2026-10-22T10:00:00Z", "signedOfferVersion": 1,
            "updatedAt": "2026-10-22T10:00:00Z"})
        res.Table("test-catering-history").put_item(Item={
            "PK": f"REQUEST#{REQ}", "SK": "OFFER#v0001", "version": 1,
            "totals": {"total": 112000, "vat": 12000, "vatRate": 12}})
        yield res, fake
        shared_dynamo._resource = None
        shared_dynamo._client = None


@pytest.fixture
def app(env):
    module = load_fn("accounting", "accounting_app")
    module.now_utc = lambda: NOW
    return module


def call(app, method, path, *, location=LOC_A, body=None, query=None, tenant_id=TENANT_A, role="owner_user"):
    sub = OWNER_A_SUB if role == "owner_user" else STAFF_A_SUB
    template = "/locations/{locationId}/accounting" + path
    event = {"routeKey": f"{method} {template}", "pathParameters": {"locationId": location},
             "queryStringParameters": query,
             "requestContext": {"http": {"method": method}, "authorizer": {"jwt": {
                 "claims": tenant_claims(sub, f'["{role}"]', tenant_id)}}}}
    if body is not None:
        event["body"] = json.dumps(body)
    response = app.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def test_settings_default_save_conflict_and_reset(app):
    status, view = call(app, "GET", "")
    assert status == 200 and view["accounts"]["stripe"] == "1580" and view["version"] == 0
    assert view["defaults"]["sales12"] == "3002" and view["series"] == "B"
    status, saved = call(app, "PUT", "", body={"accounts": {**view["accounts"], "stripe": "1581"}, "series": "A",
                                                "fiscalYearStartMonth": 5, "version": 0})
    assert status == 200 and saved["accounts"]["stripe"] == "1581" and saved["version"] == 1
    assert call(app, "PUT", "", body={"accounts": {}, "version": 0}) == (409, {"error": "changed_retry"})
    status, error = call(app, "PUT", "", body={"accounts": {"bank": "193"}, "version": 1})
    assert status == 400 and "4-digit" in error["error"]
    status, reset = call(app, "PUT", "", body={"accounts": view["defaults"], "version": 1})
    assert status == 200 and reset["accounts"] == view["defaults"] and reset["version"] == 2
    stored = boto3.resource("dynamodb", region_name=REGION).Table("test-location").get_item(
        Key={"PK": f"TENANT#{TENANT_A}", "SK": f"LOCATION#{LOC_A}"})["Item"]["accounting"]
    assert stored["accounts"] == {}


def test_export_json_month_balances_and_reconciles(app, env):
    status, body = call(app, "GET", "/export", query={"from": "2026-10-01", "to": "2026-10-31", "format": "json"})
    assert status == 200, body
    summary = body["summary"]
    assert summary["days"] == 31 and summary["bookedDays"] == 3 and summary["balanced"]
    assert summary["sales"] == 30000 + 35714 + 5000 + 100000 and summary["vat"] == 4900 + 4286 + 300 + 12000
    assert summary["verifications"] == 3 and summary["locations"] == [{"locationId": LOC_A, "name": "Södermalm"}]
    assert summary["warnings"] == []
    day = {e["date"]: e for e in body["entries"]}
    lines = {(ln["account"], ln["text"]): ln["amount"] for ln in day["2026-10-20"]["lines"]}
    assert lines[("3002", f"{acc.T_FEE_SALES} 12 % moms")] == -35714
    assert lines[("2621", "Utgående moms 12 %")] == -2400 - 4286
    assert lines[("6570", acc.T_FEES)] == 1310
    assert lines[("1930", acc.T_PAYOUT)] == 50000
    assert sum(ln["amount"] for ln in day["2026-10-20"]["lines"]) == 0
    assert {ln["account"] for ln in day["2026-10-22"]["lines"]} == {"1510", "3002", "2621"}
    assert day["2026-10-21"]["lines"][0]["name"] == "Försäljning inom Sverige, 6 % moms"
    fake = env[1]
    assert len(fake.calls) == 2 and fake.calls[1]["starting_after"] == "txn_2"
    assert fake.calls[0]["created"]["gte"] == int(datetime(2026, 9, 30, 22, tzinfo=timezone.utc).timestamp())
    assert fake.calls[0]["created"]["lt"] == int(datetime(2026, 10, 31, 23, tzinfo=timezone.utc).timestamp())


def test_export_sie_and_csv_files(app):
    status, body = call(app, "GET", "/export", query={"from": "2026-10-01", "to": "2026-10-31", "format": "sie"})
    assert status == 200 and body["filename"] == "booqy-sodermalm-2026-10-01-2026-10-31.se"
    raw = base64.b64decode(body["content"])
    records = parse_sie(raw)
    assert ("#FNAMN", ["Rökeri Åkesson AB"]) in records and b"R\x94keri \x8fkesson AB" in raw
    assert len([r for r in records if r[0] == "#VER"]) == 3
    status, body = call(app, "GET", "/export", query={"from": "2026-10-20", "to": "2026-10-20", "format": "csv"})
    text = base64.b64decode(body["content"]).decode("utf-8-sig")
    assert body["mediaType"].startswith("text/csv") and "1580;Fordringar för kontokort och kuponger" in text


def test_a_day_with_reconciliation_difference_warns(app, env):
    env[1].txns[0]["amount"] = 35000
    status, body = call(app, "GET", "/export", query={"from": "2026-10-20", "to": "2026-10-20", "format": "json"})
    assert status == 200 and "stäm av" in body["summary"]["warnings"][0]["text"]


def test_bad_requests_and_isolation(app):
    assert call(app, "GET", "/export", query={"from": "2026-10-31", "to": "2026-10-01"})[0] == 400
    assert call(app, "GET", "/export", query={"from": "2026-10-01", "to": "2026-10-02", "format": "pdf"})[0] == 400
    assert call(app, "GET", "", location=LOC_B)[0] == 404
    assert call(app, "GET", "", role="staff_user")[0] == 403
    assert call(app, "GET", "/export", role="staff_user", query={"from": "2026-10-01", "to": "2026-10-02"})[0] == 403
    assert call(app, "DELETE", "")[0] == 405


def test_stripe_refusal_is_a_502(app, monkeypatch):
    def refuse(*args, **kwargs):
        raise stripe_client.StripeError(403, {"message": "key lacks rak_balance_transaction_read"})
    monkeypatch.setattr(stripe_client, "request", refuse)
    status, body = call(app, "GET", "/export", query={"from": "2026-10-20", "to": "2026-10-20"})
    assert (status, body) == (502, {"error": "stripe_refused"})


def test_locations_sharing_the_stripe_account_get_one_file_with_cost_centres(app, env):
    res, _ = env
    res.Table("test-location").put_item(Item=location_item(TENANT_A, "loc-a-2", name="Kungsholmen",
                                                           createdAt="2026-09-01T10:00:00Z"))
    res.Table("test-order").put_item(Item={
        "PK": "LOCATION#loc-a-2", "SK": f"ORDER#2026-10-20#{'f' * 32}", "orderId": "f" * 32, "paymentStatus": "paid",
        "confirmedAt": "2026-10-20T11:00:00Z", "totalAmount": 11200,
        "vat": [{"rate": 12, "gross": 11200, "net": 10000, "vat": 1200}]})
    env[1].txns.insert(2, {"id": "txn_k", "type": "charge", "amount": 11200, "fee": 200, "currency": "sek",
                           "created": int(datetime(2026, 10, 20, 11, tzinfo=timezone.utc).timestamp())})
    status, body = call(app, "GET", "/export", query={"from": "2026-10-20", "to": "2026-10-20", "format": "json"})
    assert status == 200, body
    assert [loc["locationId"] for loc in body["summary"]["locations"]] == [LOC_A, "loc-a-2"]
    assert body["summary"]["warnings"] == []
    entries = body["entries"]
    assert [e["number"] for e in entries] == [29300, 29301, 29302]
    company, roma, kungsholmen = entries
    assert company["locationId"] is None and {ln["account"] for ln in company["lines"]} == {"6570", "1580", "1930"}
    assert sum(ln["amount"] for ln in company["lines"] if ln["account"] == "6570") == 610 + 700 + 200
    assert all("costCenter" not in ln for ln in company["lines"])
    assert {ln["costCenter"] for ln in kungsholmen["lines"] if ln["account"] == "3002"} == {"LOCA2"}
    assert all("costCenter" not in ln for ln in roma["lines"] if ln["account"] in ("1580", "2621"))
    assert all(sum(ln["amount"] for ln in e["lines"]) == 0 for e in entries)
    status, body = call(app, "GET", "/export", location="loc-a-2",
                        query={"from": "2026-10-20", "to": "2026-10-20", "format": "sie"})
    raw = base64.b64decode(body["content"]).decode("cp437")
    assert '#OBJEKT 1 "LOCA1" "S\u00f6dermalm"' in raw and '#OBJEKT 1 "LOCA2" "Kungsholmen"' in raw
    assert '#TRANS 3002 {1 "LOCA2"} -100.00' in raw


def test_cost_centre_is_saved_and_defaults_from_the_location_id(app):
    status, view = call(app, "GET", "")
    assert view["costCenter"] == view["defaultCostCenter"] == "LOCA1"
    status, saved = call(app, "PUT", "", body={"accounts": view["accounts"], "costCenter": "SOD", "version": 0})
    assert status == 200 and saved["costCenter"] == "SOD"
    assert call(app, "PUT", "", body={"costCenter": "söder", "version": 1})[0] == 400

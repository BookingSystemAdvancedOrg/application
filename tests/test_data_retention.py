"""data-retention: deletes or anonymises guest data whose time is up, writes
nothing in dry run, keeps receipts, is idempotent and logs counts only."""

import importlib.util
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from shared import consent, dynamo as shared_dynamo, marketing
from test_consent import consent_table

APP = Path(__file__).parents[1] / "functions" / "data-retention" / "app.py"
REGION = "eu-north-1"
NOW = datetime(2026, 10, 22, 3, 0, tzinfo=timezone.utc)
T, OTHER = "01tttttttttttttttttttttttt", "01oooooooooooooooooooooooo"
LOC = "loc-1"
PK = f"LOCATION#{LOC}"


def _table(res, name, gsis=()):
    attrs = [{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}]
    indexes = []
    for index, hash_key, range_key in gsis:
        attrs.append({"AttributeName": hash_key, "AttributeType": "S"})
        schema = [{"AttributeName": hash_key, "KeyType": "HASH"}]
        if range_key:
            attrs.append({"AttributeName": range_key, "AttributeType": "S"})
            schema.append({"AttributeName": range_key, "KeyType": "RANGE"})
        indexes.append({"IndexName": index, "KeySchema": schema, "Projection": {"ProjectionType": "ALL"}})
    res.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
                     AttributeDefinitions=attrs, **({"GlobalSecondaryIndexes": indexes} if indexes else {}))
    return res.Table(name)


class FakeClient:
    def __init__(self):
        self.calls = []

    def publish(self, **kwargs):
        self.calls.append(kwargs)

    def invoke(self, **kwargs):
        self.calls.append(kwargs)


class Context:
    invoked_function_arn = "arn:aws:lambda:eu-north-1:123456789012:function:dev-data-retention:live"

    def __init__(self, remaining=600_000):
        self.remaining = remaining

    def get_remaining_time_in_millis(self):
        return self.remaining


@pytest.fixture
def env(monkeypatch):
    values = {
        "ENVIRONMENT": "dev", "AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t", "AWS_DEFAULT_REGION": REGION,
        "TENANT_TABLE_NAME": "test-tenant", "TENANT_LIST_INDEX_NAME": "GSI1", "LOCATION_TABLE_NAME": "test-location",
        "RESERVATION_TABLE_NAME": "test-reservation", "ORDER_TABLE_NAME": "test-order",
        "CATERING_REQUESTS_TABLE_NAME": "test-catering", "CATERING_REQUEST_HISTORY_TABLE_NAME": "test-history",
        "CONSENT_TABLE_NAME": "test-consent", "CONSENT_TENANT_INDEX_NAME": "byTenant",
        "MARKETING_TABLE_NAME": "test-marketing", "ALERT_TOPIC_ARN": "arn:aws:sns:eu-north-1:123456789012:alerts",
        "DRY_RUN": "false",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        res = boto3.resource("dynamodb", region_name=REGION)
        consent_table(res)
        t = {
            "tenant": _table(res, "test-tenant", [("GSI1", "GSI1PK", "GSI1SK")]),
            "location": _table(res, "test-location"),
            "reservation": _table(res, "test-reservation"),
            "order": _table(res, "test-order"),
            "catering": _table(res, "test-catering"),
            "history": _table(res, "test-history"),
            "marketing": _table(res, "test-marketing"),
            "consent": res.Table("test-consent"),
        }
        spec = importlib.util.spec_from_file_location("data_retention_app", APP)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "now_utc", lambda: NOW)
        fakes = {"sns": FakeClient(), "lambda": FakeClient()}
        module._clients.update(fakes)
        seed(t)
        yield {"app": module, "t": t, **fakes}
        shared_dynamo._resource = None
        shared_dynamo._client = None


def seed(t):
    for tenant_id, status in ((T, "active"), (OTHER, "purged")):
        t["tenant"].put_item(Item={"PK": f"TENANT#{tenant_id}", "SK": "PROFILE", "GSI1PK": "TENANT",
                                   "GSI1SK": tenant_id, "tenantId": tenant_id, "status": status})
        t["location"].put_item(Item={"PK": f"TENANT#{tenant_id}", "SK": f"LOCATION#{LOC if tenant_id == T else 'x'}",
                                     "locationId": LOC if tenant_id == T else "x"})
    guest = {"customerName": "Anna Gäst", "customerEmail": "anna@example.se", "customerPhone": "+46701234567",
             "notes": "nötallergi"}
    r = t["reservation"]
    for rid, day, extra in (("old", "2025-08-01", {}), ("new", "2026-10-01", {}),
                            ("fee-old", "2025-01-10", {"payment": {"status": "succeeded"}}),
                            ("fee-new", "2026-10-10", {"payment": {"status": "succeeded"}, "ttl": 1})):
        r.put_item(Item={"PK": PK, "SK": f"RESERVATION#{day}#{rid}", "reservationId": rid, "date": day,
                         "tenantId": T, "status": "reserved", **guest, **extra})
        r.put_item(Item={"PK": PK, "SK": f"RID#{rid}", "date": day})
    o = t["order"]
    for oid, day, pay in (("paid-old", "2024-06-01", "paid"), ("paid-new", "2026-09-01", "paid"),
                          ("failed-old", "2025-06-01", "failed"), ("ancient", "2017-03-01", "paid")):
        o.put_item(Item={"PK": PK, "SK": f"ORDER#{day}#{oid}", "orderId": oid, "date": day, "paymentStatus": pay,
                         "totalAmount": 12900, "vat": [{"rate": 12}], **guest})
        o.put_item(Item={"PK": PK, "SK": f"OID#{oid}", "date": day})
    c = t["catering"]
    for qid, status, extra in (("declined-old", "declined", {"updatedAt": "2025-06-01T00:00:00Z"}),
                               ("declined-new", "declined", {"updatedAt": "2026-06-01T00:00:00Z"}),
                               ("invoiced", "closed", {"stripeInvoiceId": "in_1", "customerType": "company",
                                                       "companyName": "Firma AB", "deliveryAddress": {"street": "G 1"}})):
        c.put_item(Item={"PK": PK, "SK": f"REQUEST#{qid}", "requestId": qid, "status": status,
                         "deliveryDate": "2024-09-01", **guest, **extra})
        t["history"].put_item(Item={"PK": f"REQUEST#{qid}", "SK": "AUDIT#1", "ip": "1.2.3.4"})
    # consent: one withdrawn long ago, one granted
    consent.grant(t["consent"], tenant_id=T, restaurant="Roma", version=consent.CURRENT, channels=["email"],
                  email="gone@example.se", source="booking", at="2025-01-01T00:00:00.000Z")
    consent.withdraw(t["consent"], tenant_id=T, channel="email", identifier="gone@example.se", source="admin",
                     at="2026-08-01T00:00:00.000Z")
    consent.grant(t["consent"], tenant_id=T, restaurant="Roma", version=consent.CURRENT, channels=["email"],
                  email="stay@example.se", source="booking", at="2025-01-01T00:00:00.000Z")
    t["marketing"].put_item(Item={**marketing.sub_key(T, marketing.subscriber_id(T, "gone@example.se")),
                                  "status": "unsubscribed", "email": "gone@example.se"})


def keys(table, prefix=""):
    return sorted(i["SK"] for i in table.scan()["Items"] if i["SK"].startswith(prefix))


def test_dry_run_writes_nothing_and_counts(env, monkeypatch, caplog):
    monkeypatch.setenv("DRY_RUN", "true")
    before = {name: table.scan()["Items"] for name, table in env["t"].items()}
    with caplog.at_level(logging.INFO):
        summary = env["app"].handler({}, Context())
    after = {name: table.scan()["Items"] for name, table in env["t"].items()}
    assert before == after
    assert summary["dryRun"] is True
    assert summary["counts"] == {
        "bookingsAnonymised": 1, "bookingsDeleted": 1, "bookingsProtected": 1,
        "cateringAnonymised": 1, "cateringDeleted": 1,
        "consentSuppressed": 1, "ordersAnonymised": 1, "ordersDeleted": 2,
    }


def test_dry_run_is_the_default(env, monkeypatch):
    monkeypatch.delenv("DRY_RUN")
    assert env["app"].handler({}, Context())["dryRun"] is True


def test_run_deletes_anonymises_and_keeps_receipts(env):
    t = env["t"]
    env["app"].handler({}, Context())
    # bookings: the old plain one gone with its pointer, the recent one untouched
    assert keys(t["reservation"]) == ["RESERVATION#2025-01-10#fee-old", "RESERVATION#2026-10-01#new",
                                      "RESERVATION#2026-10-10#fee-new", "RID#fee-new", "RID#fee-old", "RID#new"]
    fee_old = t["reservation"].get_item(Key={"PK": PK, "SK": "RESERVATION#2025-01-10#fee-old"})["Item"]
    assert fee_old["customerName"] == "Anonymiserad" and "customerEmail" not in fee_old and "notes" not in fee_old
    assert fee_old["payment"] == {"status": "succeeded"}
    fee_new = t["reservation"].get_item(Key={"PK": PK, "SK": "RESERVATION#2026-10-10#fee-new"})["Item"]
    assert int(fee_new["ttl"]) > int(NOW.timestamp()) + 6 * 365 * 86400
    assert fee_new["customerEmail"] == "anna@example.se"
    # orders: receipts stay, contact details go; unpaid and expired receipts deleted with the pointer
    assert keys(t["order"]) == ["OID#paid-new", "OID#paid-old", "ORDER#2024-06-01#paid-old",
                                "ORDER#2026-09-01#paid-new"]
    paid_old = t["order"].get_item(Key={"PK": PK, "SK": "ORDER#2024-06-01#paid-old"})["Item"]
    assert paid_old["customerName"] == "Anonymiserad" and "customerPhone" not in paid_old
    assert paid_old["totalAmount"] == 12900 and paid_old["vat"] == [{"rate": 12}]
    # catering: the old declined quote and its history gone; the invoice kept, contact stripped
    assert keys(t["catering"]) == ["REQUEST#declined-new", "REQUEST#invoiced"]
    assert sorted(i["PK"] for i in t["history"].scan()["Items"]) == ["REQUEST#declined-new", "REQUEST#invoiced"]
    invoiced = t["catering"].get_item(Key={"PK": PK, "SK": "REQUEST#invoiced"})["Item"]
    assert invoiced["companyName"] == "Firma AB" and invoiced["deliveryAddress"] == {"street": "G 1"}
    assert "customerEmail" not in invoiced
    # consent: the withdrawn guest is a bare suppression record, the other is untouched
    gone = consent.subject(T, "email", "gone@example.se")
    records = consent.history(t["consent"], T, gone)
    assert len(records) == 1 and records[0]["action"] == "suppressed"
    assert "identifier" not in records[0] and "text" not in records[0]
    assert not consent.may_send(t["consent"], T, "email", "gone@example.se")
    assert consent.may_send(t["consent"], T, "email", "stay@example.se")
    assert t["marketing"].scan()["Items"] == []


def test_second_run_changes_nothing(env):
    env["app"].handler({}, Context())
    snapshot = {name: table.scan()["Items"] for name, table in env["t"].items()}
    summary = env["app"].handler({}, Context())
    assert {name: table.scan()["Items"] for name, table in env["t"].items()} == snapshot
    assert summary["counts"] == {}


def test_logs_hold_counts_only(env, caplog):
    with caplog.at_level(logging.INFO):
        env["app"].handler({}, Context())
    text = caplog.text
    for secret in ("anna@example.se", "Anna", "+46701234567", "gone@example.se", "nötallergi"):
        assert secret not in text


def test_purged_tenants_are_skipped(env):
    assert env["app"].handler({}, Context())["tenants"] == 1


def test_out_of_time_continues_after_the_last_tenant(env):
    summary = env["app"].handler({}, Context(remaining=1_000))
    assert summary["continued"] is True
    assert json.loads(env["lambda"].calls[0]["Payload"]) == {"after": "", "round": 1}


def _subscribers(t):
    sub = lambda email, **f: t["marketing"].put_item(Item={  # noqa: E731
        **marketing.sub_key(T, marketing.subscriber_id(T, email)), "email": email, "name": "Gäst", **f})
    sub("pending-old@example.se", status="pending", pendingAt="2026-09-01T00:00:00Z")
    sub("pending-new@example.se", status="pending", pendingAt="2026-10-20T00:00:00Z")
    sub("bounced@example.se", status="bounced", suppressedAt="2026-08-01T00:00:00Z", lastVisit="2026-01-01")
    sub("complained-ancient@example.se", status="complained", suppressedAt="2023-01-01T00:00:00Z")
    sub("active@example.se", status="subscribed", consentAt="2020-01-01T00:00:00Z")


def test_subscriber_rows_dry_run_counts_only(env, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    _subscribers(env["t"])
    before = env["t"]["marketing"].scan()["Items"]
    counts = env["app"].handler({}, Context())["counts"]
    assert env["t"]["marketing"].scan()["Items"] == before
    assert (counts["subscribersPendingDeleted"], counts["subscribersSuppressedStripped"],
            counts["subscribersSuppressedDeleted"]) == (1, 1, 1)


def test_subscriber_rows_expire_and_bounces_keep_a_hashed_block(env):
    t = env["t"]
    _subscribers(t)
    env["app"].handler({}, Context())
    rows = {i["SK"]: i for i in t["marketing"].scan()["Items"]}
    sid = lambda email: f"SUB#{marketing.subscriber_id(T, email)}"  # noqa: E731
    assert set(rows) == {sid("pending-new@example.se"), sid("bounced@example.se"), sid("active@example.se")}
    bounced = rows[sid("bounced@example.se")]
    assert set(bounced) == {"PK", "SK", "status", "suppressedAt", "anonymisedAt"} and bounced["status"] == "bounced"
    # The block still works: a new opt-in for the same address is refused.
    marketing.upsert_subscriber(t["marketing"], tenant_id=T, restaurant="Roma", email="bounced@example.se",
                                name="x", source="booking")
    assert t["marketing"].get_item(Key=marketing.sub_key(T, marketing.subscriber_id(T, "bounced@example.se")))[
        "Item"]["status"] == "bounced"


def test_consent_suppression_keeps_a_bounced_subscriber_block(env):
    t = env["t"]
    t["marketing"].update_item(Key=marketing.sub_key(T, marketing.subscriber_id(T, "gone@example.se")),
                               UpdateExpression="SET #s = :c, suppressedAt = :at",
                               ExpressionAttributeNames={"#s": "status"},
                               ExpressionAttributeValues={":c": "complained", ":at": "2026-10-20T00:00:00Z"})
    env["app"].handler({}, Context())
    (row,) = t["marketing"].scan()["Items"]
    assert row["status"] == "complained"

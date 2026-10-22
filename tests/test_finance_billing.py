"""platform-stripe-webhook /billing: subscription events keep
PROFILE.billing truthful, invoices become revenue once, credit notes reduce
it. DynamoDB and Secrets Manager are moto; signatures are real HMACs."""

import hashlib
import hmac
import importlib.util
import json
import time
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from shared import dynamo as shared_dynamo
from shared import finance
from tenant_support import REGION, TENANT_A, TENANT_B, TENANT_TABLE, seed_tenancy, tenant_row

APP = Path(__file__).parents[1] / "functions" / "platform-stripe-webhook" / "app.py"
FINANCE_TABLE = "test-finance"
CONNECT_SECRET = "whsec_connect_test"
BILLING_SECRET = "whsec_billing_test"
CUS_A = "cus_AAAAAAAAAAAAAA"
CUS_B = "cus_BBBBBBBBBBBBBB"
SUB_A = "sub_AAAAAAAAAAAAAA"


def _table(ddb, name):
    ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
                     AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                           {"AttributeName": "SK", "AttributeType": "S"}])


@pytest.fixture
def app(monkeypatch):
    for key, value in {"ENVIRONMENT": "dev", "AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t",
                       "AWS_SESSION_TOKEN": "t", "AWS_DEFAULT_REGION": REGION,
                       "FINANCE_TABLE_NAME": FINANCE_TABLE}.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        sm = boto3.client("secretsmanager", region_name=REGION)
        monkeypatch.setenv("CONNECT_WEBHOOK_SECRET_ARN",
                           sm.create_secret(Name="connect", SecretString=CONNECT_SECRET)["ARN"])
        monkeypatch.setenv("BILLING_WEBHOOK_SECRET_ARN",
                           sm.create_secret(Name="billing", SecretString=BILLING_SECRET)["ARN"])
        seed_tenancy(monkeypatch,
                     tenant_a=tenant_row(TENANT_A, billing={"customerId": CUS_A, "startedAt": "2026-10-01T00:00:00Z"}),
                     tenant_b=tenant_row(TENANT_B, billing={"customerId": CUS_B}))
        _table(boto3.client("dynamodb", region_name=REGION), FINANCE_TABLE)
        table = boto3.resource("dynamodb", region_name=REGION).Table(TENANT_TABLE)
        table.put_item(Item={"PK": f"STRIPE_CUSTOMER#{CUS_A}", "SK": "TENANT", "tenantId": TENANT_A})
        table.put_item(Item={"PK": f"STRIPE_CUSTOMER#{CUS_B}", "SK": "TENANT", "tenantId": TENANT_B})
        spec = importlib.util.spec_from_file_location("platform_stripe_webhook_billing_app", APP)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.reset_cache()
        yield module
        shared_dynamo._resource = None
        shared_dynamo._client = None


def _billing(tenant_id):
    item = boto3.resource("dynamodb", region_name=REGION).Table(TENANT_TABLE).get_item(
        Key={"PK": f"TENANT#{tenant_id}", "SK": "PROFILE"})["Item"]
    return item["billing"]


def _fin(pk, sk):
    return boto3.resource("dynamodb", region_name=REGION).Table(FINANCE_TABLE).get_item(
        Key={"PK": pk, "SK": sk}).get("Item")


def _day(tenant_id, day):
    return _fin(finance.tenant_pk(tenant_id), finance.day_sk(day)) or {}


def _event(evt, *, secret=BILLING_SECRET):
    body = json.dumps(evt)
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return {"rawPath": "/billing", "body": body, "isBase64Encoded": False,
            "headers": {"stripe-signature": f"t={ts},v1={sig}"}}


def _send(app, evt):
    res = app.handler(_event(evt), None)
    assert res["statusCode"] == 200, res
    return json.loads(res["body"])["result"]


def _subscription(customer=CUS_A, *, status="active", created=1_800_000_000, amount=99_000, interval="month",
                  kind="customer.subscription.updated", pause=None, cancel_at=None):
    sub = {"id": SUB_A, "object": "subscription", "customer": customer, "status": status,
           "collection_method": "send_invoice", "cancel_at": cancel_at, "canceled_at": None, "trial_end": None,
           "pause_collection": pause,
           "items": {"data": [{"id": "si_1", "quantity": 1,
                               "current_period_start": 1_790_000_000, "current_period_end": 1_792_600_000,
                               "price": {"id": "price_growth", "unit_amount": amount, "lookup_key": None,
                                         "metadata": {"planId": "growth"},
                                         "recurring": {"interval": interval, "interval_count": 1}}}]}}
    return {"id": f"evt_{created}", "type": kind, "created": created, "livemode": False, "data": {"object": sub}}


def _invoice(customer=CUS_A, *, status="open", created=1_800_000_000, invoice_id="in_1", paid_at=None,
             finalized_at=1_800_000_000, voided_at=None, amount_paid=0, total=123_750, tax=24_750,
             kind="invoice.finalized"):
    inv = {"id": invoice_id, "object": "invoice", "customer": customer, "subscription": SUB_A, "number": "F-0001",
           "status": status, "currency": "sek", "created": 1_799_900_000, "due_date": 1_802_500_000,
           "subtotal": total - tax, "total": total, "total_excluding_tax": total - tax, "tax": tax,
           "amount_due": total, "amount_paid": amount_paid, "amount_remaining": total - amount_paid,
           "period_start": 1_790_000_000, "period_end": 1_792_600_000, "paid_out_of_band": False,
           "hosted_invoice_url": "https://invoice.stripe.com/i/x", "invoice_pdf": "https://pay.stripe.com/x.pdf",
           "metadata": {"tenantId": "x", "periodStart": "2026-09-25", "periodEnd": "2026-10-24"},
           "status_transitions": {"finalized_at": finalized_at, "paid_at": paid_at, "voided_at": voided_at,
                                  "marked_uncollectible_at": None}}
    return {"id": f"evt_{kind}_{created}", "type": kind, "created": created, "livemode": False, "data": {"object": inv}}


def test_subscription_events_are_acknowledged_and_ignored(app):
    # Invoicing is done in arrears by the console; Stripe subscriptions are not used.
    assert _send(app, _subscription()) == "ignored"
    assert _billing(TENANT_A) == {"customerId": CUS_A, "startedAt": "2026-10-01T00:00:00Z"}


def test_unknown_customer_is_ignored(app):
    assert _send(app, _subscription(customer="cus_UNKNOWN0000000")) == "ignored:unknown_customer"


def test_finalized_invoice_books_ex_vat_revenue_once(app):
    assert _send(app, _invoice()) == "applied:invoiced"
    row = _fin(finance.tenant_pk(TENANT_A), "INVOICE#2027-01-14#in_1")
    assert row["status"] == "open" and row["subtotalSek"] == Decimal("990.00") and row["taxSek"] == Decimal("247.50")
    assert row["totalSek"] == Decimal("1237.50") and row["bookedInvoiced"] == "2027-01-15"
    assert row["periodStart"] == "2026-09-25" and row["periodEnd"] == "2026-10-24"
    assert _day(TENANT_A, "2027-01-15")["invoicedSek"] == Decimal("990.00")
    # redelivered: nothing changes
    assert _send(app, _invoice()) == "applied"
    assert _day(TENANT_A, "2027-01-15")["invoicedSek"] == Decimal("990.00")


def test_paid_invoice_books_collected_revenue_and_clears_past_due(app):
    _send(app, _invoice())
    _send(app, _invoice(status="open", created=1_800_050_000, kind="invoice.payment_failed"))
    assert _billing(TENANT_A)["pastDueSince"]
    paid = _invoice(status="paid", created=1_800_100_000, paid_at=1_800_100_000, amount_paid=123_750, kind="invoice.paid")
    assert _send(app, paid) == "applied:paid"
    day = _day(TENANT_A, "2027-01-16")
    assert day["paidSek"] == Decimal("990.00") and "invoicedSek" not in day
    assert _day(TENANT_A, "2027-01-15")["invoicedSek"] == Decimal("990.00")
    b = _billing(TENANT_A)
    assert "pastDueSince" not in b and b["lastPaidAt"]
    assert _fin(finance.tenant_pk(TENANT_A), "INVOICE#2027-01-14#in_1")["status"] == "paid"


def test_payment_failed_starts_past_due(app):
    failed = _invoice(status="open", created=1_800_100_000, kind="invoice.payment_failed")
    assert _send(app, failed).startswith("applied")
    b = _billing(TENANT_A)
    assert b["pastDueSince"] and b["lastPaymentFailedAt"]


def test_voided_invoice_reverses_revenue(app):
    _send(app, _invoice())
    void = _invoice(status="void", created=1_800_200_000, voided_at=1_800_200_000, kind="invoice.voided")
    assert _send(app, void) == "applied:voided"
    assert _day(TENANT_A, "2027-01-17")["invoicedSek"] == Decimal("-990.00")
    assert _day(TENANT_A, "2027-01-15")["invoicedSek"] == Decimal("990.00")


def test_credit_note_reduces_revenue_once(app):
    note = {"id": "cn_1", "object": "credit_note", "customer": CUS_A, "invoice": "in_1", "number": "F-0001-CN-01",
            "reason": "order_change", "created": 1_800_300_000, "subtotal": 50_000, "total": 62_500,
            "total_excluding_tax": 50_000}
    evt = {"id": "evt_cn", "type": "credit_note.created", "created": 1_800_300_000, "livemode": False,
           "data": {"object": note}}
    assert _send(app, evt) == "applied:credited"
    assert _day(TENANT_A, "2027-01-18")["creditedSek"] == Decimal("500.00")
    assert _send(app, evt) == "stale"
    assert _day(TENANT_A, "2027-01-18")["creditedSek"] == Decimal("500.00")


def test_live_event_in_dev_is_ignored(app):
    evt = _invoice()
    evt["livemode"] = True
    assert _send(app, evt) == "ignored:livemode"

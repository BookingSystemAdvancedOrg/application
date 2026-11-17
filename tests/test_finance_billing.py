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
from botocore.exceptions import ClientError
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


def _refund_note(note_id="cn_2"):
    return {"id": note_id, "object": "credit_note", "customer": CUS_A, "invoice": "in_1", "number": "F-0001-CN-02",
            "reason": "requested_by_customer", "created": 1_800_300_000, "subtotal": 10_000, "total": 12_500,
            "total_excluding_tax": 10_000, "metadata": {"booqyMethod": "refund"},
            "refunds": [{"refund": "re_1", "amount_refunded": 12_500}]}


def test_refund_note_reduces_collected_once(app):
    evt = {"id": "evt_cn2", "type": "credit_note.created", "created": 1_800_300_000, "livemode": False,
           "data": {"object": _refund_note()}}
    assert _send(app, evt) == "applied:credited+refunded"
    day = _day(TENANT_A, "2027-01-18")
    assert day["creditedSek"] == Decimal("100.00") and day["refundedSek"] == Decimal("100.00")
    assert _send(app, evt) == "stale"
    day = _day(TENANT_A, "2027-01-18")
    assert day["creditedSek"] == Decimal("100.00") and day["refundedSek"] == Decimal("100.00")
    row = boto3.resource("dynamodb", region_name=REGION).Table(FINANCE_TABLE).get_item(
        Key={"PK": f"TENANT#{TENANT_A}", "SK": "CREDIT#2027-01-18#cn_2"})["Item"]
    assert row["refundedSek"] == Decimal("125.00") and row["totalSek"] == Decimal("125.00")
    assert row["method"] == "refund"


def test_note_written_first_by_the_console_is_booked_once(app):
    """The console writes the CREDIT# row when the operator issues the note;
    the webhook then only books what is not booked yet."""
    from shared import billing as shared_billing
    note = _refund_note("cn_3")
    row = shared_billing.credit_note_row(TENANT_A, note, 1_800_300_000)
    row.pop("day")
    boto3.resource("dynamodb", region_name=REGION).Table(FINANCE_TABLE).put_item(
        Item={**{k: v for k, v in row.items() if v is not None}, "bookedCredited": "2027-01-18"})
    evt = {"id": "evt_cn3", "type": "credit_note.created", "created": 1_800_300_000, "livemode": False,
           "data": {"object": note}}
    assert _send(app, evt) == "stale:refunded"
    day = _day(TENANT_A, "2027-01-18")
    assert "creditedSek" not in day and day["refundedSek"] == Decimal("100.00")


def test_note_row_says_what_it_took_back_from_the_payment(app):
    """postPaymentSek: the console reads it to say what is left to refund. A
    note on an open invoice (pre_payment) takes nothing back from a payment;
    a row written without it gets it on the next delivery."""
    from shared import billing as shared_billing
    table = boto3.resource("dynamodb", region_name=REGION).Table(FINANCE_TABLE)
    pre = {**_refund_note("cn_4"), "type": "pre_payment", "post_payment_amount": 0, "refunds": []}
    evt = {"id": "evt_cn4", "type": "credit_note.created", "created": 1_800_300_000, "livemode": False,
           "data": {"object": pre}}
    _send(app, evt)
    key = {"PK": f"TENANT#{TENANT_A}", "SK": "CREDIT#2027-01-18#cn_4"}
    assert table.get_item(Key=key)["Item"]["postPaymentSek"] == Decimal("0.00")
    table.update_item(Key=key, UpdateExpression="REMOVE postPaymentSek")
    _send(app, {**evt, "id": "evt_cn4b"})
    assert table.get_item(Key=key)["Item"]["postPaymentSek"] == Decimal("0.00")
    assert shared_billing.post_payment_minor({"total": 12_500, "type": "post_payment"}) == 12_500
    assert shared_billing.post_payment_minor({"total": 12_500, "post_payment_amount": 2_500}) == 2_500


def test_refunded_part_of_a_note():
    from shared import billing as shared_billing
    assert shared_billing.refunded_minor({"total": 12_500}) == 0
    assert shared_billing.refunded_minor({"total": 12_500, "refund": "re_1"}) == 12_500
    assert shared_billing.refunded_minor({"total": 12_500, "refund": "re_1", "out_of_band_amount": 2_500}) == 10_000
    assert shared_billing.refunded_minor({"total": 12_500, "refunds": [{"amount_refunded": 99_999}]}) == 12_500


def test_live_event_in_dev_is_ignored(app):
    evt = _invoice()
    evt["livemode"] = True
    assert _send(app, evt) == "ignored:livemode"


# --- cards, several failed invoices and the operators' alerts -----------------------------

@pytest.fixture
def sns(app, monkeypatch):
    """The operators' alerts: published messages, recorded."""
    sent = []
    real = app.boto3.client

    class FakeSns:
        def publish(self, **kw):
            if sent and sent[-1] == "FAIL":
                sent.pop()
                raise ClientError({"Error": {"Code": "Throttling", "Message": "slow"}}, "Publish")
            sent.append(kw)
            return {"MessageId": "m-1"}

    monkeypatch.setattr(app.boto3, "client", lambda name, *a, **k: FakeSns() if name == "sns" else real(name, *a, **k))
    monkeypatch.setenv("ALERT_TOPIC_ARN", "arn:aws:sns:eu-north-1:123456789012:dev-platform-alerts")
    monkeypatch.setenv("PLATFORM_ADMIN_APP_URL", "https://ops.example.se")
    return sent


@pytest.fixture
def stripe_api(app, monkeypatch):
    calls = []
    cards = {"pm_1": {"id": "pm_1", "type": "card", "card": {"brand": "visa", "last4": "4242"}}}
    state = {"default": None}

    def fake(method, path, params=None, *, account=None, idempotency_key=None):
        calls.append((method, path, params, idempotency_key))
        if path.startswith("/v1/setup_intents/"):
            return {"id": path.split("/")[-1], "payment_method": "pm_1"}
        if path.startswith("/v1/payment_methods/"):
            return cards[path.split("/")[-1]]
        if method == "GET" and path.startswith("/v1/customers/"):
            return {"id": path.split("/")[-1], "invoice_settings": {"default_payment_method": state["default"]}}
        if method == "POST" and path.startswith("/v1/customers/"):
            state["default"] = params["invoice_settings"]["default_payment_method"]
            return {"id": path.split("/")[-1]}
        raise AssertionError(path)

    monkeypatch.setattr(app.stripe, "request", fake)
    return {"calls": calls, "state": state}


def test_past_due_ends_only_when_the_last_failed_invoice_is_paid(app, sns):
    _send(app, _invoice(invoice_id="in_1"))
    _send(app, _invoice(invoice_id="in_2", created=1_800_000_100))
    _send(app, _invoice(invoice_id="in_1", created=1_800_050_000, kind="invoice.payment_failed"))
    _send(app, _invoice(invoice_id="in_2", created=1_800_050_100, kind="invoice.payment_failed"))
    assert _billing(TENANT_A)["pastDueInvoices"] == {"in_1", "in_2"}
    _send(app, _invoice(invoice_id="in_1", status="paid", created=1_800_100_000, paid_at=1_800_100_000,
                        amount_paid=123_750, kind="invoice.paid"))
    b = _billing(TENANT_A)
    assert b["pastDueSince"] and b["pastDueInvoices"] == {"in_2"}                   # in_2 is still unpaid
    _send(app, _invoice(invoice_id="in_2", status="paid", created=1_800_100_100, paid_at=1_800_100_000,
                        amount_paid=123_750, kind="invoice.paid"))
    b = _billing(TENANT_A)
    assert "pastDueSince" not in b and "pastDueInvoices" not in b


def test_a_failed_card_payment_alerts_the_operators_once_per_attempt(app, sns):
    failed = _invoice(status="open", created=1_800_100_000, kind="invoice.payment_failed")
    failed["data"]["object"].update({"attempt_count": 1, "collection_method": "charge_automatically",
                                     "next_payment_attempt": 1_800_300_000})
    assert _send(app, failed) == "applied:invoiced:alerted"
    (msg,) = sns
    assert msg["Subject"].startswith("[dev] Booqy: ") and "card payment failed" in msg["Subject"]
    assert f"https://ops.example.se/tenants/{TENANT_A}?tab=billing" in msg["Message"]
    assert "Invoice: F-0001" in msg["Message"] and "Amount due: 1 237,50 kr incl. VAT" in msg["Message"]
    assert "next retry by Stripe" in msg["Message"]
    assert "\u2013" not in msg["Message"] and "\u2014" not in msg["Message"]
    # redelivered: no second email; the next attempt: one more
    assert _send(app, failed) == "applied"
    failed["data"]["object"]["attempt_count"] = 2
    failed["created"] = 1_800_200_000
    assert _send(app, failed) == "applied:alerted" and len(sns) == 2


def test_an_alert_that_could_not_be_sent_is_retried(app, sns):
    failed = _invoice(status="open", created=1_800_100_000, kind="invoice.payment_failed")
    sns.append("FAIL")
    res = app.handler(_event(failed), None)
    assert res["statusCode"] == 500                                                  # Stripe redelivers
    assert _send(app, failed) == "applied:alerted" and len(sns) == 1


def test_a_card_added_in_checkout_becomes_the_default(app, stripe_api):
    evt = {"id": "evt_cs", "type": "checkout.session.completed", "created": 1_800_000_000, "livemode": False,
           "data": {"object": {"id": "cs_1", "object": "checkout.session", "mode": "setup", "customer": CUS_A,
                               "setup_intent": "seti_1"}}}
    assert _send(app, evt) == "applied:card"
    post = next(c for c in stripe_api["calls"] if c[0] == "POST")
    assert post[1] == f"/v1/customers/{CUS_A}" and post[3] == "default-pm-seti_1"
    b = _billing(TENANT_A)
    assert b["defaultPaymentMethod"] == "pm_1" and b["cardLast4"] == "4242" and b["cardBrand"] == "visa" and b["cardAddedAt"]
    # a payment-mode session is not ours
    evt["data"]["object"]["mode"] = "payment"
    assert _send(app, evt) == "ignored"


def test_customer_updated_keeps_the_card_on_the_profile(app, stripe_api):
    evt = {"id": "evt_cu", "type": "customer.updated", "created": 1_800_000_000, "livemode": False,
           "data": {"object": {"id": CUS_A, "object": "customer",
                               "invoice_settings": {"default_payment_method": "pm_1"}}}}
    assert _send(app, evt) == "applied:card" and _billing(TENANT_A)["cardLast4"] == "4242"
    evt["data"]["object"]["invoice_settings"]["default_payment_method"] = None
    assert _send(app, evt) == "applied:no_card"
    assert "defaultPaymentMethod" not in _billing(TENANT_A)
    # another tenant's customer can never touch this one
    evt["data"]["object"]["id"] = CUS_B
    assert _send(app, evt) == "applied:no_card" and "defaultPaymentMethod" not in _billing(TENANT_B)


def test_an_attached_card_becomes_the_default_only_when_there_is_none(app, stripe_api):
    evt = {"id": "evt_pm", "type": "payment_method.attached", "created": 1_800_000_000, "livemode": False,
           "data": {"object": {"id": "pm_1", "object": "payment_method", "type": "card", "customer": CUS_A}}}
    assert _send(app, evt) == "applied:card"
    assert _send(app, evt) == "ignored:has_default"


def test_a_late_payment_failed_event_never_marks_a_paid_invoice_past_due(app, sns):
    _send(app, _invoice(invoice_id="in_1"))
    _send(app, _invoice(invoice_id="in_1", status="paid", created=1_800_100_000, paid_at=1_800_100_000,
                        amount_paid=123_750, kind="invoice.paid"))
    # the failed attempt before the payment, delivered after it
    late = _invoice(invoice_id="in_1", status="open", created=1_800_050_000, kind="invoice.payment_failed")
    late["data"]["object"]["attempt_count"] = 1
    assert _send(app, late).startswith("stale")
    b = _billing(TENANT_A)
    assert "pastDueSince" not in b and "pastDueInvoices" not in b and sns == []
    # a payment_failed whose invoice already says paid: the same
    paid_obj = _invoice(invoice_id="in_2", status="paid", created=1_800_200_000, paid_at=1_800_190_000,
                        amount_paid=123_750, kind="invoice.payment_failed")
    _send(app, paid_obj)
    assert "pastDueSince" not in _billing(TENANT_A) and sns == []


def test_a_voided_or_uncollectible_invoice_no_longer_keeps_past_due(app, sns):
    _send(app, _invoice(invoice_id="in_1", created=1_800_050_000, kind="invoice.payment_failed"))
    _send(app, _invoice(invoice_id="in_2", created=1_800_050_100, kind="invoice.payment_failed"))
    assert _billing(TENANT_A)["pastDueInvoices"] == {"in_1", "in_2"}
    _send(app, _invoice(invoice_id="in_1", status="void", created=1_800_100_000, voided_at=1_800_100_000,
                        kind="invoice.voided"))
    b = _billing(TENANT_A)
    assert b["pastDueInvoices"] == {"in_2"} and b["pastDueSince"]
    _send(app, _invoice(invoice_id="in_2", status="uncollectible", created=1_800_100_100,
                        kind="invoice.marked_uncollectible"))
    b = _billing(TENANT_A)
    assert "pastDueSince" not in b and "pastDueInvoices" not in b and "lastPaidAt" not in b
    # the ledger still has it as owed (the console's reminders read it)
    row = _fin(finance.tenant_pk(TENANT_A), "INVOICE#2027-01-14#in_2")
    assert row["status"] == "uncollectible"


def test_an_alert_whose_restaurant_name_could_not_be_read_is_not_lost(app, sns, monkeypatch):
    failed = _invoice(status="open", created=1_800_100_000, kind="invoice.payment_failed")
    real = app.billing.tenant_name
    calls = {"n": 0}

    def flaky(tenant_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow"}}, "GetItem")
        return real(tenant_id)
    monkeypatch.setattr(app.billing, "tenant_name", flaky)
    res = app.handler(_event(failed), None)
    assert res["statusCode"] == 500 and sns == []                                    # Stripe redelivers
    assert _send(app, failed).endswith(":alerted") and len(sns) == 1


def test_a_card_invoice_without_a_due_date_is_due_when_finalized(app, sns):
    evt = _invoice(status="open", created=1_800_100_000, kind="invoice.payment_failed")
    evt["data"]["object"].update({"due_date": None, "collection_method": "charge_automatically", "attempt_count": 1})
    _send(app, evt)
    row = _fin(finance.tenant_pk(TENANT_A), "INVOICE#2027-01-14#in_1")
    assert row["dueDate"] == app.billing._iso(1_800_000_000)
    (msg,) = sns
    assert "Due date: " + app.billing._iso(1_800_000_000)[:10] in msg["Message"]


def test_an_older_customer_updated_event_never_overwrites_the_card(app, stripe_api):
    newer = {"id": "evt_cu2", "type": "customer.updated", "created": 1_800_000_200, "livemode": False,
             "data": {"object": {"id": CUS_A, "object": "customer", "invoice_settings": {"default_payment_method": "pm_1"}}}}
    older = {"id": "evt_cu1", "type": "customer.updated", "created": 1_800_000_100, "livemode": False,
             "data": {"object": {"id": CUS_A, "object": "customer", "invoice_settings": {"default_payment_method": None}}}}
    assert _send(app, newer) == "applied:card"
    assert _send(app, older) == "ignored:stale"
    b = _billing(TENANT_A)
    assert b["defaultPaymentMethod"] == "pm_1" and b["cardEventCreated"] == 1_800_000_200

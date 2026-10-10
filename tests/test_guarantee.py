"""Card guarantee end to end: pending booking with a SetupIntent, confirm,
late-cancellation and no-show fees, retries that can't double-charge,
refunds, expiry of abandoned bookings and the Stripe webhook backstop.
Stripe is a recording fake; DynamoDB/Secrets Manager are moto."""

import hashlib
import hmac
import importlib.util
import io
import json
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import boto3
import pytest

from shared import stripe_client
from shared import tenant as shared_tenant
from test_reservations import (DAY, LOC_A, LOCATION_TABLE, OWNER_A, RESERVATION_TABLE, STAFF_A,  # noqa: F401
                               TENANT_A, TENANT_TABLE, body_of, env, event, free_tables_at,
                               guest_cancel, guest_get, load)
from tenant_support import tenant_row

ACCOUNT = "acct_roma"
POLICY = {"enabled": True, "minPartySize": 2, "noShowFeePerPerson": 200,
          "lateCancelFeePerPerson": 100, "cancelCutoffHours": 24}
START = datetime(2026, 9, 20, 16, 0, tzinfo=timezone.utc)  # 18:00 Stockholm on DAY


class FakeStripe:
    def __init__(self):
        self.calls = []
        self.setup_status = "succeeded"
        self.charge = "succeeded"  # succeeded | processing | decline | unavailable | auth

    def __call__(self, method, path, params=None, *, account=None, idempotency_key=None):
        self.calls.append({"method": method, "path": path, "params": params or {},
                           "account": account, "key": idempotency_key})
        if path == "/v1/customers":
            return {"id": "cus_1"}
        if path == "/v1/setup_intents":
            return {"id": "seti_1", "client_secret": "seti_1_secret_abc"}
        if path.startswith("/v1/setup_intents/"):
            return {"id": "seti_1", "status": self.setup_status, "customer": "cus_1",
                    "payment_method": "pm_card", "metadata": {"reservationId": self.rid}}
        if path == "/v1/payment_intents":
            mode = self.charge
            if mode == "unavailable":
                raise stripe_client.StripeUnavailable("down")
            if mode == "decline":
                raise stripe_client.StripeError(402, {"type": "card_error", "code": "card_declined",
                                                      "decline_code": "insufficient_funds",
                                                      "payment_intent": {"id": "pi_declined"}})
            if mode == "auth":
                return {"id": "pi_auth", "status": "requires_action",
                        "last_payment_error": {"code": "authentication_required"}}
            return {"id": f"pi_{len(self.calls)}", "status": mode}
        if path == "/v1/refunds":
            return {"id": "re_1", "amount": params.get("amount")}
        raise AssertionError(f"unexpected Stripe call {method} {path}")

    def keys(self, path):
        return [c["key"] for c in self.calls if c["path"] == path]


@pytest.fixture
def g(env, monkeypatch):
    res = env["res"]
    res.Table(TENANT_TABLE).put_item(Item=tenant_row(
        TENANT_A, stripe={"accountId": ACCOUNT, "chargesEnabled": True}))
    res.Table(LOCATION_TABLE).update_item(
        Key={"PK": f"TENANT#{TENANT_A}", "SK": f"LOCATION#{LOC_A}"},
        UpdateExpression="SET guarantee = :g",
        ExpressionAttributeValues={":g": {k: (Decimal(v) if isinstance(v, int) and not isinstance(v, bool) else v)
                                          for k, v in POLICY.items()}})
    shared_tenant.reset_caches()
    fake = FakeStripe()
    monkeypatch.setattr(stripe_client, "request", fake)
    env["stripe"] = fake
    return env


def book(env, party=3, tables=("table-b",)):
    payload = {"date": DAY, "startTime": "18:00", "tableIds": list(tables), "partySize": party,
               "name": "Anna Svensson", "email": "anna@example.se", "phone": "0701234567",
               "acceptTerms": True}
    response = env["m"]["create"].handler(
        event("POST", "/locations/{locationId}/reservations", body=payload), None)
    body = body_of(response)
    if response["statusCode"] == 201:
        env["stripe"].rid = body["reservationId"]
    return response, body


def confirm(env, rid, token):
    return env["m"]["create"].handler(event(
        "POST", "/locations/{locationId}/reservations/{reservationId}/confirm",
        reservation=rid, headers={"x-manage-token": token}), None)


def stored(env, rid):
    return env["res"].Table(RESERVATION_TABLE).get_item(
        Key={"PK": f"LOCATION#{LOC_A}", "SK": f"RESERVATION#{DAY}#{rid}"})["Item"]


def markers(env):
    from boto3.dynamodb.conditions import Key
    return env["res"].Table(RESERVATION_TABLE).query(
        KeyConditionExpression=Key("PK").eq(f"LOCATION#{LOC_A}") & Key("SK").begins_with("PENDING#"))["Items"]


def confirmed_booking(env, party=3):
    _, body = book(env, party)
    assert confirm(env, body["reservationId"], body["manageToken"])["statusCode"] == 200
    return body["reservationId"], body["manageToken"]


def staff_status(env, rid, status, who=OWNER_A, **extra):
    return env["m"]["staff"].handler(event(
        "POST", "/locations/{locationId}/reservations/{reservationId}/status", reservation=rid,
        body={"status": status, **extra}, staff=who), None)


def staff_payment(env, rid, who=OWNER_A, **body):
    return env["m"]["staff"].handler(event(
        "POST", "/locations/{locationId}/reservations/{reservationId}/payment", reservation=rid,
        body=body, staff=who), None)


# --- booking with a card ----------------------------------------------------------------

def test_guarantee_booking_is_pending_with_a_setup_intent_on_the_restaurants_account(g):
    response, body = book(g)

    assert response["statusCode"] == 201
    assert body["status"] == "pending"
    assert body["setup"] == {"clientSecret": "seti_1_secret_abc", "stripeAccount": ACCOUNT,
                             "expiresAt": "2026-09-08T12:20:00Z"}
    assert body["guarantee"] == {"cardOnFile": False, "noShowFee": 60000, "lateCancelFee": 30000,
                                 "cancelCutoffHours": 24, "currency": "sek", "cancellationFee": 0}
    item = stored(g, body["reservationId"])
    assert "notice" not in item  # the confirmation waits for the card
    assert item["stripeAccountId"] == ACCOUNT and item["setupIntentId"] == "seti_1"
    assert "table-b" not in free_tables_at(g, "18:00")  # held while the guest enters the card
    assert len(markers(g)) == 1
    setup = [c for c in g["stripe"].calls if c["path"] == "/v1/setup_intents"][0]
    assert setup["account"] == ACCOUNT and setup["params"]["usage"] == "off_session"
    assert setup["params"]["payment_method_types"] == ["card"]
    assert setup["key"] == f"setup-{body['reservationId']}"


def test_confirm_turns_it_reserved_once_and_sends_the_confirmation(g):
    _, body = book(g)
    rid, token = body["reservationId"], body["manageToken"]

    first = confirm(g, rid, token)
    second = confirm(g, rid, token)

    assert first["statusCode"] == 200 and body_of(first)["status"] == "reserved"
    assert body_of(first)["guarantee"]["cardOnFile"] is True
    assert second["statusCode"] == 200
    item = stored(g, rid)
    assert item["stripePaymentMethodId"] == "pm_card" and item["notice"]["type"] == "confirmed"
    assert "pendingExpiresAt" not in item and markers(g) == []


@pytest.mark.parametrize("status", ["requires_payment_method", "requires_action", "processing"])
def test_confirm_before_the_card_is_done_is_409(g, status):
    _, body = book(g)
    g["stripe"].setup_status = status
    response = confirm(g, body["reservationId"], body["manageToken"])
    assert response["statusCode"] == 409 and body_of(response) == {"error": "card_not_confirmed"}
    assert stored(g, body["reservationId"])["status"] == "pending"


def test_confirm_needs_the_manage_token(g):
    _, body = book(g)
    assert confirm(g, body["reservationId"], "wrong")["statusCode"] == 404


def test_small_party_or_stripe_not_ready_books_without_a_card(g):
    response, body = book(g, party=1, tables=("table-a",))
    assert body["status"] == "reserved" and "setup" not in body and "guarantee" not in body

    g["res"].Table(TENANT_TABLE).put_item(Item=tenant_row(
        TENANT_A, stripe={"accountId": ACCOUNT, "chargesEnabled": False}))
    shared_tenant.reset_caches()
    _, body = book(g, party=4, tables=("table-c",))
    assert body["status"] == "reserved" and "setup" not in body
    assert g["stripe"].keys("/v1/setup_intents") == []


def test_stripe_down_at_booking_writes_nothing(g, monkeypatch):
    def down(*a, **k):
        raise stripe_client.StripeUnavailable("down")
    monkeypatch.setattr(stripe_client, "request", down)
    response, body = book(g)
    assert response["statusCode"] == 503 and body == {"error": "payment_unavailable"}
    assert "table-b" in free_tables_at(g, "18:00") and markers(g) == []


def test_abandoned_booking_expires_and_frees_the_tables(g):
    _, body = book(g)
    rid = body["reservationId"]
    reminders = load("reservation-reminders", "res_reminders_g")
    g["clock"].value = g["clock"].value + timedelta(minutes=21)

    result = reminders.handler({}, None)

    assert result["expired"] == 1
    assert stored(g, rid)["status"] == "expired" and markers(g) == []
    assert "table-b" in free_tables_at(g, "18:00")
    late = confirm(g, rid, body["manageToken"])
    assert late["statusCode"] == 409 and body_of(late) == {"error": "expired"}


def test_guest_can_drop_a_pending_booking_for_free(g):
    _, body = book(g)
    response = guest_cancel(g, body["reservationId"], body["manageToken"])
    assert response["statusCode"] == 200 and body_of(response)["status"] == "cancelled_no_charge"
    assert markers(g) == [] and g["stripe"].keys("/v1/payment_intents") == []


# --- late cancellation -------------------------------------------------------------------

def cancel(env, rid, token, **body):
    return env["m"]["cancel"].handler(event(
        "POST", "/locations/{locationId}/reservations/{reservationId}/cancel", reservation=rid,
        headers={"x-manage-token": token}, body=body or None), None)


def test_cancelling_before_the_cutoff_is_free(g):
    rid, token = confirmed_booking(g)
    g["clock"].value = START - timedelta(hours=25)
    response = cancel(g, rid, token)
    assert body_of(response)["status"] == "cancelled_no_charge"
    assert g["stripe"].keys("/v1/payment_intents") == []


def test_late_cancel_asks_first_then_charges_the_accepted_fee(g):
    rid, token = confirmed_booking(g)
    g["clock"].value = START - timedelta(hours=5)
    assert body_of(guest_get(g, rid, token))["guarantee"]["cancellationFee"] == 30000

    ask = cancel(g, rid, token)
    assert ask["statusCode"] == 409 and body_of(ask) == {"error": "fee_applies", "fee": 30000, "currency": "sek"}
    assert stored(g, rid)["status"] == "reserved"

    done = cancel(g, rid, token, acceptFee=True)

    assert done["statusCode"] == 200 and body_of(done)["status"] == "cancelled_charged"
    assert body_of(done)["fee"] == {"kind": "late_cancel", "status": "succeeded", "amount": 30000,
                                    "refundedAmount": 0}
    charge = [c for c in g["stripe"].calls if c["path"] == "/v1/payment_intents"][0]
    assert charge["account"] == ACCOUNT and charge["key"] == f"late_cancel-{rid}-1"
    assert charge["params"]["off_session"] is True and charge["params"]["amount"] == 30000
    assert stored(g, rid)["notice"]["type"] == "fee_charged"
    assert "table-b" in free_tables_at(g, "18:00")


def test_declined_late_fee_is_recorded_and_the_restaurant_told(g):
    rid, token = confirmed_booking(g)
    g["clock"].value = START - timedelta(hours=2)
    g["stripe"].charge = "decline"
    body = body_of(cancel(g, rid, token, acceptFee=True))
    assert body["status"] == "cancelled_charge_failed" and body["fee"]["status"] == "failed"
    item = stored(g, rid)
    assert item["payment"]["error"] == "insufficient_funds" and item["notice"]["type"] == "fee_failed"


def test_stripe_outage_leaves_the_fee_pending_and_a_retry_reuses_the_key(g):
    rid, token = confirmed_booking(g)
    g["clock"].value = START - timedelta(hours=2)
    g["stripe"].charge = "unavailable"
    body = body_of(cancel(g, rid, token, acceptFee=True))
    assert body["status"] == "cancelled_no_charge" and body["fee"]["status"] == "pending"

    g["stripe"].charge = "succeeded"
    retried = staff_payment(g, rid, action="charge")

    assert retried["statusCode"] == 200 and body_of(retried)["status"] == "cancelled_charged"
    assert g["stripe"].keys("/v1/payment_intents") == [f"late_cancel-{rid}-1", f"late_cancel-{rid}-1"]


# --- no-show --------------------------------------------------------------------------------

def test_no_show_charges_the_fee_from_booking_time(g):
    rid, _ = confirmed_booking(g)
    # The owner raises the fee afterwards - the guest's accepted fee applies.
    g["res"].Table(LOCATION_TABLE).update_item(
        Key={"PK": f"TENANT#{TENANT_A}", "SK": f"LOCATION#{LOC_A}"},
        UpdateExpression="SET guarantee.noShowFeePerPerson = :f", ExpressionAttributeValues={":f": Decimal(999)})
    g["clock"].value = START + timedelta(minutes=30)

    response = staff_status(g, rid, "no_show")

    assert response["statusCode"] == 200
    body = body_of(response)
    assert body["status"] == "no_show_charged" and body["payment"]["amount"] == 60000
    assert stored(g, rid)["notice"]["type"] == "fee_charged"


def test_waived_no_show_charges_nothing(g):
    rid, _ = confirmed_booking(g)
    g["clock"].value = START + timedelta(minutes=30)
    body = body_of(staff_status(g, rid, "no_show", chargeFee=False))
    assert body["status"] == "no_show" and g["stripe"].keys("/v1/payment_intents") == []
    assert body["history"][-1]["reason"] == "fee waived"


def test_failed_no_show_fee_retry_is_a_new_attempt(g):
    rid, _ = confirmed_booking(g)
    g["clock"].value = START + timedelta(minutes=30)
    g["stripe"].charge = "auth"
    assert body_of(staff_status(g, rid, "no_show"))["status"] == "no_show_charge_failed"

    g["stripe"].charge = "succeeded"
    body = body_of(staff_payment(g, rid, who=STAFF_A, action="charge"))

    assert body["status"] == "no_show_charged"
    assert g["stripe"].keys("/v1/payment_intents") == [f"no_show-{rid}-1", f"no_show-{rid}-2"]
    assert staff_payment(g, rid, action="charge")["statusCode"] == 409  # already charged


def test_refunds_are_owner_only_and_track_the_amount(g):
    rid, _ = confirmed_booking(g)
    g["clock"].value = START + timedelta(minutes=30)
    staff_status(g, rid, "no_show")

    assert staff_payment(g, rid, who=STAFF_A, action="refund")["statusCode"] == 403
    partial = body_of(staff_payment(g, rid, action="refund", amount=20000))
    assert partial["payment"]["refundedAmount"] == 20000 and partial["payment"]["status"] == "succeeded"
    full = body_of(staff_payment(g, rid, action="refund"))
    assert full["payment"]["refundedAmount"] == 60000 and full["payment"]["status"] == "refunded"
    assert staff_payment(g, rid, action="refund")["statusCode"] == 409
    assert g["stripe"].keys("/v1/refunds") == [f"refund-{rid}-1", f"refund-{rid}-2"]


def test_booking_without_a_card_has_nothing_to_charge(g):
    _, body = book(g, party=1, tables=("table-a",))
    g["clock"].value = START + timedelta(minutes=30)
    response = staff_status(g, body["reservationId"], "no_show")
    assert body_of(response)["status"] == "no_show"
    assert staff_payment(g, body["reservationId"], action="charge")["statusCode"] == 409


# --- webhook backstop ------------------------------------------------------------------------

@pytest.fixture
def hook(g, monkeypatch):
    secret = boto3.client("secretsmanager", region_name="eu-north-1").create_secret(
        Name="whsec", SecretString="whsec_test")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET_ARN", secret["ARN"])
    module = load("stripe-webhook", "res_webhook")
    module._secret.update(value=None, at=0.0)

    def send(evt, *, secret_value="whsec_test", ts=None):
        payload = json.dumps(evt).encode()
        ts = ts or int(time.time())
        sig = hmac.new(secret_value.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
        return module.handler({"requestContext": {"http": {"method": "POST"}},
                               "headers": {"Stripe-Signature": f"t={ts},v1={sig}"},
                               "body": payload.decode()}, None)
    return send


def stripe_event(kind, obj, account=ACCOUNT, livemode=False):
    return {"id": "evt_1", "type": kind, "account": account, "livemode": livemode,
            "data": {"object": obj}}


def test_webhook_rejects_bad_signatures(hook):
    assert hook(stripe_event("setup_intent.succeeded", {}), secret_value="whsec_other")["statusCode"] == 400
    assert hook(stripe_event("setup_intent.succeeded", {}), ts=int(time.time()) - 3600)["statusCode"] == 400


def test_webhook_confirms_a_booking_the_guest_left(g, hook):
    _, body = book(g)
    rid = body["reservationId"]
    obj = {"id": "seti_1", "status": "succeeded", "customer": "cus_1", "payment_method": "pm_card",
           "metadata": {"reservationId": rid, "locationId": LOC_A}}

    assert body_of(hook(stripe_event("setup_intent.succeeded", obj, account="acct_other")))["result"] == "ignored"
    assert body_of(hook(stripe_event("setup_intent.succeeded", obj, livemode=True))) == {"ignored": "livemode"}
    assert stored(g, rid)["status"] == "pending"

    response = hook(stripe_event("setup_intent.succeeded", obj))

    assert body_of(response)["result"] == "confirmed" and stored(g, rid)["status"] == "reserved"
    assert body_of(hook(stripe_event("setup_intent.succeeded", obj)))["result"] == "ignored"


def test_webhook_settles_a_fee_whose_result_was_unknown(g, hook):
    rid, _ = confirmed_booking(g)
    g["clock"].value = START + timedelta(minutes=30)
    g["stripe"].charge = "processing"
    assert body_of(staff_status(g, rid, "no_show"))["payment"]["status"] == "processing"
    pi = stored(g, rid)["payment"]["paymentIntentId"]

    obj = {"id": pi, "metadata": {"reservationId": rid, "locationId": LOC_A, "feeKind": "no_show"}}
    assert body_of(hook(stripe_event("payment_intent.succeeded", obj)))["result"] == "recorded"
    assert stored(g, rid)["status"] == "no_show_charged"
    assert body_of(hook(stripe_event("payment_intent.payment_failed", obj)))["result"] == "already_settled"

    refund = {"payment_intent": pi, "amount_refunded": 60000,
              "metadata": {"reservationId": rid, "locationId": LOC_A}}
    assert body_of(hook(stripe_event("charge.refunded", refund)))["result"] == "recorded"
    assert stored(g, rid)["payment"]["status"] == "refunded"


# --- policy validation ----------------------------------------------------------------------

@pytest.mark.parametrize("raw, error", [
    ("yes", "guarantee must be an object"),
    ({"enabled": True}, "needs a no-show or late-cancellation fee"),
    ({"enabled": True, "noShowFeePerPerson": -1}, "between 0 and 5000"),
    ({"enabled": True, "noShowFeePerPerson": 100, "cancelCutoffHours": 500}, "between 0 and 168"),
    ({"enabled": True, "noShowFeePerPerson": 100, "minPartySize": 0}, "between 1 and 50"),
    ({"enabled": "on", "noShowFeePerPerson": 100}, "true or false"),
    ({"enabled": True, "noShowFeePerPerson": 100, "extra": 1}, "unsupported guarantee fields: extra"),
])
def test_policy_validation(raw, error):
    from shared import guarantee
    with pytest.raises(ValueError, match=error):
        guarantee.validate_policy(raw)


def test_policy_defaults_and_disabled_policy():
    from shared import guarantee
    assert guarantee.validate_policy({"enabled": True, "noShowFeePerPerson": 150}) == {
        "enabled": True, "minPartySize": 1, "noShowFeePerPerson": 150, "lateCancelFeePerPerson": 0,
        "cancelCutoffHours": 24}
    assert guarantee.policy_of({"guarantee": {"enabled": False}}) is None
    assert guarantee.policy_of({"guarantee": "broken"}) is None


# --- the Stripe client itself -----------------------------------------------------------------

def test_form_encoding_matches_stripe():
    pairs = stripe_client._flatten({"amount": 100, "off_session": True, "payment_method_types": ["card"],
                                    "metadata": {"a": "1"}, "skip": None})
    assert pairs == [("amount", "100"), ("off_session", "true"), ("payment_method_types[]", "card"),
                     ("metadata[a]", "1")]


def test_client_maps_errors(monkeypatch):
    monkeypatch.setattr(stripe_client, "_api_key", lambda: "sk_test_x")

    def raise_http(code, body):
        def opener(req, timeout):
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(json.dumps(body).encode()))
        return opener

    monkeypatch.setattr(stripe_client.urllib.request, "urlopen",
                        raise_http(402, {"error": {"type": "card_error", "code": "card_declined"}}))
    with pytest.raises(stripe_client.StripeError) as exc:
        stripe_client.request("POST", "/v1/payment_intents", {"amount": 1}, account="acct_1", idempotency_key="k")
    assert exc.value.code == "card_declined"

    monkeypatch.setattr(stripe_client.urllib.request, "urlopen", raise_http(503, {}))
    with pytest.raises(stripe_client.StripeUnavailable):
        stripe_client.request("GET", "/v1/setup_intents/seti_1")

    seen = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def capture(req, timeout):
        seen.update(headers=dict(req.header_items()), data=req.data)
        return Response(b'{"id": "pi_1"}')

    monkeypatch.setenv("STRIPE_API_VERSION", "2026-07-29.dahlia")
    monkeypatch.setattr(stripe_client.urllib.request, "urlopen", capture)
    assert stripe_client.request("POST", "/v1/refunds", {"amount": 5}, account="acct_1",
                                 idempotency_key="refund-1")["id"] == "pi_1"
    assert seen["headers"]["Stripe-account"] == "acct_1" and seen["headers"]["Idempotency-key"] == "refund-1"
    assert seen["headers"]["Stripe-version"] == "2026-07-29.dahlia" and seen["data"] == b"amount=5"

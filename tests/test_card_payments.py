"""In-person card payments ("Ta betalt", functions/terminal-payments,
shared/card_payments.py): amounts in öre, one PaymentIntent per checkout,
status transitions, the Stripe webhook (duplicates, out of order, foreign
accounts), readers of the restaurant's own location only, refunds, paying an
unpaid order (kitchen ticket and receipt follow from it), receipts,
Statistik and retention. DynamoDB/Secrets Manager are moto; Stripe is a
recording fake that models PaymentIntents and readers."""

import hashlib
import hmac
import importlib.util
import json
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from shared import card_messages, insights, retention, stripe_client
from shared import card_payments as cp
from shared import dynamo as shared_dynamo
from shared import orders as o
from shared import tenant as shared_tenant
from tenant_support import (LOC_A, LOC_A2, LOC_B, REGION, STAFF_A_SUB, TENANT_A, TENANT_B, TENANT_TABLE,
                            create_tables, tenant_row, with_tenant_claims)

ROOT = Path(__file__).parents[1] / "functions"
LOCATION_TABLE = "test-location"
ORDER_TABLE = "test-order"
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)  # 14:00 in Stockholm
TODAY = "2026-09-08"
ACCOUNT = "acct_roma"
WHSEC = "whsec_terminal"
CHECKOUT = "a" * 32
CHECKOUT_2 = "b" * 32


def _hex(n):
    return f"{n:032x}"


class FakeStripe:
    """PaymentIntents and readers per connected account, with Stripe's
    idempotency (same key, same answer)."""

    def __init__(self):
        self.calls = []
        self.intents = {}
        self.charges = {}
        self.keys = {}
        self.readers = {
            "tmr_a1": {"id": "tmr_a1", "label": "Kassan", "location": "tml_a1", "status": "online",
                       "device_type": "simulated_wisepos_e", "action": None, "_account": ACCOUNT},
            "tmr_a2": {"id": "tmr_a2", "label": "Baren", "location": "tml_a2", "status": "online",
                       "device_type": "stripe_s700", "action": None, "_account": ACCOUNT},
            "tmr_b1": {"id": "tmr_b1", "label": "Other", "location": "tml_b1", "status": "online",
                       "device_type": "stripe_s700", "action": None, "_account": "acct_other"},
        }
        self.process_error = None
        self.session = {"id": "cs_1", "status": "open", "payment_status": "unpaid"}

    def __call__(self, method, path, params=None, *, account=None, idempotency_key=None):
        self.calls.append({"method": method, "path": path, "params": params or {}, "account": account,
                           "key": idempotency_key})
        if idempotency_key and idempotency_key in self.keys:
            return json.loads(json.dumps(self.keys[idempotency_key]))
        result = self._handle(method, path, params or {}, account)
        if idempotency_key:
            self.keys[idempotency_key] = result
        return json.loads(json.dumps(result))

    def _reader(self, reader_id, account):
        reader = self.readers.get(reader_id)
        if not reader or reader["_account"] != account:
            raise stripe_client.StripeError(404, {"code": "resource_missing", "message": "No such reader"})
        return reader

    def _handle(self, method, path, params, account):
        parts = path.strip("/").split("/")
        if path == "/v1/terminal/readers" and method == "GET":
            return {"data": [self._public(r) for r in self.readers.values()
                             if r["_account"] == account and r["location"] == params.get("location")]}
        if parts[:3] == ["v1", "terminal", "readers"] and len(parts) == 4:
            return self._public(self._reader(parts[3], account))
        if parts[:3] == ["v1", "terminal", "readers"] and parts[4] == "process_payment_intent":
            reader = self._reader(parts[3], account)
            if self.process_error:
                raise self.process_error
            reader["action"] = {"type": "process_payment_intent", "status": "in_progress",
                                "process_payment_intent": {"payment_intent": params["payment_intent"]}}
            return self._public(reader)
        if parts[:3] == ["v1", "terminal", "readers"] and parts[4] == "cancel_action":
            reader = self._reader(parts[3], account)
            if not reader["action"]:
                raise stripe_client.StripeError(400, {"code": "terminal_reader_busy"})
            reader["action"] = None
            return self._public(reader)
        if path.startswith("/v1/test_helpers/terminal/readers/") and parts[-1] == "present_payment_method":
            reader = self._reader(parts[4], account)
            intent = self.intents[reader["action"]["process_payment_intent"]["payment_intent"]]
            if params["card_present"]["number"].endswith("0002"):
                self.decline(intent["id"])
            else:
                self.pay(intent["id"])
            return self._public(reader)
        if path == "/v1/payment_intents" and method == "POST":
            pi_id = f"pi_{len(self.intents) + 1}"
            self.intents[pi_id] = {"id": pi_id, "object": "payment_intent", "amount": params["amount"],
                                   "currency": params["currency"], "status": "requires_payment_method",
                                   "metadata": dict(params["metadata"]), "latest_charge": None,
                                   "last_payment_error": None, "_account": account,
                                   "payment_method_types": params["payment_method_types"],
                                   "capture_method": params["capture_method"]}
            return self._intent(pi_id, False)
        if parts[:2] == ["v1", "payment_intents"] and len(parts) == 3:
            if self.intents[parts[2]]["_account"] != account:
                raise stripe_client.StripeError(404, {"code": "resource_missing"})
            return self._intent(parts[2], "latest_charge" in (params.get("expand") or []))
        if path == "/v1/refunds":
            intent = self.intents[params["payment_intent"]]
            charge = self.charges[intent["latest_charge"]]
            charge["amount_refunded"] = intent["amount"]
            return {"id": "re_1", "amount": intent["amount"], "status": "succeeded"}
        if parts[:3] == ["v1", "checkout", "sessions"]:
            if path.endswith("/expire"):
                if self.session["status"] != "open":
                    raise stripe_client.StripeError(400, {"code": "checkout_session_not_open"})
                self.session["status"] = "expired"
            return dict(self.session)
        raise AssertionError(path)

    @staticmethod
    def _public(reader):
        return {k: v for k, v in reader.items() if not k.startswith("_")}

    def _intent(self, pi_id, expand):
        intent = {k: v for k, v in self.intents[pi_id].items() if not k.startswith("_")}
        if expand and intent["latest_charge"]:
            intent["latest_charge"] = self.charges[intent["latest_charge"]]
        return intent

    def pay(self, pi_id):
        intent = self.intents[pi_id]
        charge_id = f"ch_{pi_id}"
        self.charges[charge_id] = {
            "id": charge_id, "payment_intent": pi_id, "amount": intent["amount"], "amount_refunded": 0,
            "metadata": intent["metadata"],
            "payment_method_details": {"card_present": {
                "brand": "visa", "last4": "4242", "read_method": "contactless_emv",
                "receipt": {"application_preferred_name": "Visa Credit", "dedicated_file_name": "A0000000031010",
                            "authorization_code": "123456", "authorization_response_code": "3030",
                            "account_type": "credit", "cardholder_verification_method": "online_pin"}}}}
        intent.update(status="succeeded", latest_charge=charge_id, last_payment_error=None)
        for reader in self.readers.values():
            if (reader["action"] or {}).get("process_payment_intent", {}).get("payment_intent") == pi_id:
                reader["action"]["status"] = "succeeded"

    def decline(self, pi_id):
        intent = self.intents[pi_id]
        intent.update(status="requires_payment_method",
                      last_payment_error={"code": "card_declined", "decline_code": "insufficient_funds",
                                          "message": "Your card has insufficient funds."})
        for reader in self.readers.values():
            if (reader["action"] or {}).get("process_payment_intent", {}).get("payment_intent") == pi_id:
                reader["action"].update(status="failed", failure_code="card_declined",
                                        failure_message="Your card was declined.")

    def count(self, path):
        return sum(1 for c in self.calls if c["path"] == path)


def location_item(tenant_id, location_id, terminal=None, **extra):
    item = {"PK": f"TENANT#{tenant_id}", "SK": f"LOCATION#{location_id}", "tenantId": tenant_id,
            "locationId": location_id, "name": "Södermalm", "address": "Götgatan 1, Stockholm",
            "timezone": "Europe/Stockholm", "createdBy": "c", "createdAt": "2026-08-20T10:00:00Z"}
    if terminal:
        item["terminal"] = terminal
    item.update(extra)
    return item


def load_fn(name, module_name):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / name / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def env(monkeypatch):
    for key, value in {
        "ENVIRONMENT": "dev", "AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t", "AWS_SESSION_TOKEN": "t",
        "AWS_DEFAULT_REGION": REGION, "ORDER_TABLE_NAME": ORDER_TABLE, "USER_TABLE_NAME": "test-user",
    }.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        sm = boto3.client("secretsmanager", region_name=REGION)
        monkeypatch.setenv("TERMINAL_STRIPE_WEBHOOK_SECRET_ARN",
                           sm.create_secret(Name="whsec-terminal", SecretString=WHSEC)["ARN"])
        res = boto3.resource("dynamodb", region_name=REGION)
        res.create_table(TableName=ORDER_TABLE,
                         KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                                    {"AttributeName": "SK", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                               {"AttributeName": "SK", "AttributeType": "S"}],
                         BillingMode="PAY_PER_REQUEST")
        create_tables(monkeypatch, location_table=LOCATION_TABLE)
        res.Table(TENANT_TABLE).put_item(Item=tenant_row(
            TENANT_A, name="Roma", stripe={"accountId": ACCOUNT}, legalName="Roma AB", orgNumber="556000-0000"))
        res.Table(TENANT_TABLE).put_item(Item=tenant_row(TENANT_B, stripe={"accountId": "acct_other"}))
        locs = res.Table(LOCATION_TABLE)
        locs.put_item(Item=location_item(TENANT_A, LOC_A, {"accountId": ACCOUNT, "locationId": "tml_a1"}))
        locs.put_item(Item=location_item(TENANT_A, LOC_A2, {"accountId": ACCOUNT, "locationId": "tml_a2"}))
        locs.put_item(Item=location_item(TENANT_B, LOC_B, {"accountId": "acct_other", "locationId": "tml_b1"}))
        res.Table("test-user").put_item(Item={"PK": f"USER#{STAFF_A_SUB}", "SK": "PROFILE",
                                              "tenantId": TENANT_A, "locationId": LOC_A, "status": "active"})
        fake = FakeStripe()
        monkeypatch.setattr(stripe_client, "request", fake)
        clock = {"now": NOW}
        monkeypatch.setattr(o, "now_utc", lambda: clock["now"])
        fn = load_fn("terminal-payments", "term_fn")
        fn._secret.update(value=None, at=0.0)
        yield {"fn": fn, "res": res, "stripe": fake, "clock": clock}
        shared_dynamo._resource = None
        shared_dynamo._client = None
        shared_tenant.reset_caches()


# --- helpers ---------------------------------------------------------------------------

def body_of(response):
    return json.loads(response["body"])


def api(env, method, suffix="", *, body=None, query=None, role="owner_user", tenant_id=TENANT_A, location_id=LOC_A):
    if suffix:
        event = {"routeKey": f"{method} /locations/{{locationId}}/card-payments/{{proxy+}}",
                 "pathParameters": {"locationId": location_id, "proxy": suffix}}
    else:
        event = {"routeKey": f"{method} /locations/{{locationId}}/card-payments",
                 "pathParameters": {"locationId": location_id}}
    if body is not None:
        event["body"] = json.dumps(body)
    if query is not None:
        event["queryStringParameters"] = query
    return env["fn"].handler(with_tenant_claims(event, tenant_id, role), None)


def charge(env, amount=12550, checkout=CHECKOUT, reader="tmr_a1", **extra):
    extra.setdefault("vatRate", 12)
    return api(env, "POST", body={"checkoutId": checkout, "readerId": reader, "amount": amount, **extra})


def stripe_event(kind, obj, *, account=ACCOUNT, livemode=False, created=None, event_id="evt_1"):
    return {"id": event_id, "type": kind, "account": account, "livemode": livemode,
            "created": created if created is not None else int(NOW.timestamp()) + 5, "data": {"object": obj}}


def webhook(env, event, *, secret=WHSEC):
    payload = json.dumps(event)
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    return env["fn"].handler({"routeKey": "$default", "body": payload,
                              "headers": {"Stripe-Signature": f"t={ts},v1={sig}"},
                              "requestContext": {"http": {"method": "POST"}}}, None)


def intent_of(env, pi_id="pi_1"):
    return env["stripe"]._intent(pi_id, False)


def put_order(env, *, status=o.PENDING_PAYMENT, total=28550, account=ACCOUNT, order_id=None):
    order_id = order_id or _hex(7)
    item = {**o.order_key(LOC_A, TODAY, order_id), "orderId": order_id, "tenantId": TENANT_A, "locationId": LOC_A,
            "date": TODAY, "timezone": "Europe/Stockholm", "mode": "table", "tableLabel": "5", "status": status,
            "paymentStatus": "processing", "totalAmount": total, "currency": "sek",
            "lines": [{"menuItemId": "pizza", "name": "Margherita", "unitPrice": 12500, "quantity": 2,
                       "total": 25000, "vatRate": 12},
                      {"menuItemId": "cola", "name": "Cola", "unitPrice": 3550, "quantity": 1, "total": 3550,
                       "vatRate": 12}],
            "vat": o.vat_breakdown([{"vatRate": 12, "total": 28550}]), "stripeAccountId": account,
            "checkoutSessionId": "cs_1", "createdAt": o.iso(NOW - timedelta(minutes=3)), "prepMinutes": 20,
            "ttl": int(NOW.timestamp()) + 600, "history": []}
    table = env["res"].Table(ORDER_TABLE)
    table.put_item(Item=item)
    table.put_item(Item={**o.pointer_key(LOC_A, order_id), "date": TODAY})
    return order_id


# --- amounts in öre ----------------------------------------------------------------------

@pytest.mark.parametrize("value", [300, 12550, 5_000_000, Decimal("12550")])
def test_amounts_are_whole_ore_within_limits(value):
    assert cp.validate_amount(value) == int(value)


@pytest.mark.parametrize("value", [299, 0, -100, 5_000_001, Decimal("125.5"), True, "12550", None, 125.5])
def test_amounts_outside_limits_or_not_whole_ore_are_refused(value):
    with pytest.raises(ValueError):
        cp.validate_amount(value)


def test_payment_intent_is_card_present_automatic_capture_in_ore_on_the_restaurants_account(env):
    response = charge(env, amount=12550, note="Bord 4, dricks")
    assert response["statusCode"] == 201
    payment = body_of(response)
    assert payment["status"] == "processing" and payment["amount"] == 12550
    create = next(c for c in env["stripe"].calls if c["path"] == "/v1/payment_intents")
    assert create["params"]["amount"] == 12550 and create["params"]["currency"] == "sek"
    assert create["params"]["payment_method_types"] == ["card_present"]
    assert create["params"]["capture_method"] == "automatic"
    assert create["account"] == ACCOUNT
    assert create["params"]["metadata"]["paymentId"] == CHECKOUT
    assert create["params"]["metadata"]["locationId"] == LOC_A
    process = next(c for c in env["stripe"].calls if c["path"].endswith("/process_payment_intent"))
    assert process["path"] == "/v1/terminal/readers/tmr_a1/process_payment_intent"
    assert process["params"] == {"payment_intent": "pi_1"} and process["account"] == ACCOUNT


def test_a_free_amount_needs_a_vat_rate_and_keeps_its_vat_row(env):
    for bad in (None, 6, 0, "12", True):
        body = {"checkoutId": CHECKOUT, "readerId": "tmr_a1", "amount": 11200}
        if bad is not None:
            body["vatRate"] = bad
        assert api(env, "POST", body=body)["statusCode"] == 400
    assert env["stripe"].count("/v1/payment_intents") == 0
    payment = body_of(charge(env, amount=11200, vatRate=25))
    assert payment["vatRate"] == 25
    item = cp.load(LOC_A, CHECKOUT)
    assert item["vat"] == [{"rate": 25, "gross": 11200, "net": 8960, "vat": 2240}]


def test_a_decimal_kronor_amount_from_the_browser_is_refused(env):
    response = api(env, "POST", body={"checkoutId": CHECKOUT, "readerId": "tmr_a1", "amount": 125.5, "vatRate": 12})
    assert response["statusCode"] == 400
    assert env["stripe"].count("/v1/payment_intents") == 0


# --- idempotency -------------------------------------------------------------------------

def test_the_same_checkout_twice_makes_one_payment_intent_and_sends_it_once(env):
    first = charge(env)
    second = charge(env)
    assert first["statusCode"] == 201 and second["statusCode"] == 200
    assert body_of(first)["paymentId"] == body_of(second)["paymentId"]
    assert env["stripe"].count("/v1/payment_intents") == 1
    assert env["stripe"].count("/v1/terminal/readers/tmr_a1/process_payment_intent") == 1


def test_stripe_calls_carry_idempotency_keys_per_checkout_and_try(env):
    charge(env)
    webhook(env, stripe_event("payment_intent.payment_failed", _declined(env)))
    api(env, "POST", f"{CHECKOUT}/retry")
    keys = [c["key"] for c in env["stripe"].calls if c["key"]]
    assert f"card-pi-{CHECKOUT}" in keys
    assert f"card-process-{CHECKOUT}-1" in keys and f"card-process-{CHECKOUT}-2" in keys
    assert env["stripe"].count("/v1/payment_intents") == 1  # the retry reuses the PaymentIntent


def test_a_retry_after_the_money_was_taken_settles_instead_of_charging_again(env):
    charge(env)
    api(env, "POST", f"{CHECKOUT}/cancel")
    env["stripe"].pay("pi_1")  # the card went through as the cancel landed
    response = api(env, "POST", f"{CHECKOUT}/retry")
    assert body_of(response)["status"] == "succeeded"
    assert env["stripe"].count("/v1/terminal/readers/tmr_a1/process_payment_intent") == 1


def test_an_order_gets_one_card_payment_even_from_two_tills(env):
    order_id = put_order(env)
    first = body_of(api(env, "POST", body={"checkoutId": CHECKOUT, "readerId": "tmr_a1", "orderId": order_id}))
    second = body_of(api(env, "POST", body={"checkoutId": CHECKOUT_2, "readerId": "tmr_a1", "orderId": order_id}))
    assert first["paymentId"] == second["paymentId"] == CHECKOUT
    assert env["stripe"].count("/v1/payment_intents") == 1


# --- status transitions ------------------------------------------------------------------

def _declined(env, pi_id="pi_1"):
    env["stripe"].decline(pi_id)
    return intent_of(env, pi_id)


def test_declined_then_retried_then_paid(env):
    charge(env)
    webhook(env, stripe_event("payment_intent.payment_failed", _declined(env)))
    failed = body_of(api(env, "GET", CHECKOUT))
    assert failed["status"] == "failed" and failed["failure"]["code"] == "insufficient_funds"
    env["clock"]["now"] = NOW + timedelta(seconds=30)
    retried = body_of(api(env, "POST", f"{CHECKOUT}/retry"))
    assert retried["status"] == "processing" and retried["attempt"] == 2 and "failure" not in retried
    env["stripe"].pay("pi_1")
    webhook(env, stripe_event("payment_intent.succeeded", intent_of(env), created=int(NOW.timestamp()) + 40))
    paid = body_of(api(env, "GET", CHECKOUT))
    assert paid["status"] == "succeeded" and paid["card"]["last4"] == "4242"


def test_cancel_clears_the_reader_and_can_be_retried_on_another_reader(env):
    charge(env)
    canceled = body_of(api(env, "POST", f"{CHECKOUT}/cancel"))
    assert canceled["status"] == "canceled"
    assert env["stripe"].readers["tmr_a1"]["action"] is None
    # tmr_a2 belongs to the other location: not usable here.
    assert api(env, "POST", f"{CHECKOUT}/retry", body={"readerId": "tmr_a2"})["statusCode"] == 404
    assert body_of(api(env, "POST", f"{CHECKOUT}/retry", body={"readerId": "tmr_a1"}))["status"] == "processing"


def test_cancel_after_the_card_went_through_shows_paid(env):
    charge(env)
    env["stripe"].pay("pi_1")
    assert body_of(api(env, "POST", f"{CHECKOUT}/cancel"))["status"] == "succeeded"


@pytest.mark.parametrize("action", ["cancel", "retry"])
def test_paid_payments_cannot_be_cancelled_or_retried(env, action):
    charge(env)
    env["stripe"].pay("pi_1")
    webhook(env, stripe_event("payment_intent.succeeded", intent_of(env)))
    response = api(env, "POST", f"{CHECKOUT}/{action}")
    assert response["statusCode"] == 409


def test_reader_offline_fails_the_try_with_a_clear_code(env):
    env["stripe"].process_error = stripe_client.StripeError(400, {"code": "terminal_reader_offline"})
    payment = body_of(charge(env))
    assert payment["status"] == "failed" and payment["failure"]["code"] == "reader_offline"
    env["stripe"].process_error = None
    assert body_of(api(env, "POST", f"{CHECKOUT}/retry"))["status"] == "processing"


def test_poll_falls_back_to_stripe_when_the_webhook_is_late(env):
    charge(env)
    env["stripe"].pay("pi_1")
    assert body_of(api(env, "GET", CHECKOUT))["status"] == "processing"  # too fresh to ask Stripe
    env["clock"]["now"] = NOW + timedelta(seconds=cp.SYNC_AFTER_SECONDS + 1)
    paid = body_of(api(env, "GET", CHECKOUT))
    assert paid["status"] == "succeeded" and paid["card"]["brand"] == "visa"


def test_poll_sees_a_decline_on_the_reader(env):
    charge(env)
    env["stripe"].decline("pi_1")
    env["clock"]["now"] = NOW + timedelta(seconds=cp.SYNC_AFTER_SECONDS + 1)
    payment = body_of(api(env, "GET", CHECKOUT))
    assert payment["status"] == "failed" and payment["failure"]["code"] == "card_declined"


def test_simulated_reader_taps_a_card_in_test_mode_only(env, monkeypatch):
    charge(env)
    assert api(env, "POST", f"{CHECKOUT}/simulate", body={"outcome": "approve"})["statusCode"] == 202
    assert env["stripe"].intents["pi_1"]["status"] == "succeeded"
    monkeypatch.setenv("ENVIRONMENT", "prod")
    assert api(env, "POST", f"{CHECKOUT}/simulate", body={"outcome": "approve"})["statusCode"] == 404


def test_a_real_reader_cannot_be_simulated(env):
    env["stripe"].readers["tmr_a1"]["device_type"] = "stripe_s700"
    charge(env)
    assert api(env, "POST", f"{CHECKOUT}/simulate", body={"outcome": "approve"})["statusCode"] == 409


# --- webhook ------------------------------------------------------------------------------

def test_webhook_rejects_a_bad_signature(env):
    charge(env)
    env["stripe"].pay("pi_1")
    response = webhook(env, stripe_event("payment_intent.succeeded", intent_of(env)), secret="whsec_wrong")
    assert response["statusCode"] == 400
    assert cp.load(LOC_A, CHECKOUT)["status"] == "processing"


def test_duplicate_success_events_are_no_ops(env):
    charge(env)
    env["stripe"].pay("pi_1")
    event = stripe_event("payment_intent.succeeded", intent_of(env))
    assert body_of(webhook(env, event))["result"] == "succeeded"
    assert body_of(webhook(env, event))["result"] == "already_settled"
    assert len([h for h in cp.load(LOC_A, CHECKOUT)["history"] if h["action"] == "paid"]) == 1


def test_a_failure_arriving_after_the_success_changes_nothing(env):
    charge(env)
    env["stripe"].pay("pi_1")
    paid = intent_of(env)
    env["stripe"].decline("pi_1")
    late_failure = intent_of(env)
    webhook(env, stripe_event("payment_intent.succeeded", paid))
    webhook(env, stripe_event("payment_intent.payment_failed", late_failure, created=int(NOW.timestamp()) - 1))
    assert cp.load(LOC_A, CHECKOUT)["status"] == "succeeded"


def test_a_failure_of_an_earlier_try_does_not_fail_the_retry(env):
    charge(env)
    declined = _declined(env)
    webhook(env, stripe_event("payment_intent.payment_failed", declined, event_id="evt_a"))
    env["clock"]["now"] = NOW + timedelta(seconds=60)
    api(env, "POST", f"{CHECKOUT}/retry")
    # Stripe redelivers the first try's failure (created before the retry).
    result = body_of(webhook(env, stripe_event("payment_intent.payment_failed", declined, event_id="evt_a",
                                               created=int(NOW.timestamp()) + 5)))
    assert result["result"] == "stale"
    assert cp.load(LOC_A, CHECKOUT)["status"] == "processing"


def test_a_success_after_a_cancel_wins(env):
    charge(env)
    api(env, "POST", f"{CHECKOUT}/cancel")
    env["stripe"].pay("pi_1")
    webhook(env, stripe_event("payment_intent.succeeded", intent_of(env)))
    assert cp.load(LOC_A, CHECKOUT)["status"] == "succeeded"


def test_reader_action_events_are_matched_through_the_payment_intent(env):
    charge(env)
    env["stripe"].decline("pi_1")
    reader = env["stripe"]._public(env["stripe"].readers["tmr_a1"])
    result = body_of(webhook(env, stripe_event("terminal.reader.action_failed", reader)))
    assert result["result"] == "failed"
    assert cp.load(LOC_A, CHECKOUT)["failure"]["code"] == "card_declined"
    env["clock"]["now"] = NOW + timedelta(seconds=30)
    api(env, "POST", f"{CHECKOUT}/retry")
    env["stripe"].pay("pi_1")
    reader = env["stripe"]._public(env["stripe"].readers["tmr_a1"])
    result = body_of(webhook(env, stripe_event("terminal.reader.action_succeeded", reader,
                                               created=int(NOW.timestamp()) + 40)))
    assert result["result"] == "succeeded"


def test_events_from_another_account_or_mode_are_ignored(env):
    charge(env)
    env["stripe"].pay("pi_1")
    intent = intent_of(env)
    assert body_of(webhook(env, stripe_event("payment_intent.succeeded", intent, account="acct_other")))["result"] \
        == "ignored"
    assert body_of(webhook(env, stripe_event("payment_intent.succeeded", intent, livemode=True)))["ignored"] \
        == "livemode"
    tampered = dict(intent, amount=1)
    assert body_of(webhook(env, stripe_event("payment_intent.succeeded", tampered)))["result"] == "ignored:mismatch"
    assert cp.load(LOC_A, CHECKOUT)["status"] == "processing"


def test_online_order_payment_intents_are_ignored(env):
    result = body_of(webhook(env, stripe_event("payment_intent.succeeded",
                                               {"id": "pi_web", "metadata": {"orderId": "x", "locationId": LOC_A}})))
    assert result["result"] == "ignored"


# --- whose reader, whose payment -----------------------------------------------------------

def test_staff_see_only_their_locations_readers(env):
    readers = body_of(api(env, "GET", "readers", role="staff_user"))
    assert [r["id"] for r in readers["readers"]] == ["tmr_a1"]
    assert readers["readers"][0]["simulated"] is True
    assert api(env, "GET", "readers", role="staff_user", location_id=LOC_A2)["statusCode"] == 404


def test_a_reader_of_another_location_or_restaurant_cannot_be_charged_on(env):
    assert charge(env, reader="tmr_a2")["statusCode"] == 404
    assert charge(env, reader="tmr_b1")["statusCode"] == 404
    assert api(env, "POST", body={"checkoutId": CHECKOUT, "readerId": "evil/../x", "amount": 500})["statusCode"] == 400
    assert env["stripe"].count("/v1/payment_intents") == 0


def test_the_account_comes_from_the_location_row_not_the_request(env):
    response = api(env, "POST", body={"checkoutId": CHECKOUT, "readerId": "tmr_a1", "amount": 500,
                                      "account": "acct_other"})
    assert response["statusCode"] == 400  # unknown field
    charge(env)
    assert {c["account"] for c in env["stripe"].calls} == {ACCOUNT}


def test_another_restaurant_cannot_see_or_touch_a_payment(env):
    charge(env)
    for method, suffix in (("GET", CHECKOUT), ("POST", f"{CHECKOUT}/cancel"), ("POST", f"{CHECKOUT}/refund")):
        assert api(env, method, suffix, tenant_id=TENANT_B, location_id=LOC_B)["statusCode"] == 404
    assert api(env, "GET", CHECKOUT, tenant_id=TENANT_B, location_id=LOC_A)["statusCode"] == 404


def test_location_without_terminals_answers_clearly(env):
    env["res"].Table(LOCATION_TABLE).put_item(Item=location_item(TENANT_A, LOC_A))
    assert body_of(api(env, "GET", "readers")) == {"enabled": False, "readers": []}
    response = charge(env)
    assert response["statusCode"] == 409 and body_of(response)["error"] == "terminal_not_enabled"


def test_plan_without_terminal_feature_is_refused(env):
    env["res"].Table(TENANT_TABLE).put_item(Item=tenant_row(
        TENANT_A, stripe={"accountId": ACCOUNT}, features={"reservations": True, "ordering": True}))
    shared_tenant.reset_caches()
    assert body_of(api(env, "GET", "readers"))["error"] == "feature_not_in_plan"


# --- refunds -------------------------------------------------------------------------------

def _paid(env, **extra):
    charge(env, **extra)
    env["stripe"].pay("pi_1")
    webhook(env, stripe_event("payment_intent.succeeded", intent_of(env)))


def test_owner_refunds_a_paid_card_payment_once(env):
    _paid(env)
    assert api(env, "POST", f"{CHECKOUT}/refund", role="staff_user")["statusCode"] == 403
    refunded = body_of(api(env, "POST", f"{CHECKOUT}/refund"))
    assert refunded["status"] == "refunded" and refunded["refund"]["amount"] == 12550
    refund_call = next(c for c in env["stripe"].calls if c["path"] == "/v1/refunds")
    assert refund_call["key"] == f"card-refund-{CHECKOUT}" and refund_call["account"] == ACCOUNT
    assert api(env, "POST", f"{CHECKOUT}/refund")["statusCode"] == 409
    # Stripe's charge.refunded for it is a no-op.
    ch = env["stripe"].charges["ch_pi_1"]
    assert body_of(webhook(env, stripe_event("charge.refunded", ch)))["result"] == "already_recorded"


def test_refund_of_an_unpaid_payment_is_refused(env):
    charge(env)
    assert body_of(api(env, "POST", f"{CHECKOUT}/refund"))["error"] == "cannot_refund_processing"


def test_dashboard_refunds_are_recorded_partial_then_full(env):
    _paid(env)
    ch = dict(env["stripe"].charges["ch_pi_1"], amount_refunded=5000)
    assert body_of(webhook(env, stripe_event("charge.refunded", ch)))["result"] == "recorded"
    item = cp.load(LOC_A, CHECKOUT)
    assert item["status"] == "succeeded" and item["refund"]["amount"] == 5000
    ch["amount_refunded"] = 12550
    webhook(env, stripe_event("charge.refunded", ch, event_id="evt_2"))
    assert cp.load(LOC_A, CHECKOUT)["status"] == "refunded"


# --- paying an unpaid order ---------------------------------------------------------------

def test_paying_an_order_confirms_it_like_an_online_payment(env):
    order_id = put_order(env)
    payment = body_of(api(env, "POST", body={"checkoutId": CHECKOUT, "readerId": "tmr_a1", "orderId": order_id}))
    assert payment["amount"] == 28550 and payment["orderId"] == order_id
    assert env["stripe"].session["status"] == "expired"  # the guest can't also pay online
    order = o.load(LOC_A, order_id)
    assert order["cardPaymentId"] == CHECKOUT and order["status"] == o.PENDING_PAYMENT
    env["stripe"].pay("pi_1")
    webhook(env, stripe_event("payment_intent.succeeded", intent_of(env)))
    order = o.load(LOC_A, order_id)
    # Same transition as online: print-jobs (pending_payment -> confirmed) and the receipt notice follow.
    assert order["status"] == o.CONFIRMED and order["paymentStatus"] == "paid"
    assert order["paymentMethod"] == "card_present" and order["paymentIntentId"] == "pi_1"
    assert order["notice"]["type"] == o.NOTICE_CONFIRMED and order["number"] == 1
    assert "ttl" not in order
    item = cp.load(LOC_A, CHECKOUT)
    assert item["orderNumber"] == 1 and [line["name"] for line in item["lines"]] == ["Margherita", "Cola"]


def test_the_expired_online_checkout_never_fails_an_order_being_paid_at_the_till(env):
    from shared import order_payments
    order_id = put_order(env)
    api(env, "POST", body={"checkoutId": CHECKOUT, "readerId": "tmr_a1", "orderId": order_id})
    order = o.load(LOC_A, order_id)
    assert order_payments.settle_failed(order, NOW, "stripe", "checkout_expired") is None
    assert order_payments.settle_failed({**order, "cardPaymentId": None}, NOW, "stripe", "checkout_expired") is None
    assert o.load(LOC_A, order_id)["status"] == o.PENDING_PAYMENT


def test_an_order_the_guest_paid_online_meanwhile_is_not_charged_again(env):
    order_id = put_order(env)
    env["stripe"].session.update(status="complete", payment_status="paid", payment_intent="pi_web",
                                 metadata={"orderId": order_id, "locationId": LOC_A}, amount_total=28550,
                                 currency="sek")
    response = api(env, "POST", body={"checkoutId": CHECKOUT, "readerId": "tmr_a1", "orderId": order_id})
    assert response["statusCode"] == 409 and body_of(response)["error"] == "order_already_paid"
    order = o.load(LOC_A, order_id)
    assert order["status"] == o.CONFIRMED and "cardPaymentId" not in order
    assert env["stripe"].count("/v1/payment_intents") == 0


def test_paid_or_foreign_orders_cannot_be_charged(env):
    paid = put_order(env, status=o.CONFIRMED, order_id=_hex(8))
    other = put_order(env, account="acct_other", order_id=_hex(9))
    for order_id, code in ((paid, "order_not_payable"), (other, "order_other_account")):
        response = api(env, "POST", body={"checkoutId": CHECKOUT, "readerId": "tmr_a1", "orderId": order_id})
        assert body_of(response)["error"] == code
    assert api(env, "POST", body={"checkoutId": CHECKOUT, "readerId": "tmr_a1", "orderId": paid,
                                  "amount": 500})["statusCode"] == 400


def test_a_card_payment_for_an_order_is_refunded_by_cancelling_the_order(env):
    order_id = put_order(env)
    api(env, "POST", body={"checkoutId": CHECKOUT, "readerId": "tmr_a1", "orderId": order_id})
    env["stripe"].pay("pi_1")
    webhook(env, stripe_event("payment_intent.succeeded", intent_of(env)))
    assert body_of(api(env, "POST", f"{CHECKOUT}/refund"))["error"] == "refund_via_order"


# --- receipts -------------------------------------------------------------------------------

def test_receipt_view_and_email_are_swedish_with_the_card_fields(env):
    _paid(env, note="Bord 4")
    response = body_of(api(env, "GET", f"{CHECKOUT}/receipt"))
    r = response["receipt"]
    assert r["total"] == 12550 and r["seller"]["org"] == "556000-0000" and r["vatOnRegister"] is False
    assert r["vat"] == [{"rate": 12, "vat": 1345, "net": 11205}]
    labels = {row["label"]: row["value"] for row in r["card"]}
    assert labels["Kort"] == "Visa **** 4242" and labels["AID"] == "A0000000031010"
    assert labels["Godkännandekod"] == "123456" and labels["Läst"] == "Kontaktlös"
    item = cp.load(LOC_A, CHECKOUT)
    subject, text, html_body = card_messages.email(item, {"name": "Roma", "legalName": "Roma AB"},
                                                   {"name": "Södermalm"})
    assert subject.startswith("Kvitto 125,50 kr")
    assert "Totalt: 125,50 kr" in text and "Visa **** 4242" in html_body
    assert "Moms 12 %: 13,45 kr" in text and "kassakvitto" not in text
    assert "\u2014" not in text + html_body and "\u2013" not in text + html_body


def test_send_receipt_sets_a_notice_and_is_limited(env):
    charge(env)
    assert body_of(api(env, "POST", f"{CHECKOUT}/receipt", body={"email": "gast@example.se"}))["error"] == "not_paid"
    env["stripe"].pay("pi_1")
    webhook(env, stripe_event("payment_intent.succeeded", intent_of(env)))
    assert api(env, "POST", f"{CHECKOUT}/receipt", body={"email": "nope"})["statusCode"] == 400
    for _ in range(cp.MAX_RECEIPTS):
        assert api(env, "POST", f"{CHECKOUT}/receipt", body={"email": "Gast@Example.se"})["statusCode"] == 200
    item = cp.load(LOC_A, CHECKOUT)
    assert item["notice"]["type"] == cp.NOTICE_RECEIPT and item["receiptEmail"] == "gast@example.se"
    assert body_of(api(env, "POST", f"{CHECKOUT}/receipt", body={"email": "gast@example.se"}))["error"] \
        == "receipt_limit"


def test_card_records_never_carry_the_orders_payment_status(env):
    _paid(env)
    assert "paymentStatus" not in cp.load(LOC_A, CHECKOUT)  # marketing-sync and print-jobs filter on it


def test_day_list_newest_first(env):
    charge(env)
    env["clock"]["now"] = NOW + timedelta(minutes=1)
    charge(env, checkout=CHECKOUT_2, amount=900)
    listed = body_of(api(env, "GET", query={"date": TODAY}))
    assert [p["paymentId"] for p in listed["payments"]] == [CHECKOUT_2, CHECKOUT] and listed["enabled"] is True
    assert api(env, "GET", query={"date": "2026-13-01"})["statusCode"] == 400


# --- Statistik ------------------------------------------------------------------------------

def test_insights_count_card_payments_without_double_counting_orders():
    location = {"locationId": LOC_A, "timezone": "Europe/Stockholm"}
    card = [
        {"status": "succeeded", "amount": 10000, "paidAt": "2026-09-08T10:30:00Z"},                 # 12 local
        {"status": "succeeded", "amount": 28550, "orderId": "o1", "paidAt": "2026-09-08T17:00:00Z"},
        {"status": "refunded", "amount": 5000, "refund": {"amount": 5000}, "paidAt": "2026-09-08T17:10:00Z"},
        {"status": "failed", "amount": 9900, "createdAt": "2026-09-08T17:20:00Z"},
    ]
    row = insights.build_day(location=location, day=TODAY, card=card, built_at="x")
    assert row["k"]["12"] == {"n": 1, "rev": 10000, "free": 10000, "rfN": 0, "rf": 0}
    assert row["k"]["19"] == {"n": 1, "rev": 28550, "free": 0, "rfN": 1, "rf": 5000}
    summary = insights.summarize([row])
    assert summary["cardPayments"] == 2 and summary["cardRevenue"] == 38550
    assert summary["inPersonRevenue"] == 10000 and summary["revenue"] == 10000
    assert summary["cardRefunds"] == 1 and summary["cardRefunded"] == 5000
    assert insights.summarize([row], service="lunch")["cardPayments"] == 1
    assert "cardRevenue" not in insights.without_money(summary)


# --- retention ------------------------------------------------------------------------------

def test_card_payment_retention():
    policy = retention.policy()
    paid = {"date": "2026-01-10", "status": "succeeded", "receiptEmail": "a@b.se"}
    assert retention.card_action(paid, date(2026, 6, 1), policy) is None
    assert retention.card_action(paid, date(2028, 2, 1), policy) == "anonymise"
    assert retention.card_action({**paid, "receiptEmail": None}, date(2028, 2, 1), policy) is None
    assert retention.card_action(paid, date(2034, 1, 1), policy) == "delete"
    failed = {"date": "2026-01-10", "status": "failed"}
    assert retention.card_action(failed, date(2027, 2, 1), policy) == "delete"
    expr, names, _ = retention.anonymise_update("card", {"receiptEmail": "a@b.se", "notice": {}}, "now")
    assert "REMOVE" in expr and set(names.values()) == {"anonymisedAt", "receiptEmail", "notice"}


# --- the receipt e-mail (notification, order stream) -----------------------------------------

class _FakeClient:
    def __init__(self):
        self.calls = []

    def send_email(self, **kwargs):
        self.calls.append(kwargs)

    def publish(self, **kwargs):
        self.calls.append(kwargs)


def test_notification_emails_the_card_receipt_once(env, monkeypatch):
    from boto3.dynamodb.types import TypeSerializer
    ser = TypeSerializer()
    monkeypatch.setenv("NO_REPLY_EMAIL_ADDRESS", "no-reply@mail.example.se")
    notify = load_fn("notification", "term_notify")
    ses, sns = _FakeClient(), _FakeClient()
    notify._clients.update(ses=ses, sns=sns)
    notify._now = lambda: NOW
    _paid(env, note="Bord 4")
    before = cp.load(LOC_A, CHECKOUT)
    api(env, "POST", f"{CHECKOUT}/receipt", body={"email": "gast@example.se"})
    item = cp.load(LOC_A, CHECKOUT)
    arn = f"arn:aws:dynamodb:{REGION}:1:table/{ORDER_TABLE}/stream/x"

    def record(new, old):
        return {"eventName": "MODIFY", "eventSourceARN": arn, "dynamodb": {
            "SequenceNumber": "1", "NewImage": {k: ser.serialize(v) for k, v in new.items()},
            "OldImage": {k: ser.serialize(v) for k, v in old.items()}}}

    assert notify.handler({"Records": [record(item, before)]}, None) == {"batchItemFailures": []}
    (mail,) = ses.calls
    assert mail["Destination"]["ToAddresses"] == ["gast@example.se"]
    assert mail["Message"]["Subject"]["Data"].startswith("Kvitto 125,50 kr")
    assert sns.calls == []
    notify.handler({"Records": [record(item, item)]}, None)  # same notice again: not resent
    assert len(ses.calls) == 1

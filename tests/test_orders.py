"""Food orders end to end: POST /order (prices from the menu, opening hours,
QR codes, VAT, abuse limits), the Stripe webhook, the guest's status page,
the restaurant's screen (status moves, ready SMS, cancel with refund, pause,
table codes) and the guest messages. DynamoDB/Secrets Manager are moto;
Stripe, SES and SNS are recording fakes."""

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
from boto3.dynamodb.types import TypeSerializer
from moto import mock_aws

from shared import dynamo as shared_dynamo
from shared import manage_link, order_messages, stripe_client
from shared import orders as o
from shared import tenant as shared_tenant
from tenant_support import (LOC_A, LOC_A2, LOC_B, REGION, STAFF_A_SUB, TENANT_A, TENANT_B, TENANT_TABLE,
                            create_tables, tenant_row, with_tenant_claims)

ROOT = Path(__file__).parents[1] / "functions"
LOCATION_TABLE = "test-location"
ORDER_TABLE = "test-order"
MENU_TABLE = "test-menu"
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)  # 14:00 in Stockholm, a Tuesday
TODAY = "2026-09-08"
WEEK = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
ACCOUNT = "acct_roma"
WHSEC = "whsec_test_secret"
POLICY = {"enabled": True, "pickup": True, "table": True, "prepMinutes": 20, "lastOrderMinutes": 15}
_ser = TypeSerializer()


def location_item(tenant_id, location_id, **extra):
    item = {
        "PK": f"TENANT#{tenant_id}", "SK": f"LOCATION#{location_id}", "tenantId": tenant_id,
        "locationId": location_id, "name": "Södermalm", "address": "Götgatan 1, Stockholm",
        "phoneNumber": "+46812345678", "timezone": "Europe/Stockholm",
        "businessHours": {d: [{"opensAt": "10:00", "closesAt": "22:00"}] for d in WEEK},
        "bookingDurationHours": Decimal("2"), "gracePeriodHours": Decimal("0.25"),
        "createdBy": "creator", "createdAt": "2026-08-20T10:00:00Z", "ordering": dict(POLICY),
    }
    item.update(extra)
    return {k: v for k, v in item.items() if v is not None}


def menu_item(location_id, item_id, name, price, *, active=True, alcohol=None, category="mains"):
    item = {"PK": f"LOCATION#{location_id}", "SK": f"MENU#{item_id}", "menuItemId": item_id,
            "name": name, "description": "", "price": Decimal(price), "category": category,
            "imageKey": "img", "active": active, "createdBy": "o", "createdAt": "2026-08-01T00:00:00Z",
            "updatedBy": "o", "updatedAt": "2026-08-01T00:00:00Z"}
    if alcohol is not None:
        item["alcohol"] = alcohol
    return item


def load(name, module_name):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / name / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeStripe:
    def __init__(self):
        self.calls = []
        self.session = {}
        self.fail = None

    def __call__(self, method, path, params=None, *, account=None, idempotency_key=None):
        self.calls.append({"method": method, "path": path, "params": params or {}, "account": account,
                           "key": idempotency_key})
        if self.fail:
            raise self.fail
        if path == "/v1/checkout/sessions":
            total = sum(li["price_data"]["unit_amount"] * li["quantity"] for li in params["line_items"])
            self.session = {"id": "cs_test_1", "url": "https://checkout.stripe.com/c/pay/cs_test_1",
                            "metadata": params["metadata"], "amount_total": total, "currency": "sek",
                            "payment_status": "unpaid", "status": "open", "payment_intent": None}
            return dict(self.session)
        if path.startswith("/v1/checkout/sessions/") and path.endswith("/expire"):
            self.session = dict(self.session, status="expired")
            return dict(self.session)
        if path.startswith("/v1/checkout/sessions/"):
            return dict(self.session)
        if path == "/v1/refunds":
            return {"id": "re_1", "amount": 25000, "status": "succeeded"}
        raise AssertionError(path)


class FakeClient:
    def __init__(self):
        self.calls = []

    def send_email(self, **kwargs):
        self.calls.append(kwargs)

    def publish(self, **kwargs):
        self.calls.append(kwargs)


@pytest.fixture
def env(monkeypatch):
    for key, value in {
        "ENVIRONMENT": "dev", "AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t",
        "AWS_SESSION_TOKEN": "t", "AWS_DEFAULT_REGION": REGION, "ORDER_TABLE_NAME": ORDER_TABLE,
        "MENU_TABLE_NAME": MENU_TABLE, "CUSTOMER_SITE_URL": "https://dev123.cloudfront.net",
        "NO_REPLY_EMAIL_ADDRESS": "no-reply@mail.example.se", "USER_TABLE_NAME": "test-user",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("REQUEST_GUARD_TABLE_NAME", raising=False)
    monkeypatch.delenv("TURNSTILE_REQUIRED", raising=False)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        manage_link.reset_cache()
        sm = boto3.client("secretsmanager", region_name=REGION)
        monkeypatch.setenv("RESERVATION_LINK_KEY_SECRET_ARN",
                           sm.create_secret(Name="link", SecretString="k" * 64)["ARN"])
        monkeypatch.setenv("ORDER_STRIPE_WEBHOOK_SECRET_ARN",
                           sm.create_secret(Name="whsec", SecretString=WHSEC)["ARN"])
        res = boto3.resource("dynamodb", region_name=REGION)
        for name in (ORDER_TABLE, MENU_TABLE):
            res.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
                AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                      {"AttributeName": "SK", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST")
        create_tables(monkeypatch, location_table=LOCATION_TABLE)
        stripe_state = {"accountId": ACCOUNT, "chargesEnabled": True}
        res.Table(TENANT_TABLE).put_item(Item=tenant_row(
            TENANT_A, name="Roma", slug="roma", stripe=stripe_state, legalName="Roma AB",
            orgNumber="556000-0000", notifications={"sms": False}))
        res.Table(TENANT_TABLE).put_item(Item=tenant_row(
            TENANT_B, slug="other", stripe={"accountId": "acct_other", "chargesEnabled": True}))
        for tenant_id, loc in ((TENANT_A, LOC_A), (TENANT_A, LOC_A2), (TENANT_B, LOC_B)):
            res.Table(LOCATION_TABLE).put_item(Item=location_item(tenant_id, loc))
        res.Table("test-user").put_item(Item={"PK": f"USER#{STAFF_A_SUB}", "SK": "PROFILE",
                                              "tenantId": TENANT_A, "locationId": LOC_A, "status": "active"})
        menu = res.Table(MENU_TABLE)
        for item in (menu_item(LOC_A, "pizza", "Margherita", "125"),
                     menu_item(LOC_A, "cola", "Cola", "35.50", category="drinks"),
                     menu_item(LOC_A, "wine", "Husets röda", "95", alcohol=True, category="drinks"),
                     menu_item(LOC_A, "old", "Borttagen", "50", active=False),
                     menu_item(LOC_B, "pizza", "Other pizza", "1")):
            menu.put_item(Item=item)
        fake = FakeStripe()
        monkeypatch.setattr(stripe_client, "request", fake)
        monkeypatch.setattr(o, "now_utc", lambda: NOW)
        mods = {"create": load("payment-intent", "ord_create"), "hook": load("webhook-payment-intent", "ord_hook"),
                "get": load("get-order", "ord_get"), "staff": load("manage-order", "ord_staff")}
        mods["hook"]._secret.update(value=None, at=0.0)
        yield {"m": mods, "res": res, "stripe": fake, "monkeypatch": monkeypatch}
        shared_dynamo._resource = None
        shared_dynamo._client = None
        shared_tenant.reset_caches()
        manage_link.reset_cache()


# --- helpers --------------------------------------------------------------------------------

def body_of(response):
    return json.loads(response["body"])


def order_request(**extra):
    data = {"locationId": LOC_A, "mode": "pickup", "items": [{"menuItemId": "pizza", "quantity": 2},
                                                              {"menuItemId": "cola", "quantity": 1}],
            "name": "Anna Svensson", "phone": "070-123 45 67", "email": "anna@example.se",
            "acceptTerms": True}
    data.update(extra)
    return {k: v for k, v in data.items() if v is not None}


def post_order(env, **extra):
    event = {"routeKey": "POST /order", "body": json.dumps(order_request(**extra)),
             "requestContext": {"http": {"method": "POST", "sourceIp": "203.0.113.5"}}}
    return env["m"]["create"].handler(event, None)


def stored(env, order_id):
    return o.load(LOC_A, order_id)


def stripe_event(kind, obj, *, account=ACCOUNT, livemode=False):
    return {"id": "evt_1", "type": kind, "account": account, "livemode": livemode,
            "data": {"object": obj}}


def webhook(env, event, *, secret=WHSEC):
    payload = json.dumps(event)
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    return env["m"]["hook"].handler({"body": payload, "headers": {"Stripe-Signature": f"t={ts},v1={sig}"},
                                     "requestContext": {"http": {"method": "POST"}}}, None)


def paid_session(env, order_id, **extra):
    session = dict(env["stripe"].session, payment_status="paid", status="complete",
                   payment_intent="pi_1")
    session.update(extra)
    return session


def place_and_pay(env, **extra):
    created = body_of(post_order(env, **extra))
    webhook(env, stripe_event("checkout.session.completed", paid_session(env, created["orderId"])))
    return created


def staff_event(method, path_suffix="", *, body=None, query=None, role="owner_user", tenant_id=TENANT_A,
                location_id=LOC_A):
    if path_suffix:
        event = {"routeKey": f"{method} /locations/{{locationId}}/orders/{{proxy+}}",
                 "pathParameters": {"locationId": location_id, "proxy": path_suffix}}
    else:
        event = {"routeKey": f"{method} /locations/{{locationId}}/orders",
                 "pathParameters": {"locationId": location_id}}
    if body is not None:
        event["body"] = json.dumps(body)
    if query is not None:
        event["queryStringParameters"] = query
    return with_tenant_claims(event, tenant_id, role)


def set_location(env, **fields):
    env["res"].Table(LOCATION_TABLE).put_item(Item=location_item(TENANT_A, LOC_A, **fields))


# --- pure rules -----------------------------------------------------------------------------

def test_vat_rates():
    assert o.vat_rate(False, "pickup", date(2026, 9, 8)) == 6
    assert o.vat_rate(False, "pickup", date(2028, 1, 1)) == 12
    assert o.vat_rate(False, "pickup", date(2026, 3, 31)) == 12
    assert o.vat_rate(False, "table", date(2026, 9, 8)) == 12
    assert o.vat_rate(True, "table", date(2026, 9, 8)) == 25


def test_vat_breakdown_is_computed_on_prices_including_vat():
    lines = [{"vatRate": 12, "total": 11200}, {"vatRate": 25, "total": 12500}, {"vatRate": 12, "total": 100}]
    assert o.vat_breakdown(lines) == [
        {"rate": 12, "gross": 11300, "net": 10089, "vat": 1211},
        {"rate": 25, "gross": 12500, "net": 10000, "vat": 2500},
    ]


@pytest.mark.parametrize("items, error", [
    ([], "items must be a non-empty list"),
    ([{"menuItemId": "a", "quantity": 0}], "quantity must be a whole number between 1 and 20"),
    ([{"menuItemId": "a", "quantity": 21}], "quantity must be a whole number between 1 and 20"),
    ([{"menuItemId": "a", "quantity": True}], "quantity must be a whole number"),
    ([{"menuItemId": "a", "quantity": 1.5}], "quantity must be a whole number"),
    ([{"menuItemId": "a", "quantity": 15}, {"menuItemId": "a", "quantity": 6}], "quantity must be at most 20 per dish"),
    ([{"menuItemId": "a", "quantity": 1, "price": 1}], "each item needs menuItemId and quantity"),
    ([{"menuItemId": f"i{n}", "quantity": 1} for n in range(31)], "items may have at most 30 lines"),
])
def test_order_lines_are_checked(items, error):
    with pytest.raises(ValueError, match=error):
        o.validate_lines(items)


def test_repeated_dish_lines_are_summed():
    assert o.validate_lines([{"menuItemId": "a", "quantity": 2}, {"menuItemId": "b", "quantity": 1},
                             {"menuItemId": "a", "quantity": 18}]) == [("a", 20), ("b", 1)]


def test_policy_validation():
    assert o.validate_policy({"enabled": True}) == {"enabled": True, "pickup": True, "table": True,
                                                    "prepMinutes": 20, "lastOrderMinutes": 15}
    with pytest.raises(ValueError, match="needs pickup or table"):
        o.validate_policy({"enabled": True, "pickup": False, "table": False})
    with pytest.raises(ValueError, match="prepMinutes"):
        o.validate_policy({"enabled": True, "prepMinutes": 2})
    with pytest.raises(ValueError, match="unsupported"):
        o.validate_policy({"enabled": True, "x": 1})
    assert o.validate_policy(None) is None


# --- placing an order -------------------------------------------------------------------------

def test_too_large_an_order_is_refused_and_logged_without_guest_data(env, caplog):
    import boto3
    boto3.resource("dynamodb", region_name=REGION).Table(MENU_TABLE).put_item(
        Item=menu_item(LOC_A, "car", "Porsche", "3500000"))
    with caplog.at_level("WARNING"):
        response = post_order(env, items=[{"menuItemId": "car", "quantity": 1}])
    assert response["statusCode"] == 400
    assert body_of(response) == {"error": "order total is too large"}
    refused = [r.getMessage() for r in caplog.records if "order refused" in r.getMessage()]
    assert refused == ["order refused: 400 order total is too large"]
    assert not any("anna" in r.getMessage().lower() or "4670" in r.getMessage() for r in caplog.records)


def test_pickup_order_is_priced_from_the_menu_and_sent_to_checkout(env):
    response = post_order(env)
    assert response["statusCode"] == 201, response
    body = body_of(response)
    assert body["checkoutUrl"].startswith("https://checkout.stripe.com/")
    assert body["totalAmount"] == 2 * 12500 + 3550
    item = stored(env, body["orderId"])
    assert item["status"] == "pending_payment" and item["paymentStatus"] == "processing"
    assert item["customerPhone"] == "+46701234567" and item["mode"] == "pickup"
    assert [(line["name"], line["quantity"], line["unitPrice"], line["vatRate"]) for line in item["lines"]] == [
        ("Margherita", 2, 12500, 6), ("Cola", 1, 3550, 6)]
    assert item["ttl"] == int((NOW + timedelta(minutes=60)).timestamp())
    assert item["checkoutSessionId"] == "cs_test_1"
    assert o.token_matches(item, body["statusToken"])

    (call,) = env["stripe"].calls
    assert call["account"] == ACCOUNT and call["key"] == f"checkout-{body['orderId']}"
    params = call["params"]
    assert params["mode"] == "payment" and params["customer_email"] == "anna@example.se"
    assert params["wallet_options"] == {"link": {"display": "never"}}
    assert params["line_items"][0] == {"price_data": {"currency": "sek", "unit_amount": 12500,
                                                      "product_data": {"name": "Margherita"}}, "quantity": 2}
    assert params["metadata"] == {"orderId": body["orderId"], "locationId": LOC_A, "tenantId": TENANT_A,
                                  "date": TODAY}
    assert params["success_url"] == (f"https://dev123.cloudfront.net/bestallning/{LOC_A}/{body['orderId']}"
                                     "?restaurang=roma")
    assert params["cancel_url"] == f"https://dev123.cloudfront.net/bestall?restaurang=roma&avbruten={body['orderId']}"


def test_table_order_needs_this_locations_qr_code(env):
    code = o.table_code(TENANT_A, LOC_A, "t-7", "Bord 7")
    created = post_order(env, mode="table", tableCode=code,
                         items=[{"menuItemId": "pizza", "quantity": 1}, {"menuItemId": "wine", "quantity": 2}])
    assert created["statusCode"] == 201, created
    item = stored(env, body_of(created)["orderId"])
    assert item["tableId"] == "t-7" and item["tableLabel"] == "Bord 7"
    assert [line["vatRate"] for line in item["lines"]] == [12, 25]
    assert item["vat"] == [{"rate": 12, "gross": 12500, "net": 11161, "vat": 1339},
                           {"rate": 25, "gross": 19000, "net": 15200, "vat": 3800}]

    other = o.table_code(TENANT_A, LOC_A2, "t-7", "Bord 7")
    assert body_of(post_order(env, mode="table", tableCode=other)) == {"error": "table code is invalid"}
    assert body_of(post_order(env, mode="table")) == {"error": "table code is invalid"}
    tampered = code.replace(code.split(".")[0], o._b64(b'{"t":"t-7","n":"VIP"}'))
    assert post_order(env, mode="table", tableCode=tampered)["statusCode"] == 400
    assert body_of(post_order(env, tableCode=code)) == {"error": "tableCode is only for table orders"}


@pytest.mark.parametrize("items, error", [
    ([{"menuItemId": "old", "quantity": 1}], "menu_item_unavailable"),
    ([{"menuItemId": "nope", "quantity": 1}], "menu_item_unavailable"),
    ([{"menuItemId": "wine", "quantity": 1}], "alcohol_not_for_pickup"),
    ([{"menuItemId": "pizza", "quantity": 21}], "quantity must be a whole number between 1 and 20"),
])
def test_unorderable_items_are_refused(env, items, error):
    response = post_order(env, items=items)
    assert response["statusCode"] == 400 and body_of(response) == {"error": error}
    assert env["stripe"].calls == []


def test_another_restaurants_dish_cannot_be_ordered(env):
    # LOC_B's "pizza" costs 1 kr - the menu is read from the ordering location only.
    body = body_of(post_order(env, items=[{"menuItemId": "pizza", "quantity": 1}]))
    assert body["totalAmount"] == 12500


@pytest.mark.parametrize("change, error", [
    ({"ordering": None}, "ordering_disabled"),
    ({"ordering": {**POLICY, "enabled": False}}, "ordering_disabled"),
    ({"ordering": {**POLICY, "pickup": False}}, "ordering_pickup_off"),
    ({"orderingPausedUntil": "2026-09-08T12:30:00Z"}, "ordering_paused"),
    ({"businessHours": {d: [{"opensAt": "15:00", "closesAt": "22:00"}] for d in WEEK}}, "ordering_closed"),
    ({"businessHours": {d: [{"opensAt": "10:00", "closesAt": "14:10"}] for d in WEEK}}, "ordering_closed"),
    ({"specialDays": [{"date": TODAY, "kind": "closed"}]}, "ordering_closed"),
])
def test_ordering_only_while_open(env, change, error):
    set_location(env, **change)
    response = post_order(env)
    assert response["statusCode"] == 409 and body_of(response) == {"error": error}


def test_expired_pause_and_special_hours_allow_orders(env):
    set_location(env, orderingPausedUntil="2026-09-08T11:00:00Z",
                 specialDays=[{"date": TODAY, "kind": "hours",
                               "intervals": [{"opensAt": "13:00", "closesAt": "16:00"}]}])
    assert post_order(env)["statusCode"] == 201


def test_no_stripe_account_no_orders(env):
    env["res"].Table(TENANT_TABLE).put_item(Item=tenant_row(TENANT_A, slug="roma",
                                                            stripe={"accountId": ACCOUNT, "chargesEnabled": False}))
    shared_tenant.reset_caches()
    assert body_of(post_order(env)) == {"error": "ordering_no_payments"}


def test_tenant_without_ordering_feature_gets_404(env):
    env["res"].Table(TENANT_TABLE).put_item(Item=tenant_row(
        TENANT_A, slug="roma", features={"reservations": True}, stripe={"accountId": ACCOUNT, "chargesEnabled": True}))
    shared_tenant.reset_caches()
    assert post_order(env)["statusCode"] == 404


@pytest.mark.parametrize("change, error", [
    ({"acceptTerms": None}, "acceptTerms must be true"),
    ({"mode": "delivery"}, "mode must be pickup or table"),
    ({"phone": None}, "phone is required"),
    ({"email": "nope"}, "email must be a valid email address"),
    ({"price": 1}, "unsupported fields: price"),
    ({"notes": "x" * 301}, "notes may be at most 300 characters"),
])
def test_request_validation(env, change, error):
    assert body_of(post_order(env, **change)) == {"error": error}


def test_stripe_rejecting_the_checkout_fails_the_order(env):
    env["stripe"].fail = stripe_client.StripeError(400, {"message": "bad", "code": "x"})
    response = post_order(env)
    assert response["statusCode"] == 503
    items = o.list_day(LOC_A, TODAY)
    (order,) = [i for i in items if i["SK"].startswith("ORDER#")]
    assert order["status"] == "failed" and "ttl" not in order


def test_too_many_orders_from_one_address(env):
    env["res"].create_table(TableName="test-guard", KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}],
                            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}],
                            BillingMode="PAY_PER_REQUEST")
    env["monkeypatch"].setenv("REQUEST_GUARD_TABLE_NAME", "test-guard")
    for _ in range(10):
        assert post_order(env)["statusCode"] == 201
    response = post_order(env)
    assert response["statusCode"] == 429 and int(response["headers"]["Retry-After"]) > 0


def test_turnstile_is_checked_with_the_order_action(env):
    from shared import turnstile
    seen = []
    env["monkeypatch"].setenv("TURNSTILE_REQUIRED", "true")
    env["monkeypatch"].setattr(turnstile, "verify", lambda token, ip, action: seen.append((token, ip, action)))
    assert post_order(env, turnstileToken="tok")["statusCode"] == 201
    assert seen == [("tok", "203.0.113.5", "order")]


# --- webhook ------------------------------------------------------------------------------------

def test_paid_checkout_confirms_with_a_daily_number_and_a_notice(env):
    first = place_and_pay(env)
    item = stored(env, first["orderId"])
    assert item["status"] == "confirmed" and item["paymentStatus"] == "paid" and item["number"] == 1
    assert item["paymentIntentId"] == "pi_1" and "ttl" not in item
    assert item["readyEstimate"] == "2026-09-08T12:20:00Z"
    assert item["notice"]["type"] == "order_confirmed"

    second = place_and_pay(env)
    assert stored(env, second["orderId"])["number"] == 2


def test_duplicate_and_out_of_order_events_change_nothing(env):
    created = place_and_pay(env)
    before = stored(env, created["orderId"])
    session = paid_session(env, created["orderId"])
    assert body_of(webhook(env, stripe_event("checkout.session.completed", session)))["result"] == "already_settled"
    assert body_of(webhook(env, stripe_event("checkout.session.expired", session)))["result"] == "already_settled"
    assert stored(env, created["orderId"]) == before


def test_async_payment_waits_then_settles(env):
    created = body_of(post_order(env))
    unpaid = dict(env["stripe"].session, status="complete")
    assert body_of(webhook(env, stripe_event("checkout.session.completed", unpaid)))["result"] == "waiting"
    assert stored(env, created["orderId"])["status"] == "pending_payment"
    paid = paid_session(env, created["orderId"])
    assert body_of(webhook(env, stripe_event("checkout.session.async_payment_succeeded", paid)))["result"] == "confirmed"


@pytest.mark.parametrize("kind, reason", [("checkout.session.expired", "checkout_expired"),
                                          ("checkout.session.async_payment_failed", "payment_failed")])
def test_abandoned_or_failed_payment_fails_the_order(env, kind, reason):
    created = body_of(post_order(env))
    webhook(env, stripe_event(kind, dict(env["stripe"].session, status="expired")))
    item = stored(env, created["orderId"])
    assert item["status"] == "failed" and item["failureReason"] == reason and "ttl" not in item


def test_events_for_another_account_or_amount_are_ignored(env):
    created = body_of(post_order(env))
    session = paid_session(env, created["orderId"])
    assert body_of(webhook(env, stripe_event("checkout.session.completed", session, account="acct_evil")))["result"] == "ignored"
    cheap = dict(session, amount_total=100)
    assert body_of(webhook(env, stripe_event("checkout.session.completed", cheap)))["result"] == "ignored:mismatch"
    other = dict(session, id="cs_other")
    assert body_of(webhook(env, stripe_event("checkout.session.completed", other)))["result"] == "ignored:mismatch"
    assert stored(env, created["orderId"])["status"] == "pending_payment"


def test_bad_signature_and_wrong_mode(env):
    created = body_of(post_order(env))
    event = stripe_event("checkout.session.completed", paid_session(env, created["orderId"]))
    assert webhook(env, event, secret="whsec_wrong")["statusCode"] == 400
    assert body_of(webhook(env, dict(event, livemode=True))) == {"ignored": "livemode"}
    assert stored(env, created["orderId"])["status"] == "pending_payment"


def test_dashboard_refund_is_recorded(env):
    created = place_and_pay(env)
    total = int(stored(env, created["orderId"])["totalAmount"])
    charge = {"payment_intent": "pi_1", "amount_refunded": total,
              "metadata": {"orderId": created["orderId"], "locationId": LOC_A}}
    assert body_of(webhook(env, stripe_event("charge.refunded", charge)))["result"] == "recorded"
    item = stored(env, created["orderId"])
    assert item["paymentStatus"] == "refunded" and item["refund"]["amount"] == total
    assert body_of(webhook(env, stripe_event("charge.refunded", charge)))["result"] == "already_recorded"


# --- guest status ---------------------------------------------------------------------------------

def status_event(order_id, token, location_id=LOC_A):
    return {"routeKey": "GET /locations/{locationId}/order-status/{orderId}",
            "pathParameters": {"locationId": location_id, "orderId": order_id},
            "headers": {"X-Order-Token": token}}


def test_guest_sees_the_order_with_its_token_only(env):
    created = place_and_pay(env)
    response = env["m"]["get"].handler(status_event(created["orderId"], created["statusToken"]), None)
    body = body_of(response)
    assert response["statusCode"] == 200 and body["status"] == "confirmed" and body["number"] == 1
    assert body["location"]["name"] == "Södermalm" and "customerPhone" not in body
    for token in ("wrong", ""):
        assert env["m"]["get"].handler(status_event(created["orderId"], token), None)["statusCode"] == 404
    assert env["m"]["get"].handler(status_event(created["orderId"], created["statusToken"], LOC_A2),
                                   None)["statusCode"] == 404


def test_status_page_settles_a_paid_order_before_the_webhook(env):
    created = body_of(post_order(env))
    env["stripe"].session.update(payment_status="paid", status="complete", payment_intent="pi_9")
    body = body_of(env["m"]["get"].handler(status_event(created["orderId"], created["statusToken"]), None))
    assert body["status"] == "confirmed" and body["number"] == 1
    assert stored(env, created["orderId"])["paymentIntentId"] == "pi_9"


def test_ordering_state_and_table_label(env):
    code = o.table_code(TENANT_A, LOC_A, "t-3", "Fönsterbordet")
    event = {"routeKey": "GET /locations/{locationId}/ordering", "pathParameters": {"locationId": LOC_A},
             "queryStringParameters": {"table": code}}
    body = body_of(env["m"]["get"].handler(event, None))
    assert body == {"open": True, "pickup": True, "table": True, "prepMinutes": 20, "closesAt": "22:00",
                    "lastOrderAt": "21:45", "forTable": {"label": "Fönsterbordet"}}
    event["queryStringParameters"] = {"table": "junk.sig"}
    assert env["m"]["get"].handler(event, None)["statusCode"] == 400


# --- restaurant screen ------------------------------------------------------------------------------

def test_day_list_hides_unpaid_orders(env):
    paid = place_and_pay(env)
    post_order(env)
    body = body_of(env["m"]["staff"].handler(staff_event("GET", query={"date": TODAY}), None))
    assert [order["orderId"] for order in body["orders"]] == [paid["orderId"]]
    assert body["orders"][0]["customerPhone"] == "+46701234567" and body["ordering"]["open"] is True
    everything = body_of(env["m"]["staff"].handler(staff_event("GET", query={"date": TODAY, "all": "true"}), None))
    assert len(everything["orders"]) == 2


def set_status(env, order_id, status, **kw):
    return env["m"]["staff"].handler(staff_event("PUT", f"{order_id}/status", body={"status": status}, **kw), None)


def test_kitchen_flow_and_ready_sms_notice_for_pickup(env):
    created = place_and_pay(env)
    oid = created["orderId"]
    assert body_of(set_status(env, oid, "preparing"))["status"] == "preparing"
    notice_before = stored(env, oid)["notice"]
    ready = set_status(env, oid, "ready")
    assert body_of(ready)["status"] == "ready"
    item = stored(env, oid)
    assert item["notice"]["type"] == "order_ready" and item["notice"]["id"] != notice_before["id"]
    assert item["readyAt"] == "2026-09-08T12:00:00Z"
    assert body_of(set_status(env, oid, "completed"))["status"] == "completed"
    assert body_of(set_status(env, oid, "preparing")) == {"error": "cannot_move_completed_to_preparing"}


def test_table_order_ready_sends_nothing(env):
    created = place_and_pay(env, mode="table", tableCode=o.table_code(TENANT_A, LOC_A, "t1", "1"),
                            items=[{"menuItemId": "pizza", "quantity": 1}])
    set_status(env, created["orderId"], "ready")
    assert stored(env, created["orderId"])["notice"]["type"] == "order_confirmed"


def test_cancel_refunds_and_tells_the_guest(env):
    created = place_and_pay(env)
    env["stripe"].calls.clear()
    body = body_of(set_status(env, created["orderId"], "cancelled"))
    assert body["status"] == "cancelled" and body["paymentStatus"] == "refunded"
    (call,) = env["stripe"].calls
    assert call["path"] == "/v1/refunds" and call["params"]["payment_intent"] == "pi_1"
    assert call["account"] == ACCOUNT and call["key"] == f"refund-{created['orderId']}"
    assert stored(env, created["orderId"])["notice"]["type"] == "order_cancelled"


def test_failed_refund_keeps_the_order(env):
    created = place_and_pay(env)
    env["stripe"].fail = stripe_client.StripeError(400, {"message": "no", "code": "charge_already_refunded"})
    assert set_status(env, created["orderId"], "cancelled")["statusCode"] == 502
    assert stored(env, created["orderId"])["status"] == "confirmed"


def test_stale_screen_gets_409(env):
    created = place_and_pay(env)
    oid = created["orderId"]
    event = staff_event("PUT", f"{oid}/status", body={"status": "ready", "expectedStatus": "preparing"})
    assert body_of(env["m"]["staff"].handler(event, None)) == {"error": "changed_retry"}


def test_unpaid_orders_cannot_be_moved(env):
    created = body_of(post_order(env))
    assert body_of(set_status(env, created["orderId"], "ready")) == {"error": "cannot_move_pending_payment_to_ready"}


def test_other_tenant_and_other_location_staff_see_nothing(env):
    created = place_and_pay(env)
    assert set_status(env, created["orderId"], "ready", tenant_id=TENANT_B)["statusCode"] == 404
    staff = staff_event("GET", query={"date": TODAY}, role="staff_user", location_id=LOC_A2)
    assert env["m"]["staff"].handler(staff, None)["statusCode"] == 404
    own = staff_event("GET", query={"date": TODAY}, role="staff_user")
    assert len(body_of(env["m"]["staff"].handler(own, None))["orders"]) == 1


def test_pause_and_resume(env):
    body = body_of(env["m"]["staff"].handler(staff_event("PUT", "pause", body={"minutes": 30}), None))
    assert body["ordering"] == {"open": False, "reason": "paused", "pausedUntil": "2026-09-08T12:30:00Z",
                                "pickup": True, "table": True, "prepMinutes": 20}
    shared_tenant.reset_caches()
    assert body_of(post_order(env)) == {"error": "ordering_paused"}
    body = body_of(env["m"]["staff"].handler(staff_event("PUT", "pause", body={"minutes": 0}), None))
    assert body["ordering"]["open"] is True
    assert post_order(env)["statusCode"] == 201
    assert env["m"]["staff"].handler(staff_event("PUT", "pause", body={"minutes": 241}), None)["statusCode"] == 400


def test_table_codes_round_trip(env):
    body = body_of(env["m"]["staff"].handler(staff_event("POST", "table-codes", body={
        "tables": [{"tableId": "t1", "label": "  Bord   1 "}, {"tableId": "t2", "label": "Bar"}]}), None))
    assert [(t["tableId"], t["label"]) for t in body["tables"]] == [("t1", "Bord 1"), ("t2", "Bar")]
    assert o.read_table_code(TENANT_A, LOC_A, body["tables"][0]["code"]) == ("t1", "Bord 1")
    assert body["tables"][0]["url"].startswith("https://dev123.cloudfront.net/bestall?restaurang=roma&bord=")
    with pytest.raises(ValueError):
        o.read_table_code(TENANT_B, LOC_A, body["tables"][0]["code"])


# --- guest messages ------------------------------------------------------------------------------------

@pytest.fixture
def notify(env):
    spec = importlib.util.spec_from_file_location("ord_notify", ROOT / "notification" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ses, sns = FakeClient(), FakeClient()
    module._clients.update(ses=ses, sns=sns)
    module._now = lambda: NOW
    return module, ses, sns


def order_record(item, old=None):
    arn = f"arn:aws:dynamodb:{REGION}:1:table/{ORDER_TABLE}/stream/x"
    data = {"NewImage": {k: _ser.serialize(v) for k, v in item.items()}, "SequenceNumber": "1"}
    if old is not None:
        data["OldImage"] = {k: _ser.serialize(v) for k, v in old.items()}
    return {"eventName": "MODIFY", "eventSourceARN": arn, "dynamodb": data}


def test_paid_order_gets_the_receipt_but_no_sms_yet(env, notify):
    module, ses, sns = notify
    created = place_and_pay(env)
    item = stored(env, created["orderId"])
    assert module.handler({"Records": [order_record(item)]}, None) == {"batchItemFailures": []}
    (mail,) = ses.calls
    assert mail["Message"]["Subject"]["Data"] == "Kvitto beställning #1 - Roma Södermalm"
    text = mail["Message"]["Body"]["Text"]["Data"]
    assert "2 x Margherita  250 kr" in text and "Totalt: 285,50 kr" in text
    assert "Moms 6 %: 16,16 kr" in text and "Roma AB" in text and "556000-0000" in text
    assert "sms när restaurangen har börjat laga den" in text
    assert f"/bestallning/{LOC_A}/{created['orderId']}?restaurang=roma#" in text
    assert sns.calls == []  # the confirmation SMS waits for the kitchen


def test_pressing_start_sends_the_confirmation_sms_even_with_booking_sms_off(env, notify, monkeypatch):
    module, ses, sns = notify
    created = place_and_pay(env)
    # The kitchen starts 30 minutes after the payment: the estimate counts from now.
    later = NOW + timedelta(minutes=30)
    monkeypatch.setattr(o, "now_utc", lambda: later)
    started = set_status(env, created["orderId"], "preparing")
    assert body_of(started)["status"] == "preparing"
    item = stored(env, created["orderId"])
    assert item["notice"]["type"] == "order_started"
    assert item["readyEstimate"] == "2026-09-08T12:50:00Z"

    assert module.handler({"Records": [order_record(item, old=stored_before_start(item))]}, None) == {"batchItemFailures": []}
    assert ses.calls == []
    (sms,) = sns.calls
    assert sms["PhoneNumber"] == "+46701234567"
    # Short and plain, one SMS part, no link (the link is in the receipt e-mail).
    assert sms["Message"] == "Roma Södermalm: Beställning #1 bekräftad. Klar ca 14:50. Adress: Götgatan 1, Stockholm"
    assert len(sms["Message"]) <= 160 and "http" not in sms["Message"]


def test_start_without_a_nordic_phone_is_an_email(env, notify):
    module, ses, sns = notify
    created = place_and_pay(env)
    env["res"].Table(ORDER_TABLE).update_item(
        Key=o.order_key(LOC_A, stored(env, created["orderId"])["date"], created["orderId"]),
        UpdateExpression="SET customerPhone = :p", ExpressionAttributeValues={":p": "+15551234567"})
    set_status(env, created["orderId"], "preparing")
    module.handler({"Records": [order_record(stored(env, created["orderId"]))]}, None)
    assert sns.calls == []
    (mail,) = ses.calls
    assert mail["Message"]["Subject"]["Data"] == "Vi har börjat laga din beställning #1 - Roma Södermalm"


def test_same_notice_is_not_sent_twice(env, notify):
    module, ses, sns = notify
    item = stored(env, place_and_pay(env)["orderId"])
    module.handler({"Records": [order_record(item, old=item)]}, None)
    assert ses.calls == [] and sns.calls == []


def test_ready_pickup_is_an_sms_only(env, notify):
    module, ses, sns = notify
    created = place_and_pay(env)
    set_status(env, created["orderId"], "ready")
    item = stored(env, created["orderId"])
    module.handler({"Records": [order_record(item)]}, None)
    assert ses.calls == []
    (sms,) = sns.calls
    assert sms["Message"] == "Roma Södermalm: Beställning #1 är klar att hämta. Adress: Götgatan 1, Stockholm"


def test_ready_without_a_nordic_phone_falls_back_to_email(env, notify):
    module, ses, sns = notify
    created = place_and_pay(env, phone="+12025550123")
    set_status(env, created["orderId"], "ready")
    module.handler({"Records": [order_record(stored(env, created["orderId"]))]}, None)
    assert sns.calls == []
    (mail,) = ses.calls
    assert mail["Message"]["Subject"]["Data"] == "Din beställning #1 är klar att hämta - Roma Södermalm"


def test_cancelled_order_is_an_email_with_the_refund_never_an_sms(env, notify):
    module, ses, sns = notify
    created = place_and_pay(env)
    set_status(env, created["orderId"], "cancelled")
    module.handler({"Records": [order_record(stored(env, created["orderId"]))]}, None)
    (mail,) = ses.calls
    assert "250 kr" in mail["Message"]["Body"]["Text"]["Data"]
    assert sns.calls == []  # cancellation SMS are for table bookings only


def test_order_sms_stays_one_part_with_long_names():
    item = {"orderId": "a" * 32, "number": 123, "mode": "pickup", "language": "sv",
            "readyEstimate": "2026-09-08T12:50:00Z", "timezone": "Europe/Stockholm"}
    location = {"name": "Restaurang " + "X" * 80, "address": "Väldigt lång gatuadress " + "Y" * 150}
    for kind in ("order_started", "order_ready"):
        text = order_messages.sms(kind, item, {}, location)
        assert len(text) <= 160 and "#123" in text
    with pytest.raises(ValueError):
        order_messages.sms("order_cancelled", item, {}, location)


def test_table_order_sms_names_the_table_in_english(env, notify):
    module, ses, sns = notify
    created = place_and_pay(env, mode="table", tableCode=o.table_code(TENANT_A, LOC_A, "t5", "5"),
                            language="en", items=[{"menuItemId": "pizza", "quantity": 1}])
    module.handler({"Records": [order_record(stored(env, created["orderId"]))]}, None)
    assert sns.calls == []
    assert ses.calls[0]["Message"]["Subject"]["Data"] == "Receipt order #1 - Roma Södermalm"
    set_status(env, created["orderId"], "preparing")
    module.handler({"Records": [order_record(stored(env, created["orderId"]))]}, None)
    (sms,) = sns.calls
    assert sms["Message"] == "Roma Södermalm: Order #1 confirmed. Served at table 5."


def stored_before_start(item):
    return {**item, "notice": {"id": "paid", "type": "order_confirmed", "at": item["confirmedAt"]}}


def test_receipt_escapes_what_the_guest_typed():
    item = {"orderId": "a" * 32, "number": 3, "mode": "pickup", "customerName": "<b>Eve</b>",
            "lines": [{"name": "<script>", "quantity": 1, "total": 100, "unitPrice": 100, "vatRate": 6}],
            "totalAmount": 100, "vat": o.vat_breakdown([{"vatRate": 6, "total": 100}]),
            "confirmedAt": "2026-09-08T12:00:00Z", "readyEstimate": "2026-09-08T12:20:00Z"}
    _, _, html_body = order_messages.email("order_confirmed", item, {"name": "R"}, {}, None)
    assert "<script>" not in html_body and "<b>Eve" not in html_body


# --- welcome-offer discount codes ------------------------------------------------------------

def _with_codes(env, features=None):
    from shared import marketing_offer as offer
    env["monkeypatch"].setenv("MARKETING_TABLE_NAME", "test-marketing")
    env["res"].create_table(
        TableName="test-marketing",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST")
    env["res"].Table(TENANT_TABLE).put_item(Item=tenant_row(
        TENANT_A, name="Roma", slug="roma", stripe={"accountId": ACCOUNT, "chargesEnabled": True},
        features=features or {"ordering": True, "marketing": True}, notifications={"sms": False}))
    shared_tenant.reset_caches()
    table = env["res"].Table("test-marketing")

    def code(value="ABCD-EFGH", **extra):
        table.put_item(Item={**offer.code_key(TENANT_A, value), "code": value, "tenantId": TENANT_A,
                             "subscriberId": "s" * 24, "percent": 10, "minOrderOre": 0,
                             "expiresAt": "2026-10-01T00:00:00Z", "status": "issued", **extra})
    return table, code


def test_discount_code_lowers_every_line_and_is_held_for_the_order(env):
    from shared import marketing_offer as offer
    table, code = _with_codes(env)
    code()
    response = post_order(env, discountCode="abcd efgh", marketingOptIn=True)
    assert response["statusCode"] == 201, response
    body = body_of(response)
    item = stored(env, body["orderId"])
    # 125 kr -> 112.50, 35.50 -> 31.95 (per unit, VAT stays per rate)
    assert [(line["listPrice"], line["unitPrice"]) for line in item["lines"]] == [(12500, 11250), (3550, 3195)]
    assert body["totalAmount"] == item["totalAmount"] == 2 * 11250 + 3195
    assert item["discount"] == {"code": "ABCD-EFGH", "percent": 10, "amount": 2 * 1250 + 355, "subtotal": 28550}
    assert item["marketingOptIn"] is True
    assert env["stripe"].session["amount_total"] == item["totalAmount"]
    held = table.get_item(Key=offer.code_key(TENANT_A, "ABCD-EFGH"))["Item"]
    assert held["status"] == "reserved" and held["orderId"] == body["orderId"]
    # Another guest can't use it while this one is paying.
    again = post_order(env, discountCode="ABCD-EFGH", email="bertil@example.se", phone="070-765 43 21")
    assert again["statusCode"] == 409 and body_of(again) == {"error": "discount_used"}


def test_discount_code_refusals(env):
    table, code = _with_codes(env)
    code("EXPD-CODE", expiresAt="2026-09-01T00:00:00Z")
    code("MINI-MUMS", minOrderOre=50000)
    code("USED-CODE", status="redeemed")
    for value, error in (("NOPE-NOPE", "discount_invalid"), ("EXPD-CODE", "discount_expired"),
                         ("MINI-MUMS", "discount_min_order"), ("USED-CODE", "discount_used"), ("x", "discount_invalid")):
        response = post_order(env, discountCode=value)
        assert response["statusCode"] == 409 and body_of(response) == {"error": error}, value
    assert env["stripe"].calls == []


def test_abandoned_hold_frees_the_code(env):
    from shared import marketing_offer as offer
    table, code = _with_codes(env)
    code(status="reserved", orderId="other", reservedUntil="2026-09-08T11:00:00Z")
    assert post_order(env, discountCode="ABCD-EFGH")["statusCode"] == 201
    assert table.get_item(Key=offer.code_key(TENANT_A, "ABCD-EFGH"))["Item"]["orderId"] != "other"


def test_code_released_when_checkout_fails(env):
    from shared import marketing_offer as offer
    table, code = _with_codes(env)
    code()
    env["stripe"].fail = stripe_client.StripeError(400, {"message": "bad", "code": "x"})
    assert post_order(env, discountCode="ABCD-EFGH")["statusCode"] == 503
    held = table.get_item(Key=offer.code_key(TENANT_A, "ABCD-EFGH"))["Item"]
    assert held["status"] == "issued" and "orderId" not in held


def test_codes_need_marketing_in_the_plan(env):
    _, code = _with_codes(env, features={"ordering": True})
    code()
    response = post_order(env, discountCode="ABCD-EFGH")
    assert response["statusCode"] == 409 and body_of(response) == {"error": "discount_invalid"}


def test_code_is_personal(env):
    from shared import marketing_offer as offer
    _, code = _with_codes(env)
    code(ownerKey=offer.owner_key(TENANT_A, "someone@else.se"))
    response = post_order(env, discountCode="ABCD-EFGH")
    assert response["statusCode"] == 409 and body_of(response) == {"error": "discount_other_email"}
    code("WXYZ-WXYZ", ownerKey=offer.owner_key(TENANT_A, "anna@example.se"))
    assert post_order(env, discountCode="WXYZ-WXYZ", email="Anna+x@example.se")["statusCode"] == 201


def test_back_from_stripe_the_same_guest_gets_the_code_again(env):
    from shared import marketing_offer as offer
    table, code = _with_codes(env)
    code(ownerKey=offer.owner_key(TENANT_A, "anna@example.se"))
    first = body_of(post_order(env, discountCode="ABCD-EFGH"))
    # The guest pressed back in Stripe and orders again with the same code.
    second = post_order(env, discountCode="ABCD-EFGH")
    assert second["statusCode"] == 201, second
    assert any(c["path"].endswith("/expire") for c in env["stripe"].calls)  # the first checkout can't be paid now
    assert stored(env, first["orderId"])["status"] == "failed"
    held = table.get_item(Key=offer.code_key(TENANT_A, "ABCD-EFGH"))["Item"]
    assert held["orderId"] == body_of(second)["orderId"]


def test_code_held_by_a_paid_or_open_order_is_refused(env):
    table, code = _with_codes(env)
    code()
    first = body_of(post_order(env, discountCode="ABCD-EFGH"))
    # Someone else (no personal code: legacy) can't take it over while it can still be paid.
    other = post_order(env, discountCode="ABCD-EFGH", email="bertil@example.se", phone="070-765 43 21")
    assert other["statusCode"] == 409 and body_of(other) == {"error": "discount_used"}
    webhook(env, stripe_event("checkout.session.completed", paid_session(env, first["orderId"])))
    again = post_order(env, discountCode="ABCD-EFGH")
    assert again["statusCode"] == 409


def test_discount_never_goes_under_stripes_minimum(env):
    _, code = _with_codes(env)
    code(percent=50)
    env["res"].Table(MENU_TABLE).put_item(Item=menu_item(LOC_A, "mint", "Mint", "5"))
    response = post_order(env, discountCode="ABCD-EFGH", items=[{"menuItemId": "mint", "quantity": 1}])
    assert response["statusCode"] == 409 and body_of(response) == {"error": "discount_min_order"}

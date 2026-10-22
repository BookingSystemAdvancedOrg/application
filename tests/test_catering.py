"""Catering end to end: settings and discount tiers, the guest's request
(server-side pricing and rules), the owner's offer versions with capacity,
BankID signing (simulator provider), Stripe Checkout / invoicing, the
lifecycle (company invoice, expiry timers), the Stripe webhook and the
notifications. DynamoDB, S3 and Secrets Manager are moto; Stripe, SES, SNS
and EventBridge Scheduler are recording fakes."""

import hashlib
import hmac
import importlib.util
import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from boto3.dynamodb.types import TypeSerializer
from moto import mock_aws

from shared import bankid, catering as c, catering_stripe, manage_link, payment_domains, stripe_client
from shared import dynamo as shared_dynamo
from shared import tenant as shared_tenant
from tenant_support import (LOC_A, LOC_A2, REGION, STAFF_A_SUB, TENANT_A, TENANT_B, TENANT_TABLE, create_tables,
                            tenant_row, with_tenant_claims)

ROOT = Path(__file__).parents[1] / "functions"
LOCATION_TABLE = "test-location"
REQ_TABLE = "test-catering-requests"
HIST_TABLE = "test-catering-history"
TIERS_TABLE = "test-catering-tiers"
MENU_TABLE = "test-menu"
BUCKET = "test-catering-docs"
NOW = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
DAY = "2026-10-20"
ACCOUNT = "acct_roma"
WHSEC = "whsec_catering"
WEEK = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
SETTINGS = {"enabled": True, "minPortions": 10, "maxPortions": 300, "advanceDays": 5, "maxPortionsPerDay": 100,
            "pickup": True, "delivery": True, "deliveryArea": "Stockholm", "maxDistanceKm": 25,
            "priceWithinLimit": 395, "priceBeyondLimit": 795, "allowOutsideArea": True, "offerValidDays": 7,
            "paymentTermsDays": 30, "requestExpiryHours": 72, "terms": "Avbokning endast av restaurangen.",
            "notifyEmail": "agare@roma.se", "notifyPhone": "+46709999999"}
_ser = TypeSerializer()


def _table(res, name, *, hash_only=False):
    keys = [{"AttributeName": "PK", "KeyType": "HASH"}] + ([] if hash_only else [{"AttributeName": "SK", "KeyType": "RANGE"}])
    attrs = [{"AttributeName": "PK", "AttributeType": "S"}] + ([] if hash_only else [{"AttributeName": "SK", "AttributeType": "S"}])
    res.create_table(TableName=name, KeySchema=keys, AttributeDefinitions=attrs, BillingMode="PAY_PER_REQUEST")


def location_item(tenant_id, location_id, **extra):
    item = {"PK": f"TENANT#{tenant_id}", "SK": f"LOCATION#{location_id}", "tenantId": tenant_id,
            "locationId": location_id, "name": "Södermalm", "address": "Götgatan 1", "email": "soder@roma.se",
            "phoneNumber": "+46812345678", "timezone": "Europe/Stockholm",
            "businessHours": {d: [{"opensAt": "10:00", "closesAt": "22:00"}] for d in WEEK},
            "bookingDurationHours": Decimal("2"), "gracePeriodHours": Decimal("0.25"), "createdBy": "x",
            "createdAt": "2026-08-01T00:00:00Z", "cateringSettings": dict(SETTINGS)}
    item.update(extra)
    return {k: v for k, v in item.items() if v is not None}


def menu_item(item_id, name, price, **extra):
    return {"PK": f"LOCATION#{LOC_A}", "SK": f"MENU#{item_id}", "menuItemId": item_id, "name": name,
            "description": "", "price": Decimal(price), "category": "mains", "imageKey": "k", "active": True,
            **extra}


def load(name, module_name):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / name / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeStripe:
    def __init__(self):
        self.calls = []
        self.invoice_status = "open"
        self.session_status = "open"

    def __call__(self, method, path, params=None, *, account=None, idempotency_key=None):
        self.calls.append({"method": method, "path": path, "params": params or {}, "account": account,
                           "key": idempotency_key})
        if path == "/v1/tax_rates" and method == "GET":
            return {"data": []}
        if path == "/v1/tax_rates":
            return {"id": "txr_12"}
        if path == "/v1/coupons":
            return {"id": "co_1"}
        if path == "/v1/checkout/sessions":
            n = sum(1 for x in self.calls if x["path"] == "/v1/checkout/sessions")
            return {"id": f"cs_cat_{n}", "url": f"https://checkout.stripe.com/c/cs_cat_{n}",
                    "client_secret": f"cs_cat_{n}_secret_x"}
        if path.startswith("/v1/checkout/sessions/") and path.endswith("/expire"):
            if self.session_status != "open":
                raise stripe_client.StripeError(400, {"code": "checkout_session_not_open"})
            return {"id": path.split("/")[4], "status": "expired"}
        if path.startswith("/v1/checkout/sessions/"):
            return {"id": path.split("/")[4], "status": self.session_status}
        if path == "/v1/customers":
            return {"id": "cus_1"}
        if path == "/v1/invoices":
            return {"id": "in_1"}
        if path == "/v1/invoiceitems":
            return {"id": "ii"}
        if path.endswith("/finalize"):
            return {"id": "in_1", "number": "ROMA-0001", "status": "open",
                    "hosted_invoice_url": "https://invoice.stripe.com/i/in_1"}
        if path.endswith("/send") or path.endswith("/void") or path.endswith("/pay"):
            return {"id": "in_1"}
        if path.startswith("/v1/invoices/"):
            return {"id": "in_1", "status": self.invoice_status, "payment_intent": "pi_inv"}
        if path == "/v1/refunds":
            return {"id": "re_1"}
        if path == "/v1/payment_method_domains":
            return {"data": []} if method == "GET" else {"id": "pmd_1"}
        raise AssertionError(path)


class FakeClient:
    def __init__(self):
        self.calls = []

    def send_email(self, **kw):
        self.calls.append(kw)

    def publish(self, **kw):
        self.calls.append(kw)

    def create_schedule(self, **kw):
        self.calls.append(("create", kw))

    def delete_schedule(self, **kw):
        self.calls.append(("delete", kw))


@pytest.fixture
def env(monkeypatch):
    for key, value in {
        "ENVIRONMENT": "dev", "AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t", "AWS_SESSION_TOKEN": "t",
        "AWS_DEFAULT_REGION": REGION, "CATERING_REQUESTS_TABLE_NAME": REQ_TABLE,
        "CATERING_REQUEST_HISTORY_TABLE_NAME": HIST_TABLE, "CATERING_DISCOUNT_TIERS_TABLE_NAME": TIERS_TABLE,
        "MENU_TABLE_NAME": MENU_TABLE, "CATERING_DOCUMENTS_BUCKET_NAME": BUCKET, "USER_TABLE_NAME": "test-user",
        "CUSTOMER_SITE_URL": "https://dev123.cloudfront.net", "ADMIN_DASHBOARD_URL": "https://admin.example.se",
        "NO_REPLY_EMAIL_ADDRESS": "no-reply@mail.example.se", "SCHEDULE_GROUP_NAME": "dev-catering",
        "SCHEDULER_INVOKE_ROLE_ARN": "arn:aws:iam::1:role/sched", "SCHEDULER_DLQ_ARN": "arn:aws:sqs:x:1:dlq",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("REQUEST_GUARD_TABLE_NAME", raising=False)
    monkeypatch.delenv("TURNSTILE_REQUIRED", raising=False)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        c.reset_cache()
        bankid.reset_cache()
        manage_link.reset_cache()
        payment_domains.reset_cache()
        catering_stripe._tax_cache.clear()
        sm = boto3.client("secretsmanager", region_name=REGION)
        monkeypatch.setenv("LINK_SIGNING_KEY_SECRET_ARN", sm.create_secret(Name="cat-link", SecretString="c" * 64)["ARN"])
        monkeypatch.setenv("CATERING_LINK_SIGNING_KEY_SECRET_ARN", sm.create_secret(Name="cat-link2", SecretString="c" * 64)["ARN"])
        monkeypatch.setenv("RESERVATION_LINK_KEY_SECRET_ARN", sm.create_secret(Name="res-link", SecretString="r" * 64)["ARN"])
        monkeypatch.setenv("SIGNING_PROVIDER_SECRET_ARN",
                           sm.create_secret(Name="signing", SecretString=json.dumps({"provider": "simulator"}))["ARN"])
        monkeypatch.setenv("CATERING_STRIPE_WEBHOOK_SECRET_ARN", sm.create_secret(Name="whsec", SecretString=WHSEC)["ARN"])
        res = boto3.resource("dynamodb", region_name=REGION)
        for name in (REQ_TABLE, HIST_TABLE, TIERS_TABLE, MENU_TABLE):
            _table(res, name)
        boto3.client("s3", region_name=REGION).create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}, ObjectLockEnabledForBucket=True)
        create_tables(monkeypatch, location_table=LOCATION_TABLE)
        res.Table(TENANT_TABLE).put_item(Item=tenant_row(
            TENANT_A, name="Roma", slug="roma", legalName="Roma AB", orgNumber="556000-0000",
            stripe={"accountId": ACCOUNT, "chargesEnabled": True}))
        res.Table(TENANT_TABLE).put_item(Item=tenant_row(TENANT_B, slug="other"))
        res.Table(LOCATION_TABLE).put_item(Item=location_item(TENANT_A, LOC_A))
        res.Table(LOCATION_TABLE).put_item(Item=location_item(TENANT_A, LOC_A2))
        res.Table("test-user").put_item(Item={"PK": f"USER#{STAFF_A_SUB}", "SK": "PROFILE", "tenantId": TENANT_A,
                                              "locationId": LOC_A, "status": "active"})
        for item in (menu_item("lax", "Laxtallrik", "145"), menu_item("veg", "Vegetarisk buffé", "120"),
                     menu_item("vin", "Vin", "95", alcohol=True), menu_item("old", "Gammal", "10", active=False)):
            res.Table(MENU_TABLE).put_item(Item=item)
        tiers = res.Table(TIERS_TABLE)
        tiers.put_item(Item={"PK": f"LOCATION#{LOC_A}", "SK": "TIER#" + "1" * 32, "name": "Nivå 1",
                             "fromPortions": 30, "toPortions": 59, "discountPercent": 5, "active": True})
        tiers.put_item(Item={"PK": f"LOCATION#{LOC_A}", "SK": "TIER#" + "2" * 32, "name": "Nivå 2",
                             "fromPortions": 60, "toPortions": None, "discountPercent": 10, "active": True})
        fake = FakeStripe()
        monkeypatch.setattr(stripe_client, "request", fake)
        monkeypatch.setattr(c, "now_utc", lambda: NOW)
        mods = {name: load(f"catering-{name}", f"cat_{name.replace('-', '_')}") for name in
                ("settings", "discount-tiers", "requests", "offer", "customer", "stripe-webhook", "lifecycle")}
        sched = FakeClient()
        mods["lifecycle"]._clients["scheduler"] = sched
        yield SimpleNamespace(m=mods, res=res, stripe=fake, sched=sched, monkeypatch=monkeypatch)
        shared_dynamo._resource = None
        shared_dynamo._client = None
        shared_tenant.reset_caches()


# --- helpers ---------------------------------------------------------------------------------

def body_of(response):
    return json.loads(response["body"]) if response.get("body") else None


def request_body(**extra):
    data = {"items": [{"menuItemId": "lax", "quantity": 20}, {"menuItemId": "veg", "quantity": 15}],
            "customerType": "private", "name": "Anna Svensson", "email": "anna@example.se", "phone": "0701234567",
            "deliveryDate": DAY, "deliveryTime": "12:00", "fulfilment": "delivery", "deliveryZone": "within",
            "deliveryAddress": {"street": "Storgatan 1", "postalCode": "111 22", "city": "Stockholm"},
            "guests": 35, "acceptTerms": True}
    data.update(extra)
    return {k: v for k, v in data.items() if v is not None}


def create(env, **extra):
    event = {"routeKey": "POST /locations/{locationId}/catering/requests", "pathParameters": {"locationId": LOC_A},
             "body": json.dumps(request_body(**extra)), "requestContext": {"http": {"sourceIp": "203.0.113.9"}}}
    return env.m["requests"].handler(event, None)


def staff(env, method, suffix="", *, body=None, role="owner_user", tenant_id=TENANT_A, location_id=LOC_A,
          request_id=None, extra_params=None):
    path = "/locations/{locationId}/catering/requests/{requestId}" + suffix
    params = {"locationId": location_id, "requestId": request_id, **(extra_params or {})}
    event = {"routeKey": f"{method} {path}", "pathParameters": params}
    if body is not None:
        event["body"] = json.dumps(body)
    return env.m["offer"].handler(with_tenant_claims(event, tenant_id, role), None)


def guest(env, method, suffix, request_id, token, body=None):
    event = {"routeKey": f"{method} /locations/{{locationId}}/catering/requests/{{requestId}}/customer{suffix}",
             "pathParameters": {"locationId": LOC_A, "requestId": request_id},
             "headers": {"X-Order-Token": token}, "requestContext": {"http": {"sourceIp": "203.0.113.9"}}}
    if body is not None:
        event["body"] = json.dumps(body)
    return env.m["customer"].handler(event, None)


OFFER = {"lines": [{"name": "Laxtallrik", "unitPrice": 140, "quantity": 20, "menuItemId": "lax"},
                   {"name": "Vegetarisk buffé", "unitPrice": 120, "quantity": 15, "menuItemId": "veg"},
                   {"name": "Serveringspersonal", "unitPrice": 450, "quantity": 2}],
         "discountPercent": 5, "deliveryFee": 395, "message": "Välkomna!"}


def sent_offer(env, **extra):
    created = body_of(create(env, **extra))
    rid = created["requestId"]
    assert staff(env, "POST", "/offer/send", body=OFFER, request_id=rid)["statusCode"] == 200
    return rid, created["token"]


def signed(env, **extra):
    rid, token = sent_offer(env, **extra)
    assert guest(env, "POST", "/sign", rid, token)["statusCode"] == 200
    guest(env, "POST", "/sign/status", rid, token)
    done = body_of(guest(env, "POST", "/sign/status", rid, token))
    assert done["status"] == "complete", done
    return rid, token


def stream_record(item, old=None, arn_table=REQ_TABLE):
    data = {"NewImage": {k: _ser.serialize(v) for k, v in item.items()}, "SequenceNumber": "1"}
    if old is not None:
        data["OldImage"] = {k: _ser.serialize(v) for k, v in old.items()}
    return {"eventName": "MODIFY", "eventSourceARN": f"arn:aws:dynamodb:{REGION}:1:table/{arn_table}/stream/x",
            "dynamodb": data}


def webhook(env, kind, obj):
    payload = json.dumps({"id": f"evt_{kind}_{time.time_ns()}", "type": kind, "account": ACCOUNT, "livemode": False,
                          "data": {"object": obj}})
    ts = int(time.time())
    sig = hmac.new(WHSEC.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    return env.m["stripe-webhook"].handler({"body": payload, "headers": {"Stripe-Signature": f"t={ts},v1={sig}"},
                                            "requestContext": {"http": {"method": "POST"}}}, None)


# --- settings and tiers -------------------------------------------------------------------------

def test_settings_public_and_owner_update(env):
    get = env.m["settings"].handler({"routeKey": "GET /locations/{locationId}/catering/settings",
                                     "pathParameters": {"locationId": LOC_A}}, None)
    body = body_of(get)
    assert body["enabled"] is True and body["minPortions"] == 10 and "notifyPhone" not in body
    put = {"routeKey": "PUT /locations/{locationId}/catering/settings", "pathParameters": {"locationId": LOC_A},
           "body": json.dumps({"minPortions": 20, "notifyPhone": "070-111 22 33"})}
    saved = body_of(env.m["settings"].handler(with_tenant_claims(put), None))
    assert saved["minPortions"] == 20 and saved["notifyPhone"] == "+46701112233" and saved["maxPortions"] == 300
    staff_put = with_tenant_claims(dict(put), role="staff_user")
    assert env.m["settings"].handler(staff_put, None)["statusCode"] == 403
    bad = dict(put, body=json.dumps({"minPortions": 500, "maxPortions": 100}))
    assert env.m["settings"].handler(with_tenant_claims(bad), None)["statusCode"] == 400


def test_tiers_crud(env):
    def call(method, suffix="", body=None, tier=None):
        path = "/locations/{locationId}/catering/discount-tiers" + suffix
        ev = {"routeKey": f"{method} {path}", "pathParameters": {"locationId": LOC_A, "tierId": tier}}
        if body is not None:
            ev["body"] = json.dumps(body)
        return env.m["discount-tiers"].handler(with_tenant_claims(ev) if method != "GET" else ev, None)
    created = body_of(call("POST", body={"name": "Stor", "fromPortions": 100, "toPortions": None, "discountPercent": 15}))
    assert created["discountPercent"] == 15
    tiers = body_of(call("GET"))["tiers"]
    assert [t["name"] for t in tiers] == ["Nivå 1", "Nivå 2", "Stor"]
    updated = call("PUT", "/{tierId}", {"name": "Stor", "fromPortions": 100, "discountPercent": 12, "active": False},
                   tier=created["tierId"])
    assert body_of(updated)["active"] is False
    assert call("DELETE", "/{tierId}", tier=created["tierId"])["statusCode"] == 204
    assert call("POST", body={"name": "x", "fromPortions": 10, "toPortions": 5})["statusCode"] == 400


# --- the guest's request ---------------------------------------------------------------------------

def test_request_is_priced_on_the_server(env):
    response = create(env)
    assert response["statusCode"] == 201, response
    body = body_of(response)
    priced = body["request"]
    # 20 x 145 + 15 x 120 = 4700 kr; 35 portions -> Nivå 1 (5 %) = 235 kr; delivery 395 kr
    assert priced["subtotal"] == 470000 and priced["discount"] == 23500 and priced["deliveryFee"] == 39500
    assert priced["total"] == 486000 and priced["tierName"] == "Nivå 1" and priced["portions"] == 35
    item = c.load(LOC_A, body["requestId"])
    assert item["status"] == "requested" and item["notice"]["type"] == "catering_request_received"
    assert item["customerPhone"] == "+46701234567" and item["expiresAt"] == "2026-10-04T10:00:00Z"
    assert c.token_matches(item, body["token"])


@pytest.mark.parametrize("change, status, error", [
    ({"deliveryDate": "2026-10-04"}, 409, "catering_too_soon"),
    ({"items": [{"menuItemId": "lax", "quantity": 5}]}, 400, "at least 10 portions"),
    ({"items": [{"menuItemId": "vin", "quantity": 20}]}, 400, "menu_item_unavailable"),
    ({"items": [{"menuItemId": "old", "quantity": 20}]}, 400, "menu_item_unavailable"),
    ({"customerType": "company"}, 400, "orgNumber is required"),
    ({"fulfilment": "delivery", "deliveryAddress": None}, 400, "deliveryAddress needs street, postalCode and city"),
    ({"acceptTerms": None}, 400, "acceptTerms must be true"),
    ({"items": [{"menuItemId": "lax", "quantity": 120}]}, 409, "catering_day_full"),
])
def test_request_rules(env, change, status, error):
    response = create(env, **change)
    assert response["statusCode"] == status and error in body_of(response)["error"]


def test_company_request_and_pickup(env):
    body = body_of(create(env, customerType="company", companyName="Acme AB", orgNumber="5560001234",
                          invoiceReference="Kalle", fulfilment="pickup", deliveryAddress=None))
    item = c.load(LOC_A, body["requestId"])
    assert item["orgNumber"] == "556000-1234" and item["request"]["deliveryFee"] == 0
    assert "deliveryAddress" not in item


def test_catering_off_or_not_in_plan(env):
    env.res.Table(LOCATION_TABLE).put_item(Item=location_item(TENANT_A, LOC_A, cateringSettings={**SETTINGS, "enabled": False}))
    assert body_of(create(env)) == {"error": "catering_disabled"}
    env.res.Table(TENANT_TABLE).put_item(Item=tenant_row(TENANT_A, slug="roma", features={"reservations": True}))
    shared_tenant.reset_caches()
    assert create(env)["statusCode"] == 404


def test_staff_list(env):
    create(env)
    event = {"routeKey": "GET /locations/{locationId}/catering/requests", "pathParameters": {"locationId": LOC_A}}
    rows = body_of(env.m["requests"].handler(with_tenant_claims(event), None))["requests"]
    assert rows[0]["status"] == "requested" and rows[0]["portions"] == 35 and rows[0]["total"] == 486000
    assert env.m["requests"].handler(with_tenant_claims(dict(event), TENANT_B), None)["statusCode"] == 404


# --- the owner's offer -----------------------------------------------------------------------------

def test_send_offer_versions_and_capacity(env):
    rid = body_of(create(env))["requestId"]
    view = body_of(staff(env, "GET", request_id=rid))
    assert view["status"] == "requested" and view["capacityLeft"] == 100
    assert staff(env, "PUT", "/offer", body=OFFER, request_id=rid)["statusCode"] == 200
    assert c.load(LOC_A, rid)["draft"]["totals"]["total"] == 20 * 14000 + 15 * 12000 + 2 * 45000 \
        - round((20 * 14000 + 15 * 12000 + 2 * 45000) * 0.05) + 39500
    sent = body_of(staff(env, "POST", "/offer/send", request_id=rid))
    assert sent["status"] == "offer_sent" and sent["currentOfferVersion"] == 1 and "draft" not in sent
    assert c.capacity_left(LOC_A, c.settings_of(location_item(TENANT_A, LOC_A)), DAY) == 65
    item = c.load(LOC_A, rid)
    assert item["notice"]["type"] == "catering_offer_sent" and len(item["offer"]["documentHash"]) == 64
    smaller = {**OFFER, "lines": [{"name": "Laxtallrik", "unitPrice": 140, "quantity": 20, "menuItemId": "lax"}]}
    assert body_of(staff(env, "POST", "/offer/send", body=smaller, request_id=rid))["currentOfferVersion"] == 2
    assert c.capacity_left(LOC_A, c.settings_of(location_item(TENANT_A, LOC_A)), DAY) == 80
    view = body_of(staff(env, "GET", request_id=rid))
    assert [o["version"] for o in view["offers"]] == [1, 2]
    assert [h["action"] for h in view["history"]][:3] == ["offer_sent", "offer_sent", "requested"]


def test_offer_capacity_full_and_validation(env):
    rid = body_of(create(env))["requestId"]
    big = {**OFFER, "lines": [{"name": "Lax", "unitPrice": 140, "quantity": 101, "menuItemId": "lax"}]}
    assert body_of(staff(env, "POST", "/offer/send", body=big, request_id=rid)) == {"error": "capacity_full"}
    pricey = {**OFFER, "lines": [{"name": "Lax", "unitPrice": 10000.01, "quantity": 1}]}
    assert body_of(staff(env, "PUT", "/offer", body=pricey, request_id=rid)) == {"error": "price may be at most 10000 kr"}
    at_limit = {**OFFER, "lines": [{"name": "Tårta", "unitPrice": 10000, "quantity": 1}]}
    assert staff(env, "PUT", "/offer", body=at_limit, request_id=rid)["statusCode"] == 200
    late = {**OFFER, "validUntil": "2026-10-25"}
    assert "after the delivery date" in body_of(staff(env, "POST", "/offer/send", body=late, request_id=rid))["error"]
    assert staff(env, "POST", "/offer/send", body=OFFER, request_id=rid, role="staff_user")["statusCode"] == 403


def test_decline_releases_capacity_and_tells_the_guest(env):
    rid, _ = sent_offer(env)
    body = body_of(staff(env, "POST", "/decline", body={"reason": "Fullbokat"}, request_id=rid))
    assert body["status"] == "declined"
    assert c.load(LOC_A, rid)["notice"]["type"] == "catering_declined"
    assert c.capacity_left(LOC_A, c.settings_of(location_item(TENANT_A, LOC_A)), DAY) == 100


def test_other_tenant_cannot_see_the_request(env):
    rid = body_of(create(env))["requestId"]
    assert staff(env, "GET", request_id=rid, tenant_id=TENANT_B)["statusCode"] == 404


# --- the guest's side ------------------------------------------------------------------------------

def test_guest_view_needs_the_token(env):
    rid, token = sent_offer(env)
    body = body_of(guest(env, "GET", "", rid, token))
    assert body["status"] == "offer_sent" and body["canSign"] is True and body["offer"]["version"] == 1
    assert body["offer"]["totals"]["total"] > 0 and "customerPhone" not in body
    assert guest(env, "GET", "", rid, "wrong")["statusCode"] == 404


def test_bankid_signing_with_the_simulator(env):
    rid, token = sent_offer(env)
    start = body_of(guest(env, "POST", "/sign", rid, token))
    assert start["status"] == "pending" and start["qrData"].startswith("bankid.sim-")
    assert body_of(guest(env, "POST", "/sign/status", rid, token))["status"] == "pending"
    done = body_of(guest(env, "POST", "/sign/status", rid, token))
    assert done["status"] == "complete" and done["request"]["status"] == "signed"
    assert done["request"]["canPay"] is True and done["request"]["signedBy"] == "Test Testsson"
    item = c.load(LOC_A, rid)
    assert item["signature"]["personalNumberMasked"] == "19000000-****" and item["notice"]["type"] == "catering_signed"
    evidence = boto3.client("s3", region_name=REGION).get_object(Bucket=BUCKET, Key=item["signature"]["evidenceKey"])
    data = json.loads(evidence["Body"].read())
    assert data["documentHash"] == item["offer"]["documentHash"] and data["signer"]["personalNumber"] == "190000000000"
    assert c.document_hash(data["document"]) == data["documentHash"]


def test_simulator_is_refused_in_prod(env):
    env.monkeypatch.setenv("ENVIRONMENT", "prod")
    bankid.reset_cache()
    rid, token = sent_offer(env)
    assert body_of(guest(env, "POST", "/sign", rid, token)) == {"error": "signing_unavailable"}


def test_a_new_offer_version_during_signing_is_not_signed(env):
    rid, token = sent_offer(env)
    guest(env, "POST", "/sign", rid, token)
    staff(env, "POST", "/offer/send", body=OFFER, request_id=rid)
    guest(env, "POST", "/sign/status", rid, token)
    assert body_of(guest(env, "POST", "/sign/status", rid, token))["status"] == "none"
    assert c.load(LOC_A, rid)["status"] == "offer_sent"


def test_bankid_qr_code_format():
    order = {"qrStartToken": "67df3917-fa0d-44e5-b327-edcc928297f8", "qrStartSecret": "d28db9a7-4cde-429e-a983-359be676944c",
             "startedAt": 1000}
    expected = hmac.new(b"d28db9a7-4cde-429e-a983-359be676944c", b"0", hashlib.sha256).hexdigest()
    assert bankid.qr_data(order, now=1000) == f"bankid.67df3917-fa0d-44e5-b327-edcc928297f8.0.{expected}"
    assert bankid.qr_data(order, now=1001.4).split(".")[2] == "1"


def test_private_guest_pays_with_checkout(env):
    rid, token = signed(env)
    body = body_of(guest(env, "POST", "/checkout", rid, token))
    assert body["checkoutUrl"].startswith("https://checkout.stripe.com/")
    call = next(x for x in env.stripe.calls if x["path"] == "/v1/checkout/sessions")
    assert call["account"] == ACCOUNT and call["key"] == f"checkout-{rid}-1"
    params = call["params"]
    assert params["discounts"] == [{"coupon": "co_1"}] and params["invoice_creation"]["enabled"] is True
    assert params["wallet_options"] == {"link": {"display": "never"}}
    assert [li["quantity"] for li in params["line_items"]] == [20, 15, 2, 1]
    assert params["line_items"][0]["tax_rates"] == ["txr_12"]
    assert params["success_url"].endswith(f"/catering/{LOC_A}/{rid}?restaurang=roma&betalning=klar")
    session = {"id": "cs_cat_1", "payment_status": "paid", "payment_intent": "pi_cat",
               "metadata": {"locationId": LOC_A, "requestId": rid}}
    assert body_of(webhook(env, "checkout.session.completed", session))["result"] == "confirmed"
    item = c.load(LOC_A, rid)
    assert item["status"] == "confirmed" and item["paid"] is True and item["notice"]["type"] == "catering_confirmed"
    assert body_of(webhook(env, "checkout.session.completed", session))["result"] == "already_settled"


def test_failed_payment_keeps_the_signature(env):
    rid, token = signed(env)
    guest(env, "POST", "/checkout", rid, token)
    expired = {"id": "cs_cat_1", "payment_status": "unpaid", "metadata": {"locationId": LOC_A, "requestId": rid}}
    webhook(env, "checkout.session.expired", expired)
    assert c.load(LOC_A, rid)["status"] == "signed"
    guest(env, "POST", "/checkout", rid, token)
    keys = [x["key"] for x in env.stripe.calls if x["path"] == "/v1/checkout/sessions"]
    assert keys == [f"checkout-{rid}-1", f"checkout-{rid}-2"]


def test_private_guest_pays_on_the_site_with_the_payment_element(env):
    rid, token = signed(env)
    body = body_of(guest(env, "POST", "/checkout", rid, token, {"embedded": True}))
    assert body == {"clientSecret": "cs_cat_1_secret_x", "stripeAccount": ACCOUNT}
    params = next(x for x in env.stripe.calls if x["path"] == "/v1/checkout/sessions")["params"]
    assert params["ui_mode"] == "elements" and params["invoice_creation"]["enabled"] is True
    assert params["return_url"].endswith(f"/catering/{LOC_A}/{rid}?restaurang=roma&betalning=klar")
    assert not {"success_url", "cancel_url", "locale"} & set(params)
    (domain,) = [x for x in env.stripe.calls if x["path"] == "/v1/payment_method_domains" and x["method"] == "POST"]
    assert domain["account"] == ACCOUNT and domain["params"] == {"domain_name": "dev123.cloudfront.net"}
    session = {"id": "cs_cat_1", "payment_status": "paid", "payment_intent": "pi_cat",
               "metadata": {"locationId": LOC_A, "requestId": rid}}
    assert body_of(webhook(env, "checkout.session.completed", session))["result"] == "confirmed"


def test_a_new_attempt_expires_the_open_session_first(env):
    rid, token = signed(env)
    guest(env, "POST", "/checkout", rid, token, {"embedded": True})
    body = body_of(guest(env, "POST", "/checkout", rid, token, {"embedded": True}))
    assert body["clientSecret"] == "cs_cat_2_secret_x"
    paths = [x["path"] for x in env.stripe.calls if "/checkout/sessions" in x["path"]]
    assert paths == ["/v1/checkout/sessions", "/v1/checkout/sessions/cs_cat_1/expire", "/v1/checkout/sessions"]
    assert c.load(LOC_A, rid)["checkoutSessionId"] == "cs_cat_2"
    # The first session is no longer the request's: its events are ignored.
    late = {"id": "cs_cat_1", "payment_status": "paid", "metadata": {"locationId": LOC_A, "requestId": rid}}
    assert body_of(webhook(env, "checkout.session.completed", late))["result"] != "confirmed"


def test_no_new_attempt_while_a_completed_session_settles(env):
    rid, token = signed(env)
    guest(env, "POST", "/checkout", rid, token, {"embedded": True})
    env.stripe.session_status = "complete"
    response = guest(env, "POST", "/checkout", rid, token, {"embedded": True})
    assert response["statusCode"] == 409 and body_of(response) == {"error": "payment_in_progress"}


def test_checkout_rejects_unknown_fields(env):
    rid, token = signed(env)
    response = guest(env, "POST", "/checkout", rid, token, {"embedded": "ja"})
    assert response["statusCode"] == 400


def test_guest_asks_for_changes_and_owner_sends_a_new_version(env):
    rid, token = sent_offer(env)
    body = body_of(guest(env, "POST", "/change", rid, token, {"message": "Kan ni lägga till dessert?"}))
    assert body["status"] == "change_requested" and body["canSign"] is False
    assert c.load(LOC_A, rid)["notice"]["type"] == "catering_change_requested"
    assert body_of(staff(env, "POST", "/offer/send", body=OFFER, request_id=rid))["currentOfferVersion"] == 2
    assert body_of(guest(env, "GET", "", rid, token))["canSign"] is True


def test_guest_rejects_the_offer(env):
    rid, token = sent_offer(env)
    assert body_of(guest(env, "POST", "/reject", rid, token, {"reason": "För dyrt"}))["status"] == "rejected"
    assert c.capacity_left(LOC_A, c.settings_of(location_item(TENANT_A, LOC_A)), DAY) == 100
    assert body_of(guest(env, "POST", "/sign", rid, token)) == {"error": "not_signable"}


# --- company: lifecycle invoices ------------------------------------------------------------------------

COMPANY = dict(customerType="company", companyName="Acme AB", orgNumber="556000-1234", invoiceReference="Kalle")


def test_company_is_invoiced_by_the_lifecycle(env):
    rid, token = signed(env, **COMPANY)
    item = c.load(LOC_A, rid)
    result = env.m["lifecycle"].handler({"Records": [stream_record(item, old={**item, "status": "offer_sent"})]},
                                        SimpleNamespace(invoked_function_arn="arn:fn"))
    assert result == {"batchItemFailures": []}
    item = c.load(LOC_A, rid)
    assert item["status"] == "confirmed" and item["stripeInvoiceId"] == "in_1" and item["invoiceNumber"] == "ROMA-0001"
    invoice = next(x for x in env.stripe.calls if x["path"] == "/v1/invoices")
    assert invoice["params"]["days_until_due"] == 30 and invoice["params"]["collection_method"] == "send_invoice"
    assert {"name": "Org.nr", "value": "556000-1234"} in invoice["params"]["custom_fields"]
    items = [x["params"] for x in env.stripe.calls if x["path"] == "/v1/invoiceitems"]
    assert [i["unit_amount"] for i in items][-1] < 0 and all(i["tax_rates"] == ["txr_12"] for i in items)
    assert body_of(guest(env, "GET", "", rid, token))["invoiceUrl"] == "https://invoice.stripe.com/i/in_1"
    # paid by Bankgiro: the owner marks it, Stripe's invoice.paid settles it
    ev = {"routeKey": "POST /locations/{locationId}/catering/invoices/{invoiceId}/mark-paid",
          "pathParameters": {"locationId": LOC_A, "invoiceId": "in_1"}}
    assert env.m["offer"].handler(with_tenant_claims(ev), None)["statusCode"] == 202
    staff(env, "POST", "/delivered", request_id=rid)
    assert c.load(LOC_A, rid)["status"] == "delivered"
    paid = {"id": "in_1", "status": "paid", "metadata": {"locationId": LOC_A, "requestId": rid}}
    assert body_of(webhook(env, "invoice.paid", paid))["result"] == "paid"
    item = c.load(LOC_A, rid)
    assert item["status"] == "closed" and item["paid"] is True


def test_lifecycle_timers_and_expiry(env):
    rid = body_of(create(env))["requestId"]
    item = c.load(LOC_A, rid)
    ctx = SimpleNamespace(invoked_function_arn="arn:fn:live")
    env.m["lifecycle"].handler({"Records": [stream_record(item, old={})]}, ctx)
    (kind, call), = [x for x in env.sched.calls if x[0] == "create"]
    assert call["Name"] == f"request-expiry-{rid}" and call["ScheduleExpression"] == "at(2026-10-04T10:00:00)"
    assert json.loads(call["Target"]["Input"]) == {"action": "expire", "kind": "request", "locationId": LOC_A,
                                                    "requestId": rid}
    assert env.m["lifecycle"].handler({"action": "expire", "kind": "offer", "locationId": LOC_A, "requestId": rid},
                                      ctx) == {"result": "moved_on"}
    assert env.m["lifecycle"].handler({"action": "expire", "kind": "request", "locationId": LOC_A, "requestId": rid},
                                      ctx) == {"result": "expired"}
    assert c.load(LOC_A, rid)["notice"]["type"] == "catering_expired"


def test_offer_expiry_releases_capacity(env):
    rid, _ = sent_offer(env)
    ctx = SimpleNamespace(invoked_function_arn="arn:fn")
    item = c.load(LOC_A, rid)
    env.m["lifecycle"].handler({"Records": [stream_record(item, old={**item, "status": "requested"})]}, ctx)
    created = [x[1] for x in env.sched.calls if x[0] == "create"]
    assert created[-1]["Name"] == f"offer-expiry-{rid}" and created[-1]["ScheduleExpression"] == "at(2026-10-08T21:59:00)"
    assert env.m["lifecycle"].handler({"action": "expire", "kind": "offer", "locationId": LOC_A, "requestId": rid},
                                      ctx) == {"result": "expired"}
    assert c.capacity_left(LOC_A, c.settings_of(location_item(TENANT_A, LOC_A)), DAY) == 100


# --- cancellation ----------------------------------------------------------------------------------------

def test_cancel_refunds_a_paid_private_order(env):
    rid, token = signed(env)
    guest(env, "POST", "/checkout", rid, token)
    webhook(env, "checkout.session.completed", {"id": "cs_cat_1", "payment_status": "paid", "payment_intent": "pi_cat",
                                                "metadata": {"locationId": LOC_A, "requestId": rid}})
    assert staff(env, "POST", "/cancel", body={}, request_id=rid)["statusCode"] == 400
    body = body_of(staff(env, "POST", "/cancel", body={"reason": "Sjukdom"}, request_id=rid))
    assert body["status"] == "cancelled" and body["refundNote"] == "refunded"
    refund = next(x for x in env.stripe.calls if x["path"] == "/v1/refunds")
    assert refund["params"]["payment_intent"] == "pi_cat" and refund["account"] == ACCOUNT


def test_cancel_voids_an_open_company_invoice(env):
    rid, _ = signed(env, **COMPANY)
    item = c.load(LOC_A, rid)
    env.m["lifecycle"].handler({"Records": [stream_record(item, old={**item, "status": "offer_sent"})]},
                               SimpleNamespace(invoked_function_arn="arn:fn"))
    body = body_of(staff(env, "POST", "/cancel", body={"reason": "Force majeure"}, request_id=rid))
    assert body["refundNote"] == "invoice_voided"
    assert any(x["path"] == "/v1/invoices/in_1/void" for x in env.stripe.calls)


# --- notifications ----------------------------------------------------------------------------------------

@pytest.fixture
def notify(env):
    module = load("notification", "cat_notify")
    ses, sns = FakeClient(), FakeClient()
    module._clients.update(ses=ses, sns=sns)
    module._now = lambda: NOW
    env.monkeypatch.setenv("CATERING_REQUESTS_TABLE_NAME", REQ_TABLE)
    return module, ses, sns


def test_owner_gets_sms_and_email_and_guest_a_confirmation(env, notify):
    module, ses, sns = notify
    rid = body_of(create(env))["requestId"]
    item = c.load(LOC_A, rid)
    assert module.handler({"Records": [stream_record(item, old={})]}, None) == {"batchItemFailures": []}
    guest_mail, owner_mail = ses.calls
    assert guest_mail["Destination"] == {"ToAddresses": ["anna@example.se"]}
    assert f"/catering/{LOC_A}/{rid}?restaurang=roma#t=" in guest_mail["Message"]["Body"]["Text"]["Data"]
    assert owner_mail["Destination"] == {"ToAddresses": ["agare@roma.se"]}
    assert f"https://admin.example.se/catering/{LOC_A}/{rid}" in owner_mail["Message"]["Body"]["Text"]["Data"]
    (sms,) = sns.calls
    assert sms["PhoneNumber"] == "+46709999999" and "Ny cateringförfrågan från Anna Svensson" in sms["Message"]


def test_offer_reaches_the_guest_by_email_and_sms(env, notify):
    module, ses, sns = notify
    rid, _ = sent_offer(env)
    item = c.load(LOC_A, rid)
    module.handler({"Records": [stream_record(item, old={**item, "notice": {"id": "old"}})]}, None)
    (mail,) = ses.calls
    assert mail["Message"]["Subject"]["Data"] == "Din cateringoffert från Roma Södermalm"
    (sms,) = sns.calls
    assert sms["PhoneNumber"] == "+46701234567" and "Signera med BankID" in sms["Message"]


def test_bankid_rp_api_calls(env, monkeypatch):
    sm = boto3.client("secretsmanager", region_name=REGION)
    sm.put_secret_value(SecretId="signing",
                        SecretString=json.dumps({"provider": "bankid", "environment": "test",
                                                 "certPem": "x", "keyPem": "y", "caPem": "z"}))
    bankid.reset_cache()
    calls = []

    def fake_call(path, body):
        calls.append((path, body))
        if path == "sign":
            return {"orderRef": "ref-1", "autoStartToken": "ast", "qrStartToken": "qst", "qrStartSecret": "qss"}
        return {"status": "complete", "completionData": {
            "user": {"personalNumber": "199001011234", "name": "Anna Svensson"},
            "signature": "PFNpZz4=", "ocspResponse": "MII=", "bankIdIssueDate": "2024-01-01"}}

    monkeypatch.setattr(bankid, "_call", fake_call)
    rid, token = sent_offer(env)
    start = body_of(guest(env, "POST", "/sign", rid, token))
    assert start["autoStartToken"] == "ast" and start["qrData"].startswith("bankid.qst.")
    path, body = calls[0]
    assert path == "sign" and body["endUserIp"] == "203.0.113.9" and body["userVisibleDataFormat"] == "simpleMarkdownV1"
    import base64
    visible = base64.b64decode(body["userVisibleData"]).decode()
    assert "Säljare: Roma AB" in visible and "Offertversion: 1" in visible
    item = c.load(LOC_A, rid)
    assert base64.b64decode(body["userNonVisibleData"]).decode() == f"sha256:{item['offer']['documentHash']}"
    done = body_of(guest(env, "POST", "/sign/status", rid, token))
    assert done["status"] == "complete" and done["request"]["signedBy"] == "Anna Svensson"
    assert c.load(LOC_A, rid)["signature"]["personalNumberMasked"] == "19900101-****"


def test_catering_opt_in_subscribes_and_counts_for_marketing_restaurants(env):
    from shared import marketing as mk
    env.monkeypatch.setenv("MARKETING_TABLE_NAME", "test-marketing")
    env.res.create_table(
        TableName="test-marketing",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST")
    table = env.res.Table("test-marketing")
    sid = mk.subscriber_id(TENANT_A, "anna@example.se")
    # Without marketing in the plan nothing is written.
    assert create(env, marketingOptIn=True)["statusCode"] == 201
    assert table.scan()["Items"] == []
    env.res.Table(TENANT_TABLE).put_item(Item=tenant_row(
        TENANT_A, name="Roma", slug="roma", stripe={"accountId": ACCOUNT, "chargesEnabled": True},
        features={"catering": True, "marketing": True}))
    shared_tenant.reset_caches()
    assert create(env, marketingOptIn=True, email="anna@example.se")["statusCode"] == 201
    sub = table.get_item(Key=mk.sub_key(TENANT_A, sid))["Item"]
    assert sub["status"] == "subscribed" and sub["consentSource"] == "catering"
    stats = [i for i in table.scan()["Items"] if i["SK"].startswith("DAY#")]
    assert len(stats) == 1 and stats[0]["catering"] == 1
    assert create(env, marketingOptIn="yes")["statusCode"] == 400

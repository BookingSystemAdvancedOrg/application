"""Gift cards (feature E, ADR-E, CONTRACTS.md 6.E, 9.2): routes E1-E13, the
stripe.event / checkout.giftcard / kassa.giftcard / accounting.events /
privacy / purge hooks, the nightly expiry and the two-tenant isolation
suite. Stripe is a fake; nothing leaves the test."""

import importlib.util
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from shared import accounting, events
from shared import stripe_client as stripe
from shared import tenant as shared_tenant
from shared.features import gift_cards as gc
from tenant_support import (LOC_A, LOC_A2, LOC_B, REGION, TENANT_A, TENANT_B, TENANT_TABLE, features_on, location_row,
                            seed_tenancy, tenant_row, with_tenant_claims)

ROOT = Path(__file__).parents[1] / "functions"
TABLE = "test-gift-cards"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
ACCT_A, ACCT_B = "acct_aaaaaaaaaaaaaa", "acct_bbbbbbbbbbbbbb"
P1, P2 = "01PURCHASEAAAAAAAAAAAAAAAA", "01PURCHASEBBBBBBBBBBBBBBBB"


def load(name):
    spec = importlib.util.spec_from_file_location(f"gc_{name.replace('-', '_')}", ROOT / name / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeStripe:
    def __init__(self):
        self.calls = []
        self.fail = False

    def __call__(self, method, path, params=None, *, account=None, idempotency_key=None):
        self.calls.append({"method": method, "path": path, "params": params, "account": account,
                           "key": idempotency_key})
        if self.fail:
            raise stripe.StripeUnavailable("down")
        if path == "/v1/payment_intents":
            pid = f"pi_{idempotency_key[-10:]}"
            return {"id": pid, "client_secret": f"{pid}_secret", "amount": params["amount"]}
        if method == "GET" and path.startswith("/v1/payment_intents/"):
            pid = path.rsplit("/", 1)[-1]
            return {"id": pid, "client_secret": f"{pid}_secret", "status": "requires_payment_method"}
        return {"id": f"re_{len(self.calls)}", "status": "succeeded"}


class FakeLambda:
    def __init__(self):
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(json.loads(kwargs["Payload"]))
        return {"StatusCode": 202}


@pytest.fixture
def env(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
        monkeypatch.setenv("ENVIRONMENT", "dev")
        monkeypatch.setenv("GIFT_CARDS_TABLE_NAME", TABLE)
        monkeypatch.setenv("NOTIFICATION_FUNCTION_NAME", "dev-notification:live")
        monkeypatch.setenv("USER_TABLE_NAME", "test-user")
        secret = boto3.client("secretsmanager", region_name=REGION).create_secret(Name="pepper", SecretString="p" * 48)
        monkeypatch.setenv("GIFT_CARD_PEPPER_SECRET_ARN", secret["ARN"])
        on = features_on("giftCards", "kassa")
        seed_tenancy(monkeypatch,
                     tenant_a=tenant_row(TENANT_A, features=on, stripe={"accountId": ACCT_A, "chargesEnabled": True}),
                     tenant_b=tenant_row(TENANT_B, features=on, stripe={"accountId": ACCT_B, "chargesEnabled": True}),
                     locations_a=[location_row(TENANT_A, LOC_A, timezone="Europe/Stockholm"),
                                  location_row(TENANT_A, LOC_A2)],
                     locations_b=[location_row(TENANT_B, LOC_B)])
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"}
                                  for a in ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK")],
            GlobalSecondaryIndexes=[
                {"IndexName": "byLocationDay", "Projection": {"ProjectionType": "ALL"},
                 "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"},
                               {"AttributeName": "GSI1SK", "KeyType": "RANGE"}]},
                {"IndexName": "byExpiry", "Projection": {"ProjectionType": "KEYS_ONLY"},
                 "KeySchema": [{"AttributeName": "GSI2PK", "KeyType": "HASH"},
                               {"AttributeName": "GSI2SK", "KeyType": "RANGE"}]}])
        from shared import dynamo
        dynamo._resource = None
        dynamo._client = None
        gc.reset_pepper()
        fake = FakeStripe()
        monkeypatch.setattr(stripe, "request", fake)
        clock = SimpleNamespace(value=NOW)
        monkeypatch.setattr(gc, "now", lambda: clock.value)
        published = []
        monkeypatch.setattr(events, "publish", lambda f, t, d: published.append((t, d)) or "id")
        api = load("gift-cards")
        mail = FakeLambda()
        api._clients["lambda"] = mail
        yield SimpleNamespace(api=api, stripe=fake, mail=mail, clock=clock, published=published,
                              res=boto3.resource("dynamodb", region_name=REGION))
        dynamo._resource = None
        dynamo._client = None


# --- helpers -----------------------------------------------------------------------------------

def http_call(e, method, template, *, params=None, body=None, query=None, claims=None):
    event = {"routeKey": f"{method} {template}", "pathParameters": dict(params or {}), "queryStringParameters": query,
             "headers": {}, "requestContext": {"http": {"method": method, "sourceIp": "203.0.113.5"}},
             "body": json.dumps(body) if body is not None else None}
    if claims:
        with_tenant_claims(event, *claims)
    response = e.api.handler(event, None)
    return response["statusCode"], (json.loads(response["body"]) if response.get("body") else None)


OWNER_A, OWNER_B, STAFF_A = (TENANT_A, "owner_user"), (TENANT_B, "owner_user"), (TENANT_A, "staff_user")


def enable(e, claims=OWNER_A, **over):
    body = {"enabled": True, "amountsSek": [250, 500, 1000], "customMinSek": 100, "customMaxSek": 5000,
            "validityMonths": 24, "version": 0, **over}
    return http_call(e, "PUT", "/tenant/gift-cards/settings", body=body, claims=claims)


def buy(e, purchase_id=P1, loc=LOC_A, amount=500, **over):
    body = {"purchaseId": purchase_id, "amountSek": amount, "buyerEmail": "Kopare@Example.se",
            "recipientName": "Sara", "recipientEmail": "sara@example.se", "message": "Grattis!", **over}
    return http_call(e, "POST", "/public/locations/{locationId}/gift-cards/checkout", params={"locationId": loc},
                     body=body)


def succeed(e, purchase_id=P1, tenant=TENANT_A, account=ACCT_A, amount=50000, meta_tenant=None):
    pid = f"pi_{('giftcard-' + purchase_id)[-10:]}"
    return e.api.handler({"hook": "stripe.event", "hookVersion": 1, "tenantId": tenant,
                          "type": "payment_intent.succeeded", "account": account,
                          "object": {"id": pid, "amount": amount, "metadata": {
                              "kind": "giftcard", "tenantId": meta_tenant or tenant, "purchaseId": purchase_id}}})


def issued_code(e):
    text = e.mail.calls[-1]["notice"]["text"]
    return re.search(r"Kod: ([0-9A-Z-]{19})", text).group(1)


def issue(e, purchase_id=P1, loc=LOC_A, claims=OWNER_A, amount=500):
    tenant_id = claims[0]
    if not e.res.Table(TABLE).get_item(Key=gc.settings_key(tenant_id)).get("Item"):
        enable(e, claims=claims)
    assert buy(e, purchase_id, loc=loc, amount=amount)[0] == 200
    assert succeed(e, purchase_id, tenant=tenant_id, account=ACCT_A if tenant_id == TENANT_A else ACCT_B,
                   amount=amount * 100)["result"] == "issued"
    return issued_code(e), gc.card_id_for(purchase_id)


def hook(e, name, **fields):
    return e.api.handler({"hook": name, "hookVersion": 1, **fields})


# --- E4/E5 settings and E1 offer ----------------------------------------------------------------

def test_settings_owner_only_validation_and_version(env):
    status, body = http_call(env, "GET", "/tenant/gift-cards/settings", claims=OWNER_A)
    assert status == 200 and body["enabled"] is False and body["validityMonths"] == 24
    assert enable(env, validityMonths=6)[1]["code"] == "invalid_validity"
    assert enable(env, amountsSek=[50])[1]["code"] == "invalid_amount"
    assert enable(env, claims=STAFF_A)[1]["error"] == "owner_only"
    assert enable(env)[1]["version"] == 1
    assert enable(env)[1]["code"] == "changed_retry"


OFFER = "/public/locations/{locationId}/gift-cards/offer"


def test_offer_public_and_off(env):
    assert http_call(env, "GET", OFFER, params={"locationId": LOC_A})[0] == 404
    enable(env)
    status, body = http_call(env, "GET", OFFER, params={"locationId": LOC_A})
    assert status == 200 and body == {"amountsSek": [250, 500, 1000], "customMinSek": 100, "customMaxSek": 5000,
                                      "validityMonths": 24}
    shared_tenant.reset_caches()
    env.res.Table(TENANT_TABLE).put_item(Item=tenant_row(TENANT_A, features=features_on("kassa")))
    shared_tenant.reset_caches()
    assert http_call(env, "GET", OFFER, params={"locationId": LOC_A})[0] == 404


# --- E2 checkout and the webhook issue -------------------------------------------------------------

def test_checkout_validation_and_idempotent(env):
    enable(env)
    assert buy(env, amount=50)[1]["code"] == "invalid_amount"
    assert buy(env, buyerEmail="nope")[1]["code"] == "invalid_email"
    assert buy(env, message="x" * 301)[1]["code"] == "message_too_long"
    status, first = buy(env)
    assert status == 200 and first["amountSek"] == 500 and first["clientSecret"].endswith("_secret")
    assert first["expiresAt"].startswith("2028-11-04")
    again = buy(env)[1]
    assert again["clientSecret"] == first["clientSecret"]
    # the retry reads the stored PaymentIntent (no second create, valid after Stripe's 24 h key window)
    assert [(c["method"], c["path"]) for c in env.stripe.calls] == [
        ("POST", "/v1/payment_intents"), ("GET", f"/v1/payment_intents/{first['clientSecret'][:-7]}")]
    assert env.stripe.calls[1]["account"] == ACCT_A
    assert {c["key"] for c in env.stripe.calls if c["method"] == "POST"} == {f"giftcard-{P1}"}
    assert env.stripe.calls[0]["account"] == ACCT_A
    assert env.stripe.calls[0]["params"]["metadata"]["kind"] == "giftcard"
    assert buy(env, amount=1000)[1]["code"] == "changed_retry"
    env.stripe.fail = True
    assert buy(env, P2)[1]["code"] == "stripe_unavailable"


def test_webhook_issues_once_and_never_stores_the_code(env):
    code, card_id = issue(env)
    assert succeed(env)["result"] == "already_issued"  # replay
    assert len(env.mail.calls) == 1
    notice = env.mail.calls[0]["notice"]
    assert notice["kind"] == "gift-cards.code" and notice["to"] == "sara@example.se"
    raw = code.replace("-", "")
    rows = json.dumps([{k: str(v) for k, v in i.items()} for i in env.res.Table(TABLE).scan()["Items"]])
    assert raw not in rows and code not in rows
    assert all(raw not in json.dumps(d) for _, d in env.published)
    card = gc.get_card(TENANT_A, card_id)
    assert card["last4"] == raw[-4:] and int(card["balanceOre"]) == 50000
    assert ("GiftCard.Sold", {"tenantId": TENANT_A, "cardId": card_id, "locationId": LOC_A, "amountOre": 50000,
                              "expiresAt": card["expiresAt"]}) in env.published


def test_same_event_from_a_foreign_account_changes_nothing(env):
    enable(env)
    buy(env)
    assert succeed(env, account=ACCT_B)["result"] == "ignored:account_mismatch"
    assert succeed(env, tenant=TENANT_B, account=ACCT_B, meta_tenant=TENANT_B)["result"] == "ignored:unknown_purchase"
    assert gc.get_card(TENANT_A, gc.card_id_for(P1)) is None and env.mail.calls == []


def failed(e, purchase_id=P1):
    pid = f"pi_{('giftcard-' + purchase_id)[-10:]}"
    return e.api.handler({"hook": "stripe.event", "hookVersion": 1, "tenantId": TENANT_A,
                          "type": "payment_intent.payment_failed", "account": ACCT_A,
                          "object": {"id": pid, "amount": 50000, "metadata": {
                              "kind": "giftcard", "tenantId": TENANT_A, "purchaseId": purchase_id}}})


def test_failed_then_succeeded_issues_once(env):
    enable(env)
    buy(env)
    assert failed(env)["result"] == "failed"  # card declined; the guest retries on the same PaymentIntent
    purchase = env.res.Table(TABLE).get_item(Key=gc.purchase_key(TENANT_A, P1))["Item"]
    assert purchase["status"] == "failed"
    assert succeed(env)["result"] == "issued"
    assert succeed(env)["result"] == "already_issued"
    assert failed(env)["result"] == "failed"  # a late failure event changes nothing
    purchase = env.res.Table(TABLE).get_item(Key=gc.purchase_key(TENANT_A, P1))["Item"]
    assert purchase["status"] == "issued" and "ttl" not in purchase
    assert len(env.mail.calls) == 1
    assert gc.get_card(TENANT_A, gc.card_id_for(P1))["status"] == "active"
    issues = [t for t in gc.transactions(TENANT_A, gc.card_id_for(P1)) if t["kind"] == "issue"]
    assert len(issues) == 1


def test_webhook_amount_mismatch_ignored(env):
    enable(env)
    buy(env)
    assert succeed(env, amount=100)["result"] == "ignored:mismatch"


# --- E3 balance, E11 lookup ----------------------------------------------------------------------

def test_balance_and_lookup(env):
    code, card_id = issue(env)
    path = "/public/locations/{locationId}/gift-cards/balance"
    status, body = http_call(env, "POST", path, params={"locationId": LOC_A}, body={"code": code.lower()})
    assert status == 200 and body["balanceSek"] == 500 and body["status"] == "active"
    assert http_call(env, "POST", path, params={"locationId": LOC_A}, body={"code": "AAAA-BBBB"})[0] == 404
    assert http_call(env, "POST", path, params={"locationId": LOC_B}, body={"code": code})[0] == 404  # B's site
    look = "/locations/{locationId}/gift-cards/lookup"
    status, body = http_call(env, "POST", look, params={"locationId": LOC_A}, body={"code": code}, claims=STAFF_A)
    assert status == 200 and body["cardId"] == card_id
    assert http_call(env, "POST", look, params={"locationId": LOC_A}, body={}, claims=STAFF_A)[0] == 400
    assert http_call(env, "POST", look, params={"locationId": LOC_A2}, body={"code": code}, claims=STAFF_A)[0] == 404
    assert http_call(env, "POST", look, params={"locationId": LOC_B}, body={"code": code}, claims=OWNER_B)[0] == 404


# --- E12/E13 Kassa redemptions and the kassa hook -------------------------------------------------

REDEEM = "/locations/{locationId}/gift-cards/redemptions"


def redeem(e, code, tx="01PAYMENTAAAAAAAAAAAAAAAAA", amount=12000, claims=STAFF_A, loc=LOC_A, bill="01BILLAAAAAAAAA"):
    return http_call(e, "POST", REDEEM, params={"locationId": loc}, claims=claims,
                     body={"txId": tx, "code": code, "amountOre": amount, "billId": bill})


def test_kassa_redemption_partial_idempotent_and_verify(env):
    code, card_id = issue(env)
    status, body = redeem(env, code)
    assert status == 200 and body["balanceAfterSek"] == 380
    assert redeem(env, code)[1]["balanceAfterSek"] == 380  # same txId -> one debit
    assert redeem(env, code, amount=100)[1]["code"] == "changed_retry"
    assert redeem(env, code, tx="01PAYMENTBBBBBBBBBBBBBBBBB", amount=99999)[1]["code"] == "insufficient_balance"
    verified = hook(env, "kassa.giftcard", tenantId=TENANT_A, op="verify", locationId=LOC_A, billId="01BILLAAAAAAAAA",
                    paymentId="01PAYMENTAAAAAAAAAAAAAAAAA")
    assert verified == {"ok": True, "amountOre": 12000, "cardId": card_id, "cardLast4": code[-4:],
                        "balanceAfterOre": 38000}
    wrong_bill = hook(env, "kassa.giftcard", tenantId=TENANT_A, op="verify", locationId=LOC_A, billId="other1234",
                      paymentId="01PAYMENTAAAAAAAAAAAAAAAAA")
    assert wrong_bill["status"] == 404
    # after Kassa took it, the direct reverse is refused; Kassa's undo reverses once
    rev = REDEEM + "/{txId}/reverse"
    assert http_call(env, "POST", rev, params={"locationId": LOC_A, "txId": "01PAYMENTAAAAAAAAAAAAAAAAA"},
                     claims=STAFF_A)[1]["code"] == "receipt_issued"
    undo = hook(env, "kassa.giftcard", tenantId=TENANT_A, op="reverse", locationId=LOC_A, billId="01BILLAAAAAAAAA",
                paymentId="01PAYMENTAAAAAAAAAAAAAAAAA")
    assert undo["balanceAfterOre"] == 50000
    again = hook(env, "kassa.giftcard", tenantId=TENANT_A, op="reverse", locationId=LOC_A, billId="01BILLAAAAAAAAA",
                 paymentId="01PAYMENTAAAAAAAAAAAAAAAAA")
    assert again["balanceAfterOre"] == 50000


def test_direct_reverse_before_kassa_and_needs_kassa(env):
    code, _ = issue(env)
    redeem(env, code)
    rev = REDEEM + "/{txId}/reverse"
    status, body = http_call(env, "POST", rev, params={"locationId": LOC_A, "txId": "01PAYMENTAAAAAAAAAAAAAAAAA"},
                             claims=STAFF_A, body={})
    assert status == 200 and body["balanceAfterSek"] == 500
    assert http_call(env, "POST", rev, params={"locationId": LOC_A, "txId": "01PAYMENTAAAAAAAAAAAAAAAAA"},
                     claims=STAFF_A, body={})[1]["balanceAfterSek"] == 500  # second reverse: same balance
    assert http_call(env, "POST", rev, params={"locationId": LOC_B, "txId": "01PAYMENTAAAAAAAAAAAAAAAAA"},
                     claims=OWNER_B, body={})[0] == 404
    env.res.Table(TENANT_TABLE).put_item(Item=tenant_row(TENANT_A, features=features_on("giftCards"),
                                                         stripe={"accountId": ACCT_A, "chargesEnabled": True}))
    shared_tenant.reset_caches()
    assert redeem(env, code, tx="01PAYMENTCCCCCCCCCCCCCCCCC")[1]["error"] == "feature_not_in_plan"


def test_concurrent_redemptions_never_go_negative(env):
    code, card_id = issue(env, amount=250)
    real = gc.get_card
    stale = {}

    def racing(tenant_id, cid):  # both writers read the same version first
        if cid not in stale:
            stale[cid] = real(tenant_id, cid)
            return stale[cid]
        return real(tenant_id, cid)
    gc.get_card = racing
    try:
        first = redeem(env, code, tx="01PAYMENTXXXXXXXXXXXXXXXXXX", amount=20000)
        second = redeem(env, code, tx="01PAYMENTYYYYYYYYYYYYYYYYYY", amount=20000)
    finally:
        gc.get_card = real
    assert first[0] == 200 and second[1]["code"] == "insufficient_balance"
    assert int(gc.get_card(TENANT_A, card_id)["balanceOre"]) == 5000


# --- checkout.giftcard (online) -------------------------------------------------------------------

def test_online_reserve_reverse_and_refund_once(env):
    code, card_id = issue(env, amount=250)
    reserve = hook(env, "checkout.giftcard", tenantId=TENANT_A, op="reserve", locationId=LOC_A, orderId="ord1",
                   code=code, totalOre=40000)
    assert reserve == {"ok": True, "debitedOre": 25000, "cardId": card_id, "cardLast4": code[-4:]}
    again = hook(env, "checkout.giftcard", tenantId=TENANT_A, op="reserve", locationId=LOC_A, orderId="ord1",
                 code=code, totalOre=40000)
    assert again["debitedOre"] == 25000
    assert gc.get_card(TENANT_A, card_id)["status"] == "used"
    rev = hook(env, "checkout.giftcard", tenantId=TENANT_A, op="reverse", locationId=LOC_A, orderId="ord1")
    assert rev == {"ok": True, "reversedOre": 25000}
    assert hook(env, "checkout.giftcard", tenantId=TENANT_A, op="reverse", locationId=LOC_A,
                orderId="ord1")["reversedOre"] == 25000
    assert hook(env, "checkout.giftcard", tenantId=TENANT_A, op="refund", locationId=LOC_A,
                orderId="ord1")["reversedOre"] == 0  # one credit per order
    assert int(gc.get_card(TENANT_A, card_id)["balanceOre"]) == 25000
    miss = hook(env, "checkout.giftcard", tenantId=TENANT_B, op="reserve", locationId=LOC_B, orderId="o2", code=code,
                totalOre=100)
    assert miss["code"] == "gift_card_not_found"  # A's code at B


# --- E6-E10 owner ---------------------------------------------------------------------------------

def test_list_detail_and_liability(env):
    issue(env)
    issue(env, P2, amount=250)
    status, body = http_call(env, "GET", "/tenant/gift-cards", claims=OWNER_A)
    assert status == 200 and body["liabilitySek"] == 750 and len(body["cards"]) == 2
    assert http_call(env, "GET", "/tenant/gift-cards", query={"status": "nope"}, claims=OWNER_A)[0] == 400
    assert http_call(env, "GET", "/tenant/gift-cards", claims=STAFF_A)[1]["error"] == "owner_only"
    card_id = gc.card_id_for(P1)
    status, card = http_call(env, "GET", "/tenant/gift-cards/{cardId}", params={"cardId": card_id}, claims=OWNER_A)
    assert status == 200 and [t["kind"] for t in card["transactions"]] == ["issue"]
    assert http_call(env, "GET", "/tenant/gift-cards/{cardId}", params={"cardId": card_id}, claims=OWNER_B)[0] == 404


def test_adjust_extends_once(env):
    _, card_id = issue(env)
    path = "/tenant/gift-cards/{cardId}/adjust"
    body = {"txId": "01ADJUSTAAAAAAAAAAAAAAAAAA", "expiresAt": "2029-12-31T23:00:00Z", "reason": "Kundtjänst"}
    assert http_call(env, "POST", path, params={"cardId": card_id}, claims=OWNER_A,
                     body={**body, "expiresAt": "2027-01-01T00:00:00Z"})[0] == 400  # shorter
    status, card = http_call(env, "POST", path, params={"cardId": card_id}, claims=OWNER_A, body=body)
    assert status == 200 and card["expiresAt"] == "2029-12-31T23:00:00Z"
    http_call(env, "POST", path, params={"cardId": card_id}, claims=OWNER_A, body=body)
    assert [t["kind"] for t in gc.transactions(TENANT_A, card_id)].count("adjust") == 1
    assert http_call(env, "POST", path, params={"cardId": card_id}, claims=OWNER_B, body=body)[0] == 404


def test_refund_stripe_first_once_and_window(env):
    _, card_id = issue(env)
    path = "/tenant/gift-cards/{cardId}/refund"
    body = {"txId": "01REFUNDAAAAAAAAAAAAAAAAAA", "reason": "Ångerrätt"}
    status, card = http_call(env, "POST", path, params={"cardId": card_id}, claims=OWNER_A, body=body)
    assert status == 200 and card["status"] == "void" and card["balanceSek"] == 0
    http_call(env, "POST", path, params={"cardId": card_id}, claims=OWNER_A, body=body)
    refunds = [c for c in env.stripe.calls if c["path"] == "/v1/refunds"]
    assert len(refunds) == 1 and refunds[0]["key"] == "giftcard-refund-01REFUNDAAAAAAAAAAAAAAAAAA"
    assert refunds[0]["params"]["amount"] == 50000 and refunds[0]["account"] == ACCT_A
    _, other = issue(env, P2)
    env.clock.value = NOW + timedelta(days=15)
    assert http_call(env, "POST", path, params={"cardId": other}, claims=OWNER_A,
                     body={**body, "txId": "01REFUNDBBBBBBBBBBBBBBBBBB"})[1]["code"] == "refund_window_passed"


def test_refund_race_with_a_redemption_refunds_only_what_the_card_held(env, monkeypatch):
    _, card_id = issue(env)
    path = "/tenant/gift-cards/{cardId}/refund"
    body = {"txId": "01REFUNDAAAAAAAAAAAAAAAAAA", "reason": "Ångerrätt"}
    real_change = gc.change_balance

    def redeem_in_between(*args, **kwargs):
        if kwargs.get("expect_version") is not None and args[4] == "refund":
            real_change(TENANT_A, card_id, "01REDEEMAAAAAAAAAAAAAAAAAA", -10000, "redeem", "kassa", LOC_A)
        return real_change(*args, **kwargs)

    monkeypatch.setattr(gc, "change_balance", redeem_in_between)
    status, err = http_call(env, "POST", path, params={"cardId": card_id}, claims=OWNER_A, body=body)
    assert status == 409 and err["code"] == "changed_retry"
    assert [c for c in env.stripe.calls if c["path"] == "/v1/refunds"] == []  # Stripe only after the card
    monkeypatch.setattr(gc, "change_balance", real_change)
    body = {**body, "txId": "01REFUNDBBBBBBBBBBBBBBBBBB"}
    status, card = http_call(env, "POST", path, params={"cardId": card_id}, claims=OWNER_A, body=body)
    assert status == 200 and card["status"] == "void"
    refunds = [c for c in env.stripe.calls if c["path"] == "/v1/refunds"]
    assert len(refunds) == 1 and refunds[0]["params"]["amount"] == 40000


def test_refund_stripe_down_finishes_on_retry_with_the_same_tx(env):
    _, card_id = issue(env)
    path = "/tenant/gift-cards/{cardId}/refund"
    body = {"txId": "01REFUNDAAAAAAAAAAAAAAAAAA", "reason": "Ångerrätt"}
    env.stripe.fail = True
    assert http_call(env, "POST", path, params={"cardId": card_id}, claims=OWNER_A, body=body)[1]["code"] == \
        "stripe_unavailable"
    assert gc.get_card(TENANT_A, card_id)["status"] == "void"  # nothing left to redeem while Stripe is down
    env.stripe.fail = False
    status, card = http_call(env, "POST", path, params={"cardId": card_id}, claims=OWNER_A, body=body)
    assert status == 200 and card["status"] == "void"
    http_call(env, "POST", path, params={"cardId": card_id}, claims=OWNER_A, body=body)
    refunds = [c for c in env.stripe.calls if c["path"] == "/v1/refunds"]
    assert len(refunds) == 2 and {c["key"] for c in refunds} == {"giftcard-refund-01REFUNDAAAAAAAAAAAAAAAAAA"}
    assert all(c["params"]["amount"] == 50000 for c in refunds)
    tx = gc.get_tx(TENANT_A, card_id, body["txId"])
    assert tx["stripeRefundId"].startswith("re_")
    assert [t["kind"] for t in gc.transactions(TENANT_A, card_id)].count("refund") == 1


def refunded(e, amount_refunded, amount=50000, purchase_id=P1, charge="ch_1"):
    pid = f"pi_{('giftcard-' + purchase_id)[-10:]}"
    return e.api.handler({"hook": "stripe.event", "hookVersion": 1, "tenantId": TENANT_A, "type": "charge.refunded",
                          "account": ACCT_A, "object": {"id": charge, "payment_intent": pid, "amount": amount,
                                                        "amount_refunded": amount_refunded,
                                                        "metadata": {"kind": "giftcard", "tenantId": TENANT_A,
                                                                     "purchaseId": purchase_id}}})


def test_charge_refunded_voids_only_a_full_refund(env):
    _, card_id = issue(env)
    assert refunded(env, 20000)["result"] == "flagged:partial_refund"
    card = gc.get_card(TENANT_A, card_id)
    assert card["status"] == "active" and int(card["balanceOre"]) == 50000
    assert int(card["partialRefundOre"]) == 20000
    assert refunded(env, 50000)["result"] == "voided"
    assert gc.get_card(TENANT_A, card_id)["status"] == "void"
    assert refunded(env, 50000)["result"] == "already_void"


def test_code_email_resent_on_replay_when_the_first_send_failed(env):
    enable(env)
    assert buy(env)[0] == 200

    def broken(**kwargs):
        raise RuntimeError("throttled")

    env.api._clients["lambda"] = SimpleNamespace(invoke=broken)
    with pytest.raises(RuntimeError):
        succeed(env)
    card_id = gc.card_id_for(P1)
    assert "codeSentAt" not in gc.get_card(TENANT_A, card_id)
    env.api._clients["lambda"] = env.mail
    assert succeed(env)["result"] == "already_issued"  # within the grace: the first may still be sending
    assert env.mail.calls == []
    env.clock.value = NOW + timedelta(minutes=5)
    assert succeed(env)["result"] == "code_resent"
    code = issued_code(env)
    assert env.mail.calls[-1]["notice"]["to"] == "sara@example.se"
    assert gc.find_card_by_code(TENANT_A, code)["cardId"] == card_id
    assert gc.get_card(TENANT_A, card_id)["codeSentAt"]
    assert succeed(env)["result"] == "already_issued"
    assert len(env.mail.calls) == 1


def test_code_sent_at_stamped_after_the_invoke(env):
    _, card_id = issue(env)
    assert gc.get_card(TENANT_A, card_id)["codeSentAt"] == gc.iso(NOW)


def test_resend_rotates_the_code(env):
    old, card_id = issue(env)
    path = "/tenant/gift-cards/{cardId}/resend"
    assert http_call(env, "POST", path, params={"cardId": card_id}, claims=OWNER_A, body={"to": "fax"})[0] == 400
    assert http_call(env, "POST", path, params={"cardId": card_id}, claims=OWNER_A, body={"to": "buyer"})[0] == 202
    new = issued_code(env)
    assert env.mail.calls[-1]["notice"]["to"] == "kopare@example.se" and new != old
    assert gc.find_card_by_code(TENANT_A, old) is None
    assert gc.find_card_by_code(TENANT_A, new)["cardId"] == card_id


def _invoke_not_accepted(**kwargs):
    return {"StatusCode": 500}


def _invoke_throttled(**kwargs):
    from botocore.exceptions import ClientError
    raise ClientError({"Error": {"Code": "TooManyRequestsException"}}, "Invoke")


@pytest.mark.parametrize("broken", [_invoke_not_accepted, _invoke_throttled])
def test_resend_answers_502_when_notification_is_down_and_a_retry_works(env, broken):
    old, card_id = issue(env)
    path = "/tenant/gift-cards/{cardId}/resend"
    env.api._clients["lambda"] = SimpleNamespace(invoke=broken)
    status, body = http_call(env, "POST", path, params={"cardId": card_id}, claims=OWNER_A, body={"to": "buyer"})
    assert status == 502 and body == {"error": "notification_unavailable", "code": "notification_unavailable"}
    assert gc.find_card_by_code(TENANT_A, old) is None  # rotated before the send, so the old code is dead
    env.api._clients["lambda"] = env.mail
    assert http_call(env, "POST", path, params={"cardId": card_id}, claims=OWNER_A, body={"to": "buyer"})[0] == 202
    assert gc.find_card_by_code(TENANT_A, issued_code(env))["cardId"] == card_id


# --- nightly expiry, accounting, privacy, purge ----------------------------------------------------

def test_nightly_expiry_idempotent_and_resumable(env):
    _, card_id = issue(env)
    env.clock.value = NOW + timedelta(days=24 * 31 + 3)
    assert env.api.handler({"job": "nightly"})["expired"] == 1
    assert env.api.handler({"job": "nightly"})["expired"] == 0  # resumes after the last finished day
    card = gc.get_card(TENANT_A, card_id)
    assert card["status"] == "expired" and int(card["balanceOre"]) == 0
    env.res.Table(TABLE).put_item(Item={"PK": "JOB", "SK": "NIGHTLY", "lastDay": "2028-11-01"})
    assert env.api.handler({"job": "nightly"})["expired"] == 0  # run twice: one expire TX
    assert [t["kind"] for t in gc.transactions(TENANT_A, card_id)].count("expire") == 1


def test_nightly_counts_failures_per_day_and_resumes_at_the_first_failed_day(env):
    _, card_a = issue(env)
    _, card_b = issue(env, P2)
    # card A expires two days before card B
    env.res.Table(TABLE).update_item(Key=gc.card_key(TENANT_A, card_a),
                                     UpdateExpression="SET expiresAt = :e, GSI2PK = :p",
                                     ExpressionAttributeValues={":e": "2026-11-10T10:00:00Z",
                                                                ":p": "EXPIRY#2026-11-10"})
    env.res.Table(TABLE).update_item(Key=gc.card_key(TENANT_A, card_b),
                                     UpdateExpression="SET expiresAt = :e, GSI2PK = :p",
                                     ExpressionAttributeValues={":e": "2026-11-12T10:00:00Z",
                                                                ":p": "EXPIRY#2026-11-12"})
    env.clock.value = datetime(2026, 11, 14, 2, 0, tzinfo=timezone.utc)
    real = env.api._expire

    def flaky(tenant_id, card_id):
        if card_id == card_b:
            raise gc.GiftCardError(409, "changed_retry")
        return real(tenant_id, card_id)
    env.api._expire = flaky
    try:
        counts = env.api.handler({"job": "nightly"})
    finally:
        env.api._expire = real
    assert counts["expired"] == 1 and counts["failed"] == 1 and counts["failedDays"] == ["2026-11-12"]
    state = env.res.Table(TABLE).get_item(Key={"PK": "JOB", "SK": "NIGHTLY"})["Item"]
    assert state["lastDay"] == "2026-11-11"  # 2026-11-10 and -11 finished; -12 and -13 are retried
    counts = env.api.handler({"job": "nightly"})
    assert counts["expired"] == 1 and counts["failed"] == 0 and counts["failedDays"] == []
    assert env.res.Table(TABLE).get_item(Key={"PK": "JOB", "SK": "NIGHTLY"})["Item"]["lastDay"] == "2026-11-13"
    assert gc.get_card(TENANT_A, card_b)["status"] == "expired"


def test_nightly_reverses_unverified_kassa_redemptions_after_30_minutes(env):
    code, card_id = issue(env)
    redeem(env, code)  # Kassa never verifies (it refuses gift cards with 501 until the voucher means is decided)
    redeem(env, code, tx="01PAYMENTBBBBBBBBBBBBBBBBBB", amount=1000, bill="01BILLBBBBBBBBB")
    hook(env, "kassa.giftcard", tenantId=TENANT_A, op="verify", locationId=LOC_A, billId="01BILLBBBBBBBBB",
         paymentId="01PAYMENTBBBBBBBBBBBBBBBBBB")
    env.clock.value = NOW + timedelta(minutes=10)
    assert env.api.handler({"job": "nightly"})["reversed"] == 0  # too young: Kassa may still take it
    env.clock.value = NOW + timedelta(minutes=31)
    counts = env.api.handler({"job": "nightly"})
    assert counts["reversed"] == 1 and counts["failed"] == 0
    assert int(gc.get_card(TENANT_A, card_id)["balanceOre"]) == 50000 - 1000  # the verified one stays
    assert env.api.handler({"job": "nightly"})["reversed"] == 0  # once
    kinds = [t["kind"] for t in gc.transactions(TENANT_A, card_id)]
    assert kinds.count("reverse") == 1
    late = hook(env, "kassa.giftcard", tenantId=TENANT_A, op="verify", locationId=LOC_A, billId="01BILLAAAAAAAAA",
                paymentId="01PAYMENTAAAAAAAAAAAAAAAAA")
    assert late["code"] == "redemption_reversed"  # Kassa can no longer take the reversed payment


def test_direct_reverse_refused_when_kassa_verifies_in_between(env):
    code, card_id = issue(env)
    redeem(env, code)
    real_get = gc.get_tx

    def verify_first(tenant_id, cid, tx_id):  # Kassa verifies between E13's read and its write
        row = real_get(tenant_id, cid, tx_id)
        if tx_id == "01PAYMENTAAAAAAAAAAAAAAAAA" and not verify_first.done:
            verify_first.done = True
            assert gc.mark_verified(tenant_id, cid, tx_id)
        return row
    verify_first.done = False
    env.api.gc.get_tx = verify_first
    try:
        status, body = http_call(env, "POST", REDEEM + "/{txId}/reverse",
                                 params={"locationId": LOC_A, "txId": "01PAYMENTAAAAAAAAAAAAAAAAA"}, claims=STAFF_A,
                                 body={})
    finally:
        env.api.gc.get_tx = real_get
    assert status == 409 and body["code"] == "receipt_issued"
    assert int(gc.get_card(TENANT_A, card_id)["balanceOre"]) == 50000 - 12000
    assert "reverse" not in [t["kind"] for t in gc.transactions(TENANT_A, card_id)]


def test_accounting_events_balanced_sale_online_breakage(env):
    code, card_id = issue(env)
    hook(env, "checkout.giftcard", tenantId=TENANT_A, op="reserve", locationId=LOC_A, orderId="ord9", code=code,
         totalOre=20000)
    redeem(env, code)  # Kassa: booked by the receipt, nothing here
    answer = hook(env, "accounting.events", tenantId=TENANT_A, locationId=LOC_A, first="2026-11-04", last="2026-11-04")
    assert answer["ok"] and len(answer["events"]) == 1
    postings = answer["events"][0]["postings"]
    assert sum(p["amount"] for p in postings) == 0 and all(p["role"] in accounting.ROLES for p in postings)
    by_role = {}
    for p in postings:
        by_role[p["role"]] = by_role.get(p["role"], 0) + p["amount"]
    assert by_role == {"stripe": 50000 - 20000, "giftCardLiability": -50000 + 20000}
    empty = hook(env, "accounting.events", tenantId=TENANT_A, locationId=LOC_A2, first="2026-11-04", last="2026-11-04")
    assert empty == {"ok": True, "events": []}  # days without gift cards: unchanged totals
    other = hook(env, "accounting.events", tenantId=TENANT_B, locationId=LOC_B, first="2026-11-04", last="2026-11-04")
    assert other["events"] == []  # A's TX rows never in B's Bokföring


def test_privacy_export_and_erase_keep_the_card(env):
    _, card_id = issue(env)
    exported = hook(env, "privacy.export", tenantId=TENANT_A, email="sara@example.se")
    assert [i["role"] for i in exported["items"]] == ["recipient"]
    erased = hook(env, "privacy.erase", tenantId=TENANT_A, email="kopare@example.se")
    assert erased["erased"] == {"giftCardContacts": 1}
    card = gc.get_card(TENANT_A, card_id)
    assert "buyerEmail" not in card and "recipientEmail" not in card and card["status"] == "active"
    assert "buyerEmailHash" not in card and "recipientEmailHash" not in card  # the hashes identify a person too
    assert hook(env, "privacy.export", tenantId=TENANT_A, email="sara@example.se")["items"] == []
    assert hook(env, "privacy.export", tenantId=TENANT_B, email="sara@example.se")["items"] == []


def test_purge_keeps_bookkeeping_rows_with_ttl(env):
    _, card_id = issue(env)
    issue(env, P2, loc=LOC_B, claims=OWNER_B)
    answer = hook(env, "tenant.purge", tenantId=TENANT_A, locationIds=[LOC_A, LOC_A2], round=1)
    assert answer["ok"] and answer["done"]
    rows = [i for i in env.res.Table(TABLE).scan()["Items"] if i["PK"] == f"TENANT#{TENANT_A}"]
    assert not [r for r in rows if r["SK"] in ("SETTINGS",) or r["SK"].startswith("PURCHASE#")]
    assert rows and all(r.get("ttl") for r in rows)
    card = gc.get_card(TENANT_A, card_id)
    assert "buyerEmail" not in card
    assert gc.get_card(TENANT_B, gc.card_id_for(P2))["buyerEmail"] == "kopare@example.se"


def test_unknown_hook_and_routes(env):
    assert hook(env, "nope", tenantId=TENANT_A)["code"] == "unknown_hook"
    assert env.api.handler({"x": 1})["code"] == "unknown_event"
    assert http_call(env, "GET", "/nope")[0] == 404


def test_code_format():
    code = gc.new_code()
    assert len(code) == 16 and set(code) <= set(gc.ALPHABET)
    assert gc.normalize_code(gc.display_code(code).lower()) == code
    assert gc.normalize_code("O0IL-1111-2222-3333") == "0011111122223333"
    assert gc.normalize_code("short") is None

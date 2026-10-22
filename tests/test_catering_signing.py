"""Catering signing (issue 5, M4): the restaurant signs first, then the
customer (BankID-like or a provider's page), simple acceptance, the generic
signing webhook (verification, dedupe, re-fetch), rate limits and the offer
PDF."""

import json

import boto3
import pytest

from shared import bankid, signing
from shared import catering as c
from test_catering import (BUCKET, COMPANY, LOC_A, LOCATION_TABLE, OFFER, REGION, SETTINGS, TENANT_A, body_of, create,
                           env, guest, location_item, restaurant_signs, sent_offer, staff)  # noqa: F401 - env fixture


def allow_acceptance(env):  # noqa: F811
    env.res.Table(LOCATION_TABLE).put_item(Item=location_item(TENANT_A, LOC_A, cateringSettings={
        **SETTINGS, "allowSimpleAcceptance": True}))
    from shared import tenant
    tenant.reset_caches()


def use_redirect_provider(env):  # noqa: F811
    boto3.client("secretsmanager", region_name=REGION).put_secret_value(
        SecretId="signing", SecretString=json.dumps({"provider": "simulator", "startKind": "redirect"}))
    bankid.reset_cache()


def webhook(env, rid, token, body=None):  # noqa: F811
    event = {"queryStringParameters": {"l": LOC_A, "r": rid, "t": token},
             "body": json.dumps(body or {"eventId": "evt-1"}), "headers": {}}
    return env.m["signing-webhook"].handler(event, None)


def test_simple_acceptance_is_off_by_default_and_needs_the_restaurant_first(env):  # noqa: F811
    rid, token = sent_offer(env)
    response = guest(env, "POST", "/accept", rid, token, {"name": "Anna Svensson", "acceptTerms": True})
    assert response["statusCode"] == 409 and body_of(response) == {"error": "acceptance_not_allowed"}
    allow_acceptance(env)
    other, other_token = sent_offer(env, sign=False, phone="0701234568", email="b@example.se")
    response = guest(env, "POST", "/accept", other, other_token, {"name": "Bo", "acceptTerms": True})
    assert body_of(response) == {"error": "awaiting_restaurant"}


def test_private_guest_accepts_without_bankid(env):  # noqa: F811
    allow_acceptance(env)
    rid, token = sent_offer(env)
    assert body_of(guest(env, "GET", "", rid, token))["canAccept"] is True
    assert guest(env, "POST", "/accept", rid, token, {"name": "Anna Svensson"})["statusCode"] == 400
    view = body_of(guest(env, "POST", "/accept", rid, token, {"name": "Anna Svensson", "acceptTerms": True}))
    assert view["status"] == "signed" and view["signedBy"] == "Anna Svensson" and view["canPay"] is True
    item = c.load(LOC_A, rid)
    assert item["signature"]["method"] == "simple_acceptance" and item["notice"]["type"] == "catering_signed"
    s3 = boto3.client("s3", region_name=REGION)
    evidence = json.loads(s3.get_object(Bucket=BUCKET, Key=item["signature"]["evidenceKey"])["Body"].read())
    assert evidence["ip"] == "203.0.113.9" and evidence["pdfSha256"] == item["signing"]["pdfSha256"]
    assert evidence["method"] == "simple_acceptance"


def test_company_acceptance_needs_role_and_authority(env):  # noqa: F811
    allow_acceptance(env)
    rid, token = sent_offer(env, **COMPANY)
    bad = guest(env, "POST", "/accept", rid, token, {"name": "Karl Ek", "acceptTerms": True})
    assert body_of(bad) == {"error": "confirmAuthority must be true"}
    ok = guest(env, "POST", "/accept", rid, token, {"name": "Karl Ek", "role": "VD", "confirmAuthority": True,
                                                    "acceptTerms": True})
    assert body_of(ok)["status"] == "signed"
    assert c.load(LOC_A, rid)["signature"]["role"] == "VD"


def test_offer_document_reads_as_the_contract():
    from test_catering import load
    doc = c.offer_document(
        {"requestId": "r1", "customerType": "company", "customerName": "Anna", "companyName": "Acme AB",
         "orgNumber": "556000-1234", "deliveryDate": "2026-11-20", "deliveryTime": "12:00", "fulfilment": "pickup",
         "guests": 40, "signatory": {"name": "Karl Ek", "role": "VD"}},
        {"version": 2, "lines": [{"name": "Buffé", "unitPrice": 24500, "quantity": 40, "vatRate": 6}],
         "totals": {"subtotal": 980000, "discount": 0, "deliveryFee": 0, "total": 980000, "vat": 55472, "vatRate": 6},
         "validUntil": "2026-11-10", "serviceIncluded": False, "terms": "Avbokning senast 7 dagar innan."},
        {"legalName": "Roma AB", "orgNumber": "556677-8899"}, {"name": "Södermalm"}, {"paymentTermsDays": 30})
    page = load("catering-document", "cat_doc_render").render_html(doc, {"address": "Götgatan 1"})
    for text in ("Offert catering", "Version 2", "10 november 2026", "Roma AB", "Acme AB", "Signerar: Karl Ek, VD",
                 "Antal gäster: 40", "Mat utan servering.", "6 %", "9 800 kr", "varav moms 6 %",
                 "Faktura, 30 dagar netto.", "Avbokning senast 7 dagar innan.", "Restaurangen signerar först"):
        assert text in page, text
    assert "<script" not in page


def test_a_new_version_and_a_decline_cancel_the_open_order(env):  # noqa: F811
    cancelled = []
    env.monkeypatch.setattr(signing, "cancel", lambda order: cancelled.append(order and order["ref"]))
    rid, _ = sent_offer(env)
    first = c.load(LOC_A, rid)["signing"]["order"]["ref"]
    staff(env, "POST", "/offer/send", body=OFFER, request_id=rid)
    assert cancelled[-1] == first
    second = c.load(LOC_A, rid)["signing"]["order"]["ref"]
    staff(env, "POST", "/decline", body={"reason": "Fullt"}, request_id=rid)
    assert cancelled[-1] == second


def test_redirect_provider_both_parties(env):  # noqa: F811
    use_redirect_provider(env)
    rid, token = sent_offer(env, sign=False)
    start = body_of(staff(env, "POST", "/restaurant-sign", request_id=rid))
    assert start == {"kind": "redirect", "url": f"https://admin.example.se/catering/{LOC_A}/{rid}?signering=klar"}
    assert body_of(staff(env, "POST", "/restaurant-sign/status", request_id=rid))["state"] == "signed"
    assert staff(env, "POST", "/restaurant-sign", request_id=rid)["statusCode"] == 409  # already signed
    start = body_of(guest(env, "POST", "/sign", rid, token))
    assert start["kind"] == "redirect" and start["url"].endswith(f"/catering/{LOC_A}/{rid}?restaurang=roma&signering=klar")
    assert body_of(guest(env, "POST", "/sign/status", rid, token))["status"] == "complete"
    assert c.load(LOC_A, rid)["status"] == "signed"


def test_signing_webhook_verifies_dedupes_and_refetches(env):  # noqa: F811
    use_redirect_provider(env)
    rid, token = sent_offer(env, sign=False)
    staff(env, "POST", "/restaurant-sign", request_id=rid)
    staff(env, "POST", "/restaurant-sign/status", request_id=rid)
    guest(env, "POST", "/sign", rid, token)
    order = c.load(LOC_A, rid)["signing"]["order"]
    assert webhook(env, rid, "wrong")["statusCode"] == 401
    assert webhook(env, "f" * 32, order["callbackToken"])["statusCode"] == 404
    # nothing signed at the provider yet: handled, nothing recorded
    assert body_of(webhook(env, rid, order["callbackToken"], {"eventId": "evt-0"})) == {"result": "handled",
                                                                                         "recorded": []}
    real_status = signing.status
    env.monkeypatch.setattr(signing, "status", lambda o: {**real_status(o), "parties": [
        {**p, "signedAt": p.get("signedAt") or 1} for p in real_status(o)["parties"]]})
    assert body_of(webhook(env, rid, order["callbackToken"])) == {"result": "handled", "recorded": ["customer"]}
    assert c.load(LOC_A, rid)["status"] == "signed"
    assert body_of(webhook(env, rid, order["callbackToken"])) == {"result": "duplicate"}
    history = c.history(rid)
    assert any(h["action"] == "signing_webhook" for h in history)


def test_signing_webhook_failure_answers_503_for_a_retry(env):  # noqa: F811
    rid, token = sent_offer(env)
    order = c.load(LOC_A, rid)["signing"]["order"]

    def broken(item):
        raise RuntimeError("boom")
    env.monkeypatch.setattr(env.m["signing-webhook"].sign, "apply_status", broken)
    assert webhook(env, rid, order["callbackToken"])["statusCode"] == 503


@pytest.fixture
def guarded(env, monkeypatch):  # noqa: F811
    env.res.create_table(TableName="test-guard", KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}],
                         AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}],
                         BillingMode="PAY_PER_REQUEST")
    monkeypatch.setenv("REQUEST_GUARD_TABLE_NAME", "test-guard")
    return env


def test_signing_attempts_are_limited(guarded):
    rid, token = sent_offer(guarded)
    for _ in range(10):
        assert guest(guarded, "POST", "/sign", rid, token)["statusCode"] == 200
    limited = guest(guarded, "POST", "/sign", rid, token)
    assert limited["statusCode"] == 429 and body_of(limited) == {"error": "too_many_requests"}

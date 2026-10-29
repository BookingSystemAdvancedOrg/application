"""Catering signing (issue 5, M4): the restaurant signs first, then the
customer (BankID-like or a provider's page), simple acceptance, the generic
signing webhook (verification, dedupe, re-fetch), rate limits and the offer
PDF."""

from error_body import err
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
    assert response["statusCode"] == 409 and body_of(response) == err("acceptance_not_allowed")
    allow_acceptance(env)
    other, other_token = sent_offer(env, sign=False, phone="0701234568", email="b@example.se")
    response = guest(env, "POST", "/accept", other, other_token, {"name": "Bo", "acceptTerms": True})
    assert body_of(response) == err("awaiting_restaurant")


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
    assert body_of(bad) == err("confirmAuthority must be true")
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
    assert limited["statusCode"] == 429 and body_of(limited) == err("too_many_requests")


# --- reminders (M5) -------------------------------------------------------------------------

def _lifecycle(env, item, old):  # noqa: F811
    from types import SimpleNamespace
    from test_catering import stream_record
    return env.m["lifecycle"].handler({"Records": [stream_record(item, old=old)]},
                                      SimpleNamespace(invoked_function_arn="arn:fn"))


def test_owner_reminder_fires_only_while_unanswered(env):  # noqa: F811
    rid = body_of(create(env))["requestId"]
    ctx = None
    remind = {"action": "remind", "kind": "owner", "locationId": LOC_A, "requestId": rid}
    assert env.m["lifecycle"].handler(remind, ctx) == {"result": "reminded"}
    assert c.load(LOC_A, rid)["notice"]["type"] == "catering_owner_reminder"
    sent_offer_rid = sent_offer(env, phone="0701234568", email="b@example.se")[0]
    assert env.m["lifecycle"].handler({**remind, "requestId": sent_offer_rid}, ctx) == {"result": "moved_on"}


def test_sign_reminder_is_scheduled_when_the_restaurant_signs(env):  # noqa: F811
    rid, token = sent_offer(env, sign=False)
    before = c.load(LOC_A, rid)
    restaurant_signs(env, rid)
    item = c.load(LOC_A, rid)
    _lifecycle(env, item, before)
    created = {x[1]["Name"]: x[1] for x in env.sched.calls if x[0] == "create"}
    # validUntil 2026-10-08 (7 days), 2 days before at 10:00 Stockholm (CEST) = 08:00 UTC
    assert created[f"sign-reminder-{rid}"]["ScheduleExpression"] == "at(2026-10-06T08:00:00)"
    assert json.loads(created[f"sign-reminder-{rid}"]["Target"]["Input"])["action"] == "remind"
    remind = {"action": "remind", "kind": "sign", "locationId": LOC_A, "requestId": rid}
    assert env.m["lifecycle"].handler(remind, None) == {"result": "reminded"}
    assert c.load(LOC_A, rid)["notice"]["type"] == "catering_sign_reminder"
    guest(env, "POST", "/sign", rid, token)
    guest(env, "POST", "/sign/status", rid, token)
    guest(env, "POST", "/sign/status", rid, token)
    assert env.m["lifecycle"].handler(remind, None) == {"result": "moved_on"}
    signed = c.load(LOC_A, rid)
    _lifecycle(env, signed, item)
    assert ("delete", {"Name": f"sign-reminder-{rid}", "GroupName": "dev-catering"}) in env.sched.calls


def test_day_before_reminder_after_confirmation_is_dst_safe(env):  # noqa: F811
    rid, _ = sent_offer(env)
    item = c.load(LOC_A, rid)
    confirmed = {**item, "status": "confirmed", "deliveryDate": "2026-10-26"}
    _lifecycle(env, confirmed, {**item, "status": "signed"})
    created = {x[1]["Name"]: x[1] for x in env.sched.calls if x[0] == "create"}
    # 2026-10-25 is the autumn DST change: 10:00 Stockholm (CET) = 09:00 UTC
    assert created[f"day-before-{rid}"]["ScheduleExpression"] == "at(2026-10-25T09:00:00)"


def test_reminder_messages():
    from shared import catering_messages as m
    item = {"customerName": "Anna Svensson", "deliveryDate": "2026-10-20", "deliveryTime": "12:00", "guests": 40,
            "fulfilment": "delivery", "offer": {"totals": {"total": 1000000}, "validUntil": "2026-10-08"},
            "signature": {"method": "simple_acceptance"}}
    tenant_row, location = {"name": "Roma"}, {"name": "Södermalm"}
    assert "väntar på din signatur" in m.guest_email("catering_sign_reminder", item, tenant_row, location, "L")[1]
    assert "i morgon" in m.guest_email("catering_day_before", item, tenant_row, location, "L")[1]
    assert "40 gäster" in m.owner_email("catering_owner_reminder", item, tenant_row, location, "A")[1]
    assert "godkänd" in m.guest_email("catering_signed", item, tenant_row, location, "L")[0]
    assert m.guest_sms("catering_sign_reminder", item, tenant_row, location, "L").endswith(": L")
    assert set(m.ROUTING) == set(c.NOTICE_TYPES)


# --- review fixes -----------------------------------------------------------------------------

def _s3():
    return boto3.client("s3", region_name=REGION)


def test_signed_copy_is_the_exact_pdf_version_even_after_an_overwrite(env):  # noqa: F811
    rid, token = sent_offer(env)
    s = c.load(LOC_A, rid)["signing"]
    _s3().put_object(Bucket=BUCKET, Key=s["pdfKey"], Body=b"%PDF-1.7 something else")  # a later render
    guest(env, "POST", "/sign", rid, token)
    guest(env, "POST", "/sign/status", rid, token)
    assert body_of(guest(env, "POST", "/sign/status", rid, token))["status"] == "complete"
    item = c.load(LOC_A, rid)
    import hashlib
    signed = _s3().get_object(Bucket=BUCKET, Key=item["signedPdfKey"], VersionId=item["signedPdfVersionId"])
    assert hashlib.sha256(signed["Body"].read()).hexdigest() == s["pdfSha256"]
    url = body_of(guest_document(env, rid, token, "offer"))["url"]
    assert f"versionId={s['pdfVersionId']}" in url


def test_a_pdf_that_does_not_match_the_hash_is_refused_not_locked(env):  # noqa: F811
    rid, token = sent_offer(env)
    item = c.load(LOC_A, rid)
    other = _s3().put_object(Bucket=BUCKET, Key=item["signing"]["pdfKey"], Body=b"%PDF-1.7 tampered")
    env.res.Table("test-catering-requests").update_item(
        Key=c.request_key(LOC_A, rid), UpdateExpression="SET signing.pdfVersionId = :v",
        ExpressionAttributeValues={":v": other["VersionId"]})
    guest(env, "POST", "/sign", rid, token)
    guest(env, "POST", "/sign/status", rid, token)
    response = guest(env, "POST", "/sign/status", rid, token)
    assert response["statusCode"] == 503 and body_of(response) == err("document_mismatch")
    assert c.load(LOC_A, rid)["status"] == "offer_sent"
    listed = _s3().list_objects_v2(Bucket=BUCKET, Prefix=f"{TENANT_A}/signed/{rid}/").get("Contents") or []
    assert listed == []


def test_restaurant_evidence_stays_unlocked_until_the_contract_is_binding(env):  # noqa: F811
    rid, token = sent_offer(env)
    key = c.load(LOC_A, rid)["signing"]["signatures"]["restaurant"]["evidenceKey"]
    assert key == f"{TENANT_A}/offers/{rid}/v0001-restaurant-bankid.json"
    assert "ObjectLockMode" not in _s3().head_object(Bucket=BUCKET, Key=key)
    assert _s3().get_object_tagging(Bucket=BUCKET, Key=key)["TagSet"] == [{"Key": "retention", "Value": "offer"}]
    guest(env, "POST", "/sign", rid, token)
    guest(env, "POST", "/sign/status", rid, token)
    guest(env, "POST", "/sign/status", rid, token)
    moved = c.load(LOC_A, rid)["signing"]["signatures"]["restaurant"]["evidenceKey"]
    assert moved == f"{TENANT_A}/signed/{rid}/v0001/restaurant-bankid.json"
    assert _s3().head_object(Bucket=BUCKET, Key=moved)["ObjectLockMode"] == "GOVERNANCE"


def test_webhook_for_a_replaced_version_is_refused(env):  # noqa: F811
    rid, token = sent_offer(env)
    old = c.load(LOC_A, rid)["signing"]["order"]["callbackToken"]
    staff(env, "POST", "/offer/send", body=OFFER, request_id=rid)
    assert webhook(env, rid, old)["statusCode"] == 401
    new = c.load(LOC_A, rid)["signing"]["order"]["callbackToken"]
    assert body_of(webhook(env, rid, new, {"eventId": "evt-v2"})) == {"result": "handled", "recorded": []}


def test_two_completions_at_once_record_only_one(env):  # noqa: F811
    from shared import catering_signing as sign
    rid, _ = sent_offer(env)
    item = c.load(LOC_A, rid)
    signer = {"name": "Anna", "method": "simple_acceptance"}
    sign.record(item, "customer", signer, {"customer-acceptance.json": b"{}"}, "1.1.1.1", item["signing"]["order"])
    with pytest.raises(c.InvalidState):
        sign.record(item, "customer", signer, {"customer-acceptance.json": b"{}"}, "1.1.1.1", item["signing"]["order"])
    assert c.load(LOC_A, rid)["status"] == "signed"


def test_guest_gets_no_offer_pdf_before_the_restaurant_signed(env):  # noqa: F811
    rid, token = sent_offer(env, sign=False)
    assert guest_document(env, rid, token, "offer")["statusCode"] == 404
    assert body_of(guest(env, "GET", "", rid, token))["documents"] == []
    restaurant_signs(env, rid)
    assert guest_document(env, rid, token, "offer")["statusCode"] == 200


def test_restaurant_cannot_sign_an_expired_offer(env):  # noqa: F811
    rid, _ = sent_offer(env, sign=False)
    env.res.Table("test-catering-requests").update_item(
        Key=c.request_key(LOC_A, rid), UpdateExpression="SET validUntil = :v", ExpressionAttributeValues={":v": "2026-09-30"})
    response = staff(env, "POST", "/restaurant-sign", request_id=rid)
    assert response["statusCode"] == 409 and body_of(response) == err("offer_expired")


def test_a_failed_send_keeps_the_open_order(env):  # noqa: F811
    cancelled = []
    env.monkeypatch.setattr(signing, "cancel", lambda order: cancelled.append(order and order["ref"]))
    rid, _ = sent_offer(env)
    first = c.load(LOC_A, rid)["signing"]["order"]["ref"]
    big = {**OFFER, "lines": [{"name": "Lax", "unitPrice": 140, "quantity": 101, "menuItemId": "lax"}]}
    assert body_of(staff(env, "POST", "/offer/send", body=big, request_id=rid)) == err("capacity_full")
    assert first not in cancelled and c.load(LOC_A, rid)["signing"]["order"]["ref"] == first


def guest_document(env, rid, token, doc_id):  # noqa: F811
    event = {"routeKey": "GET /locations/{locationId}/catering/requests/{requestId}/customer/documents/{docId}",
             "pathParameters": {"locationId": LOC_A, "requestId": rid, "docId": doc_id},
             "headers": {"X-Order-Token": token}, "requestContext": {"http": {"sourceIp": "203.0.113.9"}}}
    return env.m["customer"].handler(event, None)

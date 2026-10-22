"""catering-customer

TRIGGER:
    API Gateway -- Auth: NONE; every route needs the guest's magic-link
    token in the X-Order-Token header (shared/catering.py, HMAC over the
    request id and its linkVersion). A wrong token answers 404.

    GET  /locations/{locationId}/catering/requests/{requestId}/customer
    POST .../customer/sign             start BankID signing of the current offer version
    POST .../customer/sign/status      poll the signing (QR code, hints, result)
    POST .../customer/checkout         private guest: pay the signed offer (Stripe Checkout);
                                       {"embedded": true} -> {clientSecret, stripeAccount}
                                       for the site's Payment Element, else {checkoutUrl}
    POST .../customer/change           {"message"} ask the restaurant to change the offer
    POST .../customer/reject           {"reason"} decline the offer
    GET  .../customer/documents/signature   5-minute link to the signature evidence

PURPOSE:
    The guest's side. The guest signs exactly one offer version with
    BankID (shared/bankid.py); what BankID shows is a summary plus the
    document id (SHA-256 of the offer content, shared/catering.py
    offer_document), and the same hash is in the signature. On completion:
      - BankID's signature + OCSP response and the signed document are
        stored as evidence in the catering-documents bucket (Object Lock)
      - offer_sent -> signed (notice catering_signed: restaurant + guest)
      - a company is then invoiced by catering-lifecycle (-> confirmed);
        a private guest pays here (Checkout -> catering-stripe-webhook ->
        confirmed). A failed or abandoned payment keeps the signature; the
        guest can pay again from the link until the offer's valid-until.
    Nothing here marks a request paid - only Stripe's webhook does.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    CATERING_REQUESTS_TABLE_NAME, CATERING_REQUEST_HISTORY_TABLE_NAME,
    CATERING_DOCUMENTS_BUCKET_NAME, LINK_SIGNING_KEY_SECRET_ARN,
    SIGNING_PROVIDER_SECRET_ARN, STRIPE_SECRET_ARN, STRIPE_API_VERSION, CUSTOMER_SITE_URL
"""

import json
import logging
import os
from datetime import date
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from shared import bankid, http, payment_domains, site, tenant
from shared import catering as c
from shared import catering_stripe as cs
from shared import orders as o
from shared import stripe_client as stripe

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_BASE = "/locations/{locationId}/catering/requests/{requestId}/customer"
_ROUTES = {
    ("GET", _BASE): "view",
    ("POST", _BASE + "/sign"): "sign",
    ("POST", _BASE + "/sign/status"): "sign_status",
    ("POST", _BASE + "/checkout"): "checkout",
    ("POST", _BASE + "/change"): "change",
    ("POST", _BASE + "/reject"): "reject",
    ("GET", _BASE + "/documents/{docId}"): "document",
}


def _context(event):
    location_id = http.path_id(event, "locationId")
    ctx = tenant.for_public(location_id, feature="catering")
    item = c.load(location_id, http.path_id(event, "requestId"))
    if item.get("tenantId") != ctx.tenant_id or not c.token_matches(item, http.header(event, "x-order-token")):
        raise c.NotFound
    return ctx, item


def _today(location, now):
    return now.astimezone(ZoneInfo(location.get("timezone") or "Europe/Stockholm")).date()


def _offer_still_valid(item, location, now):
    if not item.get("validUntil") or date.fromisoformat(item["validUntil"]) < _today(location, now):
        raise c.InvalidState("offer_expired")


def _view_of(ctx, item):
    offer = None
    if item.get("currentOfferVersion"):
        try:
            offer = c.load_offer(item["requestId"], item["currentOfferVersion"])
        except c.NotFound:
            offer = None
    return c.guest_view(item, offer, ctx.location, c.settings_of(ctx.location))


def _view(event):
    ctx, item = _context(event)
    return http.respond(200, _view_of(ctx, item))


def _visible_text(ctx, item, offer):
    seller = (ctx.tenant.get("legalName") or ctx.tenant.get("name") or "").replace("*", "")
    when = f"{item.get('deliveryDate')} kl {item.get('deliveryTime')}"
    total = c.money(offer["totals"]["total"])
    return (
        f"# Offert catering\n\n"
        f"Säljare: {seller}\n\n"
        f"Leverans: {when}\n\n"
        f"Totalt: {total} inkl. moms\n\n"
        f"Offertversion: {int(offer['version'])}\n\n"
        f"Dokument-ID: {offer['documentHash'][:32]}\n\n"
        f"Jag godkänner offerten och villkoren."
    )


def _sign(event):
    ctx, item = _context(event)
    http.body(event, allowed=set(), required=False)
    if item["status"] != c.OFFER_SENT:
        raise c.InvalidState("not_signable")
    now = c.now_utc()
    _offer_still_valid(item, ctx.location, now)
    offer = c.load_offer(item["requestId"], item["currentOfferVersion"])
    if item.get("signing"):
        bankid.cancel(item["signing"])
    order = bankid.start(http.source_ip(event), _visible_text(ctx, item, offer), offer["documentHash"])
    signing = {**order, "offerVersion": int(offer["version"]), "documentHash": offer["documentHash"]}
    c.transact([
        c.head_update(item, sets=["signing = :s", "updatedAt = :now"],
                      values={":s": signing, ":now": c.iso(now)}),
        c.audit_put(item["requestId"], "signing_started", "guest", now, version=int(offer["version"]),
                    ip=http.source_ip(event), provider=bankid.provider()),
    ])
    return http.respond(200, {"status": "pending", "autoStartToken": order["autoStartToken"],
                              "qrData": bankid.qr_data(order)})


def _evidence(ctx, item, signing, completion, offer, now):
    key = f"{ctx.tenant_id}/signed/{item['requestId']}/v{int(signing['offerVersion']):04d}.json"
    body = json.dumps({
        "requestId": item["requestId"], "offerVersion": int(signing["offerVersion"]),
        "documentHash": signing["documentHash"], "document": c.plain(offer.get("document")),
        "signedAt": c.iso(now), "provider": bankid.provider(), "orderRef": signing["orderRef"],
        "signer": {"name": completion.get("name"), "personalNumber": completion.get("personalNumber")},
        "signature": completion.get("signature"), "ocspResponse": completion.get("ocspResponse"),
        "bankIdIssueDate": completion.get("bankIdIssueDate"), "device": completion.get("device"),
    }, ensure_ascii=False, default=str).encode("utf-8")
    boto3.client("s3").put_object(Bucket=os.environ["CATERING_DOCUMENTS_BUCKET_NAME"], Key=key, Body=body,
                                  ContentType="application/json", ChecksumAlgorithm="SHA256")
    return key


def _sign_status(event):
    ctx, item = _context(event)
    http.body(event, allowed=set(), required=False)
    signing = item.get("signing")
    if item["status"] != c.OFFER_SENT or not signing:
        return http.respond(200, {"status": "none", "request": _view_of(ctx, item)})
    result = bankid.collect(signing)
    now = c.now_utc()
    if result["status"] == "pending":
        if bankid.provider() == "simulator":
            c.dynamo.table(c.requests_table()).update_item(
                Key=c.request_key(item["locationId"], item["requestId"]),
                UpdateExpression="SET signing.polls = signing.polls + :one",
                ConditionExpression="attribute_exists(signing)",
                ExpressionAttributeValues={":one": 1})
        return http.respond(200, {"status": "pending", "hintCode": result.get("hintCode"),
                                  "qrData": bankid.qr_data(signing)})
    if result["status"] == "failed":
        c.transact([
            c.head_update(item, removes=["signing"], sets=["updatedAt = :now"], values={":now": c.iso(now)}),
            c.audit_put(item["requestId"], "signing_failed", "guest", now, hint=result.get("hintCode")),
        ])
        return http.respond(200, {"status": "failed", "hintCode": result.get("hintCode")})
    # complete - only for the version that is still current
    if int(signing["offerVersion"]) != int(item.get("currentOfferVersion") or 0):
        raise c.InvalidState("offer_changed")
    completion = result["completion"]
    offer = c.load_offer(item["requestId"], signing["offerVersion"])
    key = _evidence(ctx, item, signing, completion, offer, now)
    signature = {"name": completion.get("name"),
                 "personalNumberMasked": bankid.mask_personal_number(completion.get("personalNumber")),
                 "signedAt": c.iso(now), "documentHash": signing["documentHash"], "evidenceKey": key,
                 "provider": bankid.provider()}
    account = o.stripe_account(ctx.tenant, ctx.location)
    c.transact([
        c.head_update(item, to_status=c.SIGNED, sets=[
            "signature = :sig", "signedAt = :now", "signedOfferVersion = :v", "notice = :n",
            "updatedAt = :now", "stripeAccountId = :acct"], removes=["signing"],
            values={":sig": signature, ":now": c.iso(now), ":v": int(signing["offerVersion"]),
                    ":n": c.notice(c.N_SIGNED, now), ":acct": account or "-"}),
        c.audit_put(item["requestId"], "signed", "guest", now, version=int(signing["offerVersion"]),
                    documentHash=signing["documentHash"], ip=http.source_ip(event), evidence=key),
    ])
    logger.info(json.dumps({"catering": "signed", "requestId": item["requestId"],
                            "version": int(signing["offerVersion"])}))
    return http.respond(200, {"status": "complete",
                              "request": _view_of(ctx, c.load(item["locationId"], item["requestId"]))})


def _expire_previous(item):
    """One payable session per request: a new attempt first expires the
    previous one, so two open sessions (two tabs, a reload) can never both
    take the money. A session that already completed is being settled by
    the webhook - no new attempt then."""
    previous, account = item.get("checkoutSessionId"), item.get("stripeAccountId")
    if not previous or not account:
        return
    try:
        stripe.request("POST", f"/v1/checkout/sessions/{previous}/expire", account=account)
    except stripe.StripeError:
        session = stripe.request("GET", f"/v1/checkout/sessions/{previous}", account=account)
        if session.get("status") == "complete":
            raise c.InvalidState("payment_in_progress") from None


def _checkout(event):
    ctx, item = _context(event)
    data = http.body(event, allowed={"embedded"}, required=False) or {}
    embedded = data.get("embedded", False)
    if not isinstance(embedded, bool):
        raise ValueError("embedded must be true or false")
    if item["status"] != c.SIGNED or item.get("customerType") != c.PRIVATE:
        raise c.InvalidState("not_payable")
    now = c.now_utc()
    _offer_still_valid(item, ctx.location, now)
    account = item.get("stripeAccountId")
    if not account or not account.startswith("acct_"):
        account = o.stripe_account(ctx.tenant, ctx.location)
    if not account:
        raise c.Unavailable("payment_unavailable")
    offer = c.load_offer(item["requestId"], item["signedOfferVersion"])
    base, slug = site.base(ctx.tenant_id, ctx.tenant)
    if not base:
        raise c.Unavailable("payment_unavailable")
    page = site.page(base, slug, f"/catering/{item['locationId']}/{item['requestId']}")
    join = "&" if slug else "?"
    attempt = int(item.get("checkoutAttempts") or 0) + 1
    _expire_previous(item)
    if embedded:
        payment_domains.ensure(account, lambda: site.hosts(ctx.tenant_id, ctx.tenant))
    session = cs.checkout(item, offer, account, f"{page}{join}betalning=klar", f"{page}{join}betalning=avbruten",
                          now, attempt, embedded=embedded)
    c.transact([
        c.head_update(item, sets=["checkoutSessionId = :s", "checkoutAttempts = :a", "updatedAt = :now",
                                  "stripeAccountId = :acct"],
                      values={":s": session["id"], ":a": attempt, ":now": c.iso(now), ":acct": account}),
        c.audit_put(item["requestId"], "checkout_started", "guest", now, session=session["id"]),
    ])
    if embedded:
        return http.respond(200, {"clientSecret": session["client_secret"], "stripeAccount": account})
    return http.respond(200, {"checkoutUrl": session["url"]})


def _change(event):
    ctx, item = _context(event)
    data = http.body(event, allowed={"message"})
    message = c.text(data.get("message"), "message", required=True, max_length=c.MESSAGE_MAX)
    if item["status"] != c.OFFER_SENT:
        raise c.InvalidState("not_changeable")
    now = c.now_utc()
    if item.get("signing"):
        bankid.cancel(item["signing"])
    c.transact([
        c.head_update(item, to_status=c.CHANGE_REQUESTED, sets=[
            "changeMessage = :m", "notice = :n", "updatedAt = :now"], removes=["signing"],
            values={":m": message, ":n": c.notice(c.N_CHANGE_REQUESTED, now), ":now": c.iso(now)}),
        c.audit_put(item["requestId"], "change_requested", "guest", now, message=message),
    ])
    return http.respond(200, _view_of(ctx, c.load(item["locationId"], item["requestId"])))


def _reject(event):
    ctx, item = _context(event)
    data = http.body(event, allowed={"reason"}, required=False)
    reason = c.text(data.get("reason"), "reason", max_length=500)
    if item["status"] not in (c.REQUESTED, c.OFFER_SENT, c.CHANGE_REQUESTED):
        raise c.InvalidState("not_rejectable")
    now = c.now_utc()
    if item.get("signing"):
        bankid.cancel(item["signing"])
    c.transact([
        c.head_update(item, to_status=c.REJECTED, sets=[
            "rejectReason = :r", "notice = :n", "updatedAt = :now", "capacityHeld = :zero"], removes=["signing"],
            values={":r": reason or "", ":n": c.notice(c.N_REJECTED, now), ":now": c.iso(now), ":zero": 0}),
        c.capacity_change(item["locationId"], c.settings_of(ctx.location), item["deliveryDate"],
                          -int(item.get("capacityHeld") or 0)),
        c.audit_put(item["requestId"], "rejected", "guest", now, reason=reason),
    ])
    return http.respond(200, _view_of(ctx, c.load(item["locationId"], item["requestId"])))


def _document(event):
    ctx, item = _context(event)
    if http.path_id(event, "docId") != "signature" or not (item.get("signature") or {}).get("evidenceKey"):
        raise c.NotFound
    url = boto3.client("s3").generate_presigned_url(
        "get_object", ExpiresIn=300,
        Params={"Bucket": os.environ["CATERING_DOCUMENTS_BUCKET_NAME"], "Key": item["signature"]["evidenceKey"]})
    return http.respond(200, {"url": url, "expiresIn": 300})


_HANDLERS = {"view": _view, "sign": _sign, "sign_status": _sign_status, "checkout": _checkout,
             "change": _change, "reject": _reject, "document": _document}


def handler(event, context):
    method, path = http.route(event)
    action = _ROUTES.get((method, path))
    if not action:
        return http.error(404, "not found")
    try:
        return _HANDLERS[action](event)
    except tenant.TenantError as exc:
        return exc.response()
    except c.NotFound:
        return http.error(404, "not found")
    except c.InvalidState as exc:
        return http.error(409, exc.code)
    except c.Unavailable as exc:
        return http.error(503, exc.code)
    except bankid.SigningRejected as exc:
        return http.error(409, f"signing_{exc.code}")
    except bankid.SigningUnavailable:
        logger.exception("signing unavailable")
        return http.error(503, "signing_unavailable")
    except stripe.StripeError as exc:
        logger.error(json.dumps({"stripe": "rejected", "code": exc.code, "message": str(exc)}))
        return http.error(502, "payment_provider_rejected")
    except stripe.StripeUnavailable:
        return http.error(503, "payment_unavailable")
    except ValueError as exc:
        return http.error(400, str(exc))
    except (BotoCoreError, ClientError, c.LinkKeyUnavailable):
        logger.exception("catering service unavailable")
        return http.error(503, "catering service unavailable")

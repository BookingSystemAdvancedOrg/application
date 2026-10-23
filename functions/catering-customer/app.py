"""catering-customer

TRIGGER:
    API Gateway -- Auth: NONE; every route needs the guest's magic-link
    token in the X-Order-Token header (shared/catering.py, HMAC over the
    request id and its linkVersion). A wrong token answers 404.

    GET  /locations/{locationId}/catering/requests/{requestId}/customer
    POST .../customer/sign             start signing the current offer version (after the restaurant):
                                       {"kind": "bankid", autoStartToken, qrData} or {"kind": "redirect", url}
    POST .../customer/sign/status      poll the signing (QR code, hints, result), also after the redirect back
    POST .../customer/accept           simple acceptance without BankID, where the restaurant allows it:
                                       {"name", "role"?, "confirmAuthority"?, "acceptTerms": true}
    POST .../customer/checkout         private guest: pay the signed offer (Stripe Checkout);
                                       {"embedded": true} -> {clientSecret, stripeAccount}
                                       for the site's Payment Element, else {checkoutUrl}
    POST .../customer/change           {"message"} ask the restaurant to change the offer
    POST .../customer/reject           {"reason"} decline the offer
    GET  .../customer/documents/{offer|signed|signature}   5-minute link to the offer PDF,
                                       the signed copy or the signature evidence

PURPOSE:
    The guest's side. The guest signs exactly one offer version, after the
    restaurant (shared/catering_signing.py, provider in shared/signing.py);
    what is signed is the SHA-256 of the offer PDF. On completion:
      - the signature evidence, the signed PDF and a manifest of hashes are
        stored under signed/ with a per-object lock (catering-documents)
      - offer_sent -> signed (notice catering_signed: restaurant + guest,
        with the link to the signed copy)
      - a company is then invoiced by catering-lifecycle (-> confirmed);
        a private guest pays here (Checkout -> catering-stripe-webhook ->
        confirmed). A failed or abandoned payment keeps the signature; the
        guest can pay again from the link until the offer's valid-until.
    Nothing here marks a request paid - only Stripe's webhook does.
    Signing and acceptance attempts are limited per request and per IP
    (shared/request_guard.py): each BankID order costs money.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    CATERING_REQUESTS_TABLE_NAME, CATERING_REQUEST_HISTORY_TABLE_NAME,
    CATERING_DOCUMENTS_BUCKET_NAME, LINK_SIGNING_KEY_SECRET_ARN,
    SIGNING_PROVIDER_SECRET_ARN, STRIPE_SECRET_ARN, STRIPE_API_VERSION, CUSTOMER_SITE_URL,
    REQUEST_GUARD_TABLE_NAME, DOCUMENT_LOCK_MODE, DOCUMENT_LOCK_DAYS
"""

import json
import logging
from datetime import date
from zoneinfo import ZoneInfo

from botocore.exceptions import BotoCoreError, ClientError

from shared import http, payment_domains, request_guard, signing, site, tenant
from shared import catering as c
from shared import catering_signing as sign
from shared import catering_stripe as cs
from shared import orders as o
from shared import stripe_client as stripe

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SIGN_REQUEST_LIMIT = (10, 60 * 60)  # signing starts / acceptances per request and hour
SIGN_IP_LIMIT = (20, 60 * 60)

_BASE = "/locations/{locationId}/catering/requests/{requestId}/customer"
_ROUTES = {
    ("GET", _BASE): "view",
    ("POST", _BASE + "/sign"): "sign",
    ("POST", _BASE + "/sign/status"): "sign_status",
    ("POST", _BASE + "/accept"): "accept",
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


def _guard(event, item, kind):
    request_guard.check([(f"catering-{kind}-request", item["requestId"], *SIGN_REQUEST_LIMIT),
                         (f"catering-{kind}-ip", http.source_ip(event), *SIGN_IP_LIMIT)], c.now_utc())


def _return_url(ctx, item):
    base, slug = site.base(ctx.tenant_id, ctx.tenant)
    if not base:
        return None
    page = site.page(base, slug, f"/catering/{item['locationId']}/{item['requestId']}")
    return f"{page}{'&' if slug else '?'}signering=klar"


def _sign(event):
    ctx, item = _context(event)
    http.body(event, allowed=set(), required=False)
    if item["status"] != c.OFFER_SENT:
        raise c.InvalidState("not_signable")
    _offer_still_valid(item, ctx.location, c.now_utc())
    _guard(event, item, "sign")
    return http.respond(200, {"status": "pending", **sign.begin(item, sign.CUSTOMER, http.source_ip(event),
                                                                 _return_url(ctx, item))})


def _sign_status(event):
    ctx, item = _context(event)
    http.body(event, allowed=set(), required=False)
    if item["status"] != c.OFFER_SENT or not (item.get("signing") or {}).get("order"):
        return http.respond(200, {"status": "none", "request": _view_of(ctx, item)})
    try:
        result = sign.poll(item, sign.CUSTOMER, http.source_ip(event))
    except sign.NotReady:
        return http.respond(200, {"status": "none", "request": _view_of(ctx, item)})
    if result["state"] == "pending":
        return http.respond(200, {"status": "pending", "hintCode": result.get("hint"), "qrData": result.get("qrData")})
    if result["state"] == "failed":
        return http.respond(200, {"status": "failed", "hintCode": result.get("hint")})
    logger.info(json.dumps({"catering": "signed", "requestId": item["requestId"]}))
    return http.respond(200, {"status": "complete",
                              "request": _view_of(ctx, c.load(item["locationId"], item["requestId"]))})


def _accept(event):
    ctx, item = _context(event)
    data = http.body(event, allowed={"name", "role", "confirmAuthority", "acceptTerms"})
    if not c.settings_of(ctx.location)["allowSimpleAcceptance"]:
        raise c.InvalidState("acceptance_not_allowed")
    if item["status"] != c.OFFER_SENT:
        raise c.InvalidState("not_signable")
    if data.get("acceptTerms") is not True:
        raise ValueError("acceptTerms must be true")
    signer = {"name": c.text(data.get("name"), "name", required=True, max_length=120)}
    if item.get("customerType") == c.COMPANY:
        if data.get("confirmAuthority") is not True:
            raise ValueError("confirmAuthority must be true")
        signer.update(role=c.text(data.get("role"), "role", required=True, max_length=80), authorityConfirmed=True)
    _offer_still_valid(item, ctx.location, c.now_utc())
    _guard(event, item, "accept")
    sign.accept(item, signer, http.source_ip(event), http.header(event, "user-agent"))
    return http.respond(200, _view_of(ctx, c.load(item["locationId"], item["requestId"])))


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
    signing.cancel((item.get("signing") or {}).get("order"))
    c.transact([
        c.head_update(item, to_status=c.CHANGE_REQUESTED, sets=[
            "changeMessage = :m", "notice = :n", "updatedAt = :now"], removes=["signing", "restaurantSignedAt"],
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
    signing.cancel((item.get("signing") or {}).get("order"))
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
    doc_id = http.path_id(event, "docId")
    if doc_id == "offer" and not sign.restaurant_signed(item):
        raise c.NotFound  # the guest sees a version only once the restaurant has signed it
    key, version = sign.document_ref(item, doc_id)
    if not key:
        raise c.NotFound
    return http.respond(200, {"url": sign.presign(key, version), "expiresIn": 300})


_HANDLERS = {"view": _view, "sign": _sign, "sign_status": _sign_status, "accept": _accept, "checkout": _checkout,
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
    except sign.NotReady as exc:
        return http.error(409, exc.code)
    except request_guard.Limited as exc:
        return http.respond(429, {"error": "too_many_requests"}, {"Retry-After": str(exc.retry_after)})
    except signing.SigningRejected as exc:
        return http.error(409, f"signing_{exc.code}")
    except signing.SigningUnavailable:
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

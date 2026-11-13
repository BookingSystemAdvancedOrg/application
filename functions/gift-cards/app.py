"""gift-cards

TRIGGER:
    API Gateway -- Auth: NONE (Turnstile + request guard on the POSTs)
    GET  /public/locations/{locationId}/gift-cards/offer           E1
    POST /public/locations/{locationId}/gift-cards/checkout        E2 PaymentIntent on the connected account
    POST /public/locations/{locationId}/gift-cards/balance         E3 any miss 404

    API Gateway -- Auth: JWT (tenant pool), feature `giftCards`
    GET  /tenant/gift-cards/settings                               E4 owner
    PUT  /tenant/gift-cards/settings                               E5 owner
    GET  /tenant/gift-cards?status=&cursor=                        E6 owner
    GET  /tenant/gift-cards/{cardId}                               E7 owner
    POST /tenant/gift-cards/{cardId}/adjust                        E8 owner: extend validity
    POST /tenant/gift-cards/{cardId}/refund                        E9 owner: Stripe refund of the unused part
    POST /tenant/gift-cards/{cardId}/resend                        E10 owner: a NEW code by email (old one stops)
    (E11-E13, the staff lookup and the Kassa redemptions, were removed with Kassa on 2026-11-14.)

    Lambda invoke (no requestContext): {"hook": ...} stripe.event,
    checkout.giftcard, accounting.events, privacy.export,
    privacy.erase, tenant.purge; EventBridge Scheduler {"job": "nightly"}
    (expiry at 02:30 Europe/Stockholm and the 24-month contact clean-up).

PURPOSE:
    Presentkort (ADR-E): sold on the restaurant site, issued only from
    Stripe's payment_intent.succeeded on the purchase's own connected
    account, redeemed online (order checkout). Balance
    changes are append-only TX rows written in one transaction with the
    card (txId idempotency). Codes exist only as HMAC + last4.
    Accounting: multi-purpose voucher, NEEDS ACCOUNTANT REVIEW.

ENV_VARS:
    ENVIRONMENT, GIFT_CARDS_TABLE_NAME, TENANT_TABLE_NAME, LOCATION_TABLE_NAME,
    LOCATION_ID_INDEX_NAME, USER_TABLE_NAME, GIFT_CARD_PEPPER_SECRET_ARN,
    STRIPE_SECRET_ARN, STRIPE_API_VERSION, TURNSTILE_REQUIRED,
    TURNSTILE_SECRET_ARN, REQUEST_GUARD_TABLE_NAME, NOTIFICATION_FUNCTION_NAME,
    EVENT_BUS_NAME, CLOUDFRONT_EDGE_SECRET
"""

import json
import logging
import os
from datetime import timedelta

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from shared import dynamo, events, http, orders, request_guard, tenant, turnstile
from shared import stripe_client as stripe
from shared.features import gift_cards as gc

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CHECKOUT_IP_LIMIT = (10, 600)
BALANCE_IP_LIMIT = (20, 600)
CODE_SEND_GRACE_SECONDS = 120
_clients = {}


def _client(name):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def _err(status, code, field=None):
    body = {"error": code, "code": code}
    if field:
        body["field"] = field
    return http.respond(status, body)


def _publish(detail_type, detail):
    try:
        events.publish(gc.FEATURE_ID, detail_type, detail)
    except Exception as exc:
        logger.warning(json.dumps({"event": detail_type, "publish": "failed", "type": type(exc).__name__}))


def _guard(event, kind, limit):
    try:
        request_guard.check([(kind, http.source_ip(event), *limit)], gc.now())
    except request_guard.Limited:
        raise gc.GiftCardError(429, "too_many_requests")


def _kr(ore) -> str:
    ore = int(ore)
    return f"{ore // 100} kr" if ore % 100 == 0 else f"{ore / 100:.2f}".replace(".", ",") + " kr"


def _date(iso_value) -> str:
    return str(iso_value)[:10]


# --- the code email (notification:live, CONTRACTS.md 9.4) -------------------------------------

class NotificationUnavailable(RuntimeError):
    """notification:live did not take the code email. A RuntimeError, so the Stripe replay path
    still fails the hook and Stripe retries; E10 answers 502 notification_unavailable."""


def _send_code(card, code, location, *, kind, to):
    name = (location.get("name") or "restaurangen").strip()
    who = f"Hej {card['recipientName']}!" if card.get("recipientName") else "Hej!"
    text = (f"{who}\n\nHär är ditt presentkort till {name}.\n\nKod: {gc.display_code(code)}\n"
            f"Värde: {_kr(card['balanceOre'])}\nGäller till och med {_date(card['expiresAt'])}.\n\n"
            + (f"Meddelande: {card['message']}\n\n" if card.get("message") else "")
            + "Visa koden i restaurangen eller ange den när du beställer på hemsidan. "
              "Spara mejlet: den som har koden kan använda presentkortet.\n")
    notice = {"version": 1, "kind": kind, "tenantId": card["tenantId"], "locationId": card.get("soldAtLocationId"),
              "channel": "email", "to": to, "subject": f"Presentkort till {name}", "text": text,
              "replyTo": "location"}
    try:
        resp = _client("lambda").invoke(FunctionName=os.environ["NOTIFICATION_FUNCTION_NAME"],
                                        InvocationType="Event", Payload=json.dumps({"notice": notice}).encode("utf-8"))
    except (BotoCoreError, ClientError) as exc:
        raise NotificationUnavailable(f"notification invoke failed ({type(exc).__name__})") from exc
    if (resp or {}).get("StatusCode") != 202:
        raise NotificationUnavailable(f"notification invoke returned {(resp or {}).get('StatusCode')}")
    # Stamped only once notification has the email: a webhook replay resends when it is missing.
    try:
        gc.table().update_item(Key=gc.card_key(card["tenantId"], card["cardId"]),
                               UpdateExpression="SET codeSentAt = :at", ConditionExpression="attribute_exists(PK)",
                               ExpressionAttributeValues={":at": gc.iso(gc.now())})
    except ClientError as exc:
        if not gc.conditional_failed(exc):
            raise


def _rotate_code(tenant_id, card):
    """The code is not stored: a new code replaces the old one (which stops
    working at once). Returns (card, code)."""
    code = gc.new_code()
    new_hmac = gc.code_hmac(tenant_id, code)
    name = os.environ["GIFT_CARDS_TABLE_NAME"]
    try:
        dynamo.client().transact_write_items(TransactItems=[
            {"Put": {"TableName": name, "Item": gc._ser({**gc.code_key(tenant_id, new_hmac),
                                                         "tenantId": tenant_id, "cardId": card["cardId"]}),
                     "ConditionExpression": "attribute_not_exists(PK)"}},
            {"Delete": {"TableName": name, "Key": gc._ser(gc.code_key(tenant_id, card["codeHmac"]))}},
            {"Update": {"TableName": name, "Key": gc._ser(gc.card_key(tenant_id, card["cardId"])),
                        "UpdateExpression": "SET codeHmac = :h, last4 = :l, version = version + :one",
                        "ConditionExpression": "version = :v",
                        "ExpressionAttributeValues": gc._ser({":h": new_hmac, ":l": code[-4:], ":one": 1,
                                                             ":v": int(card["version"])})}},
        ])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        raise gc.GiftCardError(409, "changed_retry")
    return gc.get_card(tenant_id, card["cardId"]), code


# --- public E1-E3 ------------------------------------------------------------------------------

def _public_settings(ctx):
    settings = gc.get_settings(ctx.tenant_id)
    if not settings.get("enabled"):
        raise gc.GiftCardError(404, "not found")
    return settings


def offer(event):
    ctx = tenant.for_public(http.path_id(event, "locationId"), feature=gc.FEATURE_KEY)
    view = gc.settings_view(_public_settings(ctx))
    return http.respond(200, {k: view[k] for k in ("amountsSek", "customMinSek", "customMaxSek", "validityMonths")})


def checkout(event):
    location_id = http.path_id(event, "locationId")
    ctx = tenant.for_public(location_id, feature=gc.FEATURE_KEY)
    body = http.body(event, allowed=("purchaseId", "amountSek", "buyerEmail", "recipientName", "recipientEmail",
                                     "message", "turnstileToken"))
    settings = _public_settings(ctx)
    purchase_id = body.get("purchaseId")
    if not gc.valid_id(purchase_id):
        raise gc.GiftCardError(400, "purchaseId is invalid", "purchaseId")
    amount = body.get("amountSek")
    if isinstance(amount, bool) or not isinstance(amount, int) or not (
            amount in [int(a) for a in settings["amountsSek"]]
            or int(settings["customMinSek"]) <= amount <= int(settings["customMaxSek"])):
        raise gc.GiftCardError(400, "invalid_amount", "amountSek")
    if not gc.valid_email(body.get("buyerEmail")):
        raise gc.GiftCardError(400, "invalid_email", "buyerEmail")
    if body.get("recipientEmail") not in (None, "") and not gc.valid_email(body["recipientEmail"]):
        raise gc.GiftCardError(400, "invalid_email", "recipientEmail")
    message = body.get("message")
    if message is not None and (not isinstance(message, str) or len(message) > gc.MESSAGE_MAX):
        raise gc.GiftCardError(400, "message_too_long", "message")
    recipient_name = body.get("recipientName")
    if recipient_name is not None and (not isinstance(recipient_name, str) or len(recipient_name) > 80):
        raise gc.GiftCardError(400, "recipientName must be at most 80 characters", "recipientName")
    _guard(event, "giftcard-ip", CHECKOUT_IP_LIMIT)
    if turnstile.required():
        try:
            turnstile.verify(body.get("turnstileToken"), http.source_ip(event))
        except ValueError:
            raise gc.GiftCardError(400, "turnstile_failed", "turnstileToken")
    account = orders.stripe_account(ctx.tenant, ctx.location)
    if not account:
        raise gc.GiftCardError(409, "no_stripe_account")
    existing = gc.table().get_item(Key=gc.purchase_key(ctx.tenant_id, purchase_id), ConsistentRead=True).get("Item")
    if existing and (existing.get("locationId") != location_id or int(existing["amountOre"]) != amount * 100
                     or existing.get("stripeAccountId") != account):
        raise gc.GiftCardError(409, "changed_retry")
    buyer = body["buyerEmail"].strip().lower()
    try:
        if existing:
            # A retry: the stored PaymentIntent, not a new one (Stripe idempotency keys expire after 24 h).
            intent = stripe.request("GET", f"/v1/payment_intents/{existing['paymentIntentId']}", account=account)
        else:
            intent = stripe.request("POST", "/v1/payment_intents", {
                "amount": amount * 100, "currency": "sek", "automatic_payment_methods": {"enabled": "true"},
                "description": f"Presentkort {(ctx.location.get('name') or '')[:60]}".strip(), "receipt_email": buyer,
                "metadata": {"kind": "giftcard", "tenantId": ctx.tenant_id, "locationId": location_id,
                             "purchaseId": purchase_id},
            }, account=account, idempotency_key=f"giftcard-{purchase_id}")
    except (stripe.StripeError, stripe.StripeUnavailable) as exc:
        logger.warning(json.dumps({"giftcard": "stripe_failed", "type": type(exc).__name__}))
        raise gc.GiftCardError(502, "stripe_unavailable")
    at = gc.now()
    expires = gc.add_months(at, int(settings["validityMonths"]))
    if not existing:
        item = {**gc.purchase_key(ctx.tenant_id, purchase_id), "tenantId": ctx.tenant_id, "purchaseId": purchase_id,
                "locationId": location_id, "amountOre": amount * 100, "paymentIntentId": intent["id"],
                "stripeAccountId": account, "buyerEmail": buyer,
                "validityMonths": int(settings["validityMonths"]), "status": "pending", "createdAt": gc.iso(at),
                "ttl": int((at + timedelta(days=gc.PURCHASE_TTL_DAYS)).timestamp())}
        for field, value in (("recipientName", recipient_name), ("message", message),
                             ("recipientEmail", (body.get("recipientEmail") or "").strip().lower() or None)):
            if value:
                item[field] = value.strip() if field != "recipientEmail" else value
        try:
            gc.table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
        except ClientError as exc:
            if not gc.conditional_failed(exc):
                raise
    return http.respond(200, {"clientSecret": intent["client_secret"], "amountSek": amount,
                              "expiresAt": gc.iso(expires), "stripeAccount": account})


def balance(event):
    location_id = http.path_id(event, "locationId")
    ctx = tenant.for_public(location_id, feature=gc.FEATURE_KEY)
    body = http.body(event, allowed=("code", "turnstileToken"))
    _guard(event, "giftcard-balance-ip", BALANCE_IP_LIMIT)
    if turnstile.required():
        try:
            turnstile.verify(body.get("turnstileToken"), http.source_ip(event))
        except ValueError:
            raise gc.GiftCardError(400, "turnstile_failed", "turnstileToken")
    card = gc.find_card_by_code(ctx.tenant_id, body.get("code"))
    if not card:
        raise gc.GiftCardError(404, "not found")
    return http.respond(200, {"balanceSek": int(card["balanceOre"]) // 100, "balanceOre": int(card["balanceOre"]),
                              "expiresAt": card["expiresAt"], "status": gc.shown_status(card)})


# --- owner E4-E10 -----------------------------------------------------------------------------

def _owner(ctx):
    if not ctx.is_owner:
        raise gc.GiftCardError(403, "owner_only")


def get_settings(event, ctx):
    _owner(ctx)
    return http.respond(200, gc.settings_view(gc.get_settings(ctx.tenant_id)))


def put_settings(event, ctx):
    _owner(ctx)
    body = http.body(event, allowed=("enabled", "amountsSek", "customMinSek", "customMaxSek", "validityMonths",
                                     "version"))
    clean = gc.validate_settings(body)
    item = {**gc.settings_key(ctx.tenant_id), "tenantId": ctx.tenant_id,
            **{k: clean[k] for k in ("enabled", "amountsSek", "customMinSek", "customMaxSek", "validityMonths")},
            "version": clean["version"] + 1, "updatedAt": gc.iso(gc.now()), "updatedBy": ctx.sub}
    condition = ({"ConditionExpression": "attribute_not_exists(PK)"} if clean["version"] == 0 else
                 {"ConditionExpression": "version = :v", "ExpressionAttributeValues": {":v": clean["version"]}})
    try:
        gc.table().put_item(Item=item, **condition)
    except ClientError as exc:
        if not gc.conditional_failed(exc):
            raise
        raise gc.GiftCardError(409, "changed_retry")
    return http.respond(200, gc.settings_view(item))


def list_cards(event, ctx):
    _owner(ctx)
    params = http.query(event)
    unknown = set(params) - {"status", "cursor"}
    if unknown:
        raise gc.GiftCardError(400, f"unsupported query parameters: {', '.join(sorted(unknown))}")
    status = params.get("status")
    if status and status not in gc.STATUSES:
        raise gc.GiftCardError(400, "bad status", "status")
    cards = sorted(gc.all_cards(ctx.tenant_id), key=lambda c: c.get("purchasedAt") or "", reverse=True)
    liability = sum(int(c["balanceOre"]) for c in cards if gc.shown_status(c) == "active")
    if status:
        cards = [c for c in cards if gc.shown_status(c) == status]
    start = 0
    if params.get("cursor"):
        key = gc.decode_cursor(params["cursor"], ctx.tenant_id)
        start = next((i + 1 for i, c in enumerate(cards) if c["SK"] == key["SK"]), len(cards))
    page = cards[start:start + gc.PAGE]
    cursor = gc.encode_cursor({"PK": page[-1]["PK"], "SK": page[-1]["SK"]}) \
        if start + gc.PAGE < len(cards) and page else None
    return http.respond(200, {"cards": [gc.card_summary(c) for c in page], "liabilitySek": liability // 100,
                              "liabilityOre": liability, "cursor": cursor})


def _card_or_404(ctx, event):
    try:
        card_id = http.path_id(event, "cardId")
    except ValueError:
        raise gc.GiftCardError(404, "not found")
    card = gc.get_card(ctx.tenant_id, card_id)
    if not card:
        raise gc.GiftCardError(404, "not found")
    return card


def get_card(event, ctx):
    _owner(ctx)
    card = _card_or_404(ctx, event)
    return http.respond(200, {**gc.card_detail(card),
                              "transactions": [gc.tx_view(t) for t in gc.transactions(ctx.tenant_id, card["cardId"])]})


def _tx_id(body):
    tx_id = body.get("txId")
    if not gc.valid_id(tx_id):
        raise gc.GiftCardError(400, "txId is invalid", "txId")
    return tx_id


def _reason(body):
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
        raise gc.GiftCardError(400, "reason is required", "reason")
    return reason.strip()


def adjust(event, ctx):
    _owner(ctx)
    body = http.body(event, allowed=("txId", "expiresAt", "reason"))
    card = _card_or_404(ctx, event)
    tx_id, reason = _tx_id(body), _reason(body)
    if gc.get_tx(ctx.tenant_id, card["cardId"], tx_id):
        return http.respond(200, gc.card_detail(card))
    new_expiry = gc.parse_iso(body.get("expiresAt"))
    if not new_expiry or gc.iso(new_expiry) <= card["expiresAt"]:
        raise gc.GiftCardError(400, "expiresAt must be later than the current expiry", "expiresAt")
    if card["status"] != "active":
        raise gc.GiftCardError(409, "card_void" if card["status"] == "void" else "card_expired")
    at = gc.now()
    tx = gc.tx_item(ctx.tenant_id, card, tx_id, "adjust", 0, int(card["balanceOre"]), "admin",
                    card["soldAtLocationId"], at, by=ctx.sub, reason=reason)
    expires = gc.iso(new_expiry)
    name = os.environ["GIFT_CARDS_TABLE_NAME"]
    gsi = gc.expiry_gsi(ctx.tenant_id, card["cardId"], expires)
    try:
        dynamo.client().transact_write_items(TransactItems=[
            {"Update": {"TableName": name, "Key": gc._ser(gc.card_key(ctx.tenant_id, card["cardId"])),
                        "UpdateExpression": "SET expiresAt = :e, GSI2PK = :p, GSI2SK = :s, version = version + :one",
                        "ConditionExpression": "version = :v AND #s = :active",
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": gc._ser({":e": expires, ":p": gsi["GSI2PK"], ":s": gsi["GSI2SK"],
                                                             ":one": 1, ":v": int(card["version"]),
                                                             ":active": "active"})}},
            {"Put": {"TableName": name, "Item": gc._ser(tx), "ConditionExpression": "attribute_not_exists(PK)"}},
        ])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        raise gc.GiftCardError(409, "changed_retry")
    return http.respond(200, gc.card_detail(gc.get_card(ctx.tenant_id, card["cardId"])))


def refund(event, ctx):
    """The card first (refund TX for the balance read, version condition, so a
    redemption in between makes it 409 changed_retry), then Stripe for exactly
    that amount (key giftcard-refund-<txId>). A Stripe failure leaves the TX
    without stripeRefundId; a retry with the same txId finishes the refund."""
    _owner(ctx)
    body = http.body(event, allowed=("txId", "reason"))
    card = _card_or_404(ctx, event)
    tx_id, reason = _tx_id(body), _reason(body)
    done = gc.get_tx(ctx.tenant_id, card["cardId"], tx_id)
    if done:
        if done.get("kind") == "refund":
            _stripe_refund(ctx.tenant_id, card, done)
        return http.respond(200, gc.card_detail(gc.get_card(ctx.tenant_id, card["cardId"])))
    if card["status"] == "void":
        raise gc.GiftCardError(409, "card_void")
    purchased = gc.parse_iso(card.get("purchasedAt"))
    if not purchased or gc.now() > purchased + timedelta(days=gc.REFUND_DAYS):
        raise gc.GiftCardError(409, "refund_window_passed")
    amount = int(card["balanceOre"])
    card, tx, created = gc.change_balance(ctx.tenant_id, card["cardId"], tx_id, -amount, "refund", "admin",
                                          card["soldAtLocationId"], by=ctx.sub, reason=reason, allow_expired=True,
                                          expect_version=int(card["version"]))
    if created:
        _publish("GiftCard.Refunded", {"tenantId": ctx.tenant_id, "cardId": card["cardId"], "amountOre": amount})
    _stripe_refund(ctx.tenant_id, card, tx)
    return http.respond(200, gc.card_detail(card))


def _stripe_refund(tenant_id, card, tx):
    amount = -int(tx["amountOre"])
    if amount <= 0 or tx.get("stripeRefundId"):
        return
    try:
        result = stripe.request("POST", "/v1/refunds", {"payment_intent": card["paymentIntentId"], "amount": amount,
                                                        "metadata": {"kind": "giftcard", "tenantId": tenant_id,
                                                                     "cardId": card["cardId"]}},
                                account=card["stripeAccountId"], idempotency_key=f"giftcard-refund-{tx['txId']}")
    except (stripe.StripeError, stripe.StripeUnavailable):
        logger.warning(json.dumps({"event": "giftcard.refund", "stripe": "failed", "cardId": card["cardId"],
                                   "txId": tx["txId"]}))
        raise gc.GiftCardError(502, "stripe_unavailable")
    gc.table().update_item(Key=gc.tx_key(tenant_id, card["cardId"], tx["txId"]),
                           UpdateExpression="SET stripeRefundId = :r", ConditionExpression="attribute_exists(PK)",
                           ExpressionAttributeValues={":r": (result or {}).get("id") or "unknown"})


def resend(event, ctx):
    """The code is not stored, so "send again" rotates it: a new code by
    email, the old one stops working at once."""
    _owner(ctx)
    body = http.body(event, allowed=("to",))
    card = _card_or_404(ctx, event)
    to = body.get("to")
    if to not in ("buyer", "recipient"):
        raise gc.GiftCardError(400, "to must be buyer or recipient", "to")
    address = card.get("buyerEmail") if to == "buyer" else card.get("recipientEmail")
    if not address:
        raise gc.GiftCardError(400, "no email for that target", "to")
    if gc.shown_status(card) != "active":
        raise gc.GiftCardError(409, "card_expired" if gc.shown_status(card) == "expired" else "card_void")
    card, code = _rotate_code(ctx.tenant_id, card)
    location = tenant.get_location(ctx.tenant_id, card["soldAtLocationId"]) or {}
    try:
        _send_code(card, code, location, kind="gift-cards.code-resend", to=address)
    except NotificationUnavailable as exc:
        # The new code is in place but unsent: sending again rotates once more, so a retry is safe.
        logger.error(json.dumps({"route": "resend", "notification": str(exc)}))
        raise gc.GiftCardError(502, "notification_unavailable")
    return http.respond(202, {"last4": card["last4"]})


def _reverse(tenant_id, tx_id, reverse_id, location_id, *, by):
    """A redemption back on the card (an online order cancelled or refunded)."""
    card_id = gc.tx_card(tenant_id, tx_id)
    tx = gc.get_tx(tenant_id, card_id, tx_id) if card_id else None
    if not tx or tx.get("kind") != "redeem":
        return None, 0
    amount = -int(tx["amountOre"])
    card, rev, created = gc.change_balance(tenant_id, card_id, reverse_id, amount, "reverse", tx["channel"],
                                           location_id or tx["locationId"], ref={**(tx.get("ref") or {})}, by=by,
                                           allow_expired=True)
    if created:
        _publish("GiftCard.RedemptionReversed", {"tenantId": tenant_id, "cardId": card_id,
                                                 "locationId": tx["locationId"], "amountOre": amount, "txId": tx_id})
    return card, int(rev["balanceAfterOre"])


# --- Stripe: issue only from the purchase's own account -------------------------------------

def _code_to(card):
    return card.get("recipientEmail") or card.get("buyerEmail")


def _resend_unsent(tenant_id, card):
    """A webhook replay for an issued card whose code email never reached
    notification (the first attempt failed after the write): send it now.
    The code is not stored, so it is rotated; nobody has seen the old one."""
    if card.get("codeSentAt") or gc.shown_status(card) != "active" or not _code_to(card):
        return "already_issued"
    purchased = gc.parse_iso(card.get("purchasedAt"))
    if purchased and gc.now() < purchased + timedelta(seconds=CODE_SEND_GRACE_SECONDS):
        return "already_issued"  # the first attempt may still be sending
    try:
        card, code = _rotate_code(tenant_id, card)
    except gc.GiftCardError:
        return "already_issued"  # a concurrent change; the next replay tries again
    location = tenant.get_location(tenant_id, card["soldAtLocationId"]) or {}
    _send_code(card, code, location, kind="gift-cards.code", to=_code_to(card))
    return "code_resent"


def _issue(tenant_id, purchase, intent):
    card_id = gc.card_id_for(purchase["purchaseId"])
    existing = gc.get_card(tenant_id, card_id)
    if existing:
        return _resend_unsent(tenant_id, existing)
    location = tenant.get_location(tenant_id, purchase["locationId"]) or {}
    at = gc.now()
    expires = gc.iso(gc.add_months(at, int(purchase.get("validityMonths") or gc.DEFAULT_VALIDITY)))
    name = os.environ["GIFT_CARDS_TABLE_NAME"]
    for _ in range(3):
        code = gc.new_code()
        card = {**gc.card_key(tenant_id, card_id), "tenantId": tenant_id, "cardId": card_id,
                "codeHmac": gc.code_hmac(tenant_id, code), "last4": code[-4:], "initialOre": int(purchase["amountOre"]),
                "balanceOre": int(purchase["amountOre"]), "status": "active",
                "soldAtLocationId": purchase["locationId"], "purchaseId": purchase["purchaseId"],
                "paymentIntentId": intent["id"], "stripeAccountId": purchase["stripeAccountId"],
                "purchasedAt": gc.iso(at), "expiresAt": expires, "buyerEmail": purchase["buyerEmail"],
                "buyerEmailHash": gc.email_hash(tenant_id, purchase["buyerEmail"]), "version": 1,
                **gc.expiry_gsi(tenant_id, card_id, expires)}
        for field in ("recipientName", "recipientEmail", "message"):
            if purchase.get(field):
                card[field] = purchase[field]
        if purchase.get("recipientEmail"):
            card["recipientEmailHash"] = gc.email_hash(tenant_id, purchase["recipientEmail"])
        tx = gc.tx_item(tenant_id, card, f"issue-{purchase['purchaseId']}", "issue", int(purchase["amountOre"]),
                        int(purchase["amountOre"]), "online", purchase["locationId"], at,
                        ref={"paymentIntentId": intent["id"]})
        try:
            dynamo.client().transact_write_items(TransactItems=[
                {"Put": {"TableName": name, "Item": gc._ser(card), "ConditionExpression": "attribute_not_exists(PK)"}},
                {"Put": {"TableName": name, "Item": gc._ser({**gc.code_key(tenant_id, card["codeHmac"]),
                                                             "tenantId": tenant_id, "cardId": card_id}),
                         "ConditionExpression": "attribute_not_exists(PK)"}},
                {"Put": {"TableName": name, "Item": gc._ser(tx), "ConditionExpression": "attribute_not_exists(PK)"}},
                {"Update": {"TableName": name, "Key": gc._ser(gc.purchase_key(tenant_id, purchase["purchaseId"])),
                            "UpdateExpression": "SET #s = :issued, cardId = :c REMOVE #ttl",
                            # pending, or failed: a declined attempt the guest then retried on the same intent
                            "ConditionExpression": "#s IN (:pending, :failed)",
                            "ExpressionAttributeNames": {"#s": "status", "#ttl": "ttl"},
                            "ExpressionAttributeValues": gc._ser({":issued": "issued", ":c": card_id,
                                                                 ":pending": "pending", ":failed": "failed"})}},
            ])
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
            if gc.get_card(tenant_id, card_id):
                return "already_issued"
            continue  # a code collision (80 bits): draw again
        _send_code(card, code, location, kind="gift-cards.code", to=_code_to(card))
        _publish("GiftCard.Sold", {"tenantId": tenant_id, "cardId": card_id, "locationId": purchase["locationId"],
                                   "amountOre": int(purchase["amountOre"]), "expiresAt": expires})
        return "issued"
    raise RuntimeError("could not issue a unique gift card code")


def stripe_event(event):
    tenant_id, kind, account, obj = event.get("tenantId"), event.get("type"), event.get("account"), \
        event.get("object") or {}
    meta = obj.get("metadata") or {}
    purchase_id = meta.get("purchaseId")
    if not gc.valid_id(purchase_id) or meta.get("tenantId") != tenant_id:
        return "ignored:unknown_purchase"
    purchase = gc.table().get_item(Key=gc.purchase_key(tenant_id, purchase_id), ConsistentRead=True).get("Item")
    if not purchase:
        return "ignored:unknown_purchase"
    # Another restaurant's connected account can never issue, refund or void this restaurant's cards.
    if not account or account != purchase.get("stripeAccountId"):
        return "ignored:account_mismatch"
    if kind == "payment_intent.succeeded":
        if obj.get("id") != purchase.get("paymentIntentId") \
                or int(obj.get("amount") or 0) != int(purchase["amountOre"]):
            return "ignored:mismatch"
        return _issue(tenant_id, purchase, obj)
    if kind == "payment_intent.payment_failed":
        try:
            gc.table().update_item(Key=gc.purchase_key(tenant_id, purchase_id), UpdateExpression="SET #s = :f",
                                   ConditionExpression="#s = :p", ExpressionAttributeNames={"#s": "status"},
                                   ExpressionAttributeValues={":f": "failed", ":p": "pending"})
        except ClientError as exc:
            if not gc.conditional_failed(exc):
                raise
        return "failed"
    if kind in ("charge.refunded", "charge.dispute.closed"):
        if kind == "charge.dispute.closed" and obj.get("status") != "lost":
            return "ignored:dispute_won"
        card = gc.get_card(tenant_id, gc.card_id_for(purchase_id))
        if not card or card["status"] == "void":
            return "already_void"
        refunded, charged = int(obj.get("amount_refunded") or 0), int(obj.get("amount") or 0)
        if kind == "charge.refunded" and refunded != charged:
            # A partial refund made in Stripe: the guest still holds the card, so it is flagged on the card
            # for the owner to settle by hand (E8 adjust or E9 refund), never voided (ADR-E 9).
            logger.warning(json.dumps({"event": "giftcard.partial_refund", "tenantId": tenant_id,
                                       "cardId": card["cardId"], "chargeId": obj.get("id"),
                                       "amountRefundedOre": refunded, "amountOre": charged}))
            gc.table().update_item(Key=gc.card_key(tenant_id, card["cardId"]),
                                   UpdateExpression="SET partialRefundOre = :r, partialRefundAt = :at",
                                   ConditionExpression="attribute_exists(PK)",
                                   ExpressionAttributeValues={":r": refunded, ":at": gc.iso(gc.now())})
            return "flagged:partial_refund"
        _, _, created = gc.change_balance(tenant_id, card["cardId"], f"void-{obj.get('id')}", -int(card["balanceOre"]),
                                          "void", "system", card["soldAtLocationId"], allow_expired=True)
        return "voided" if created else "already_void"
    return "ignored:type"


# --- hooks ---------------------------------------------------------------------------------------

def checkout_hook(event):
    tenant_id, op, order_id = event["tenantId"], event.get("op"), event.get("orderId")
    if not isinstance(order_id, str) or not order_id:
        return {"ok": False, "status": 400, "code": "orderId_required"}
    if op == "reserve":
        total = event.get("totalOre")
        card = gc.find_card_by_code(tenant_id, event.get("code"))
        if not card:
            return {"ok": False, "status": 404, "code": "gift_card_not_found"}
        done = gc.get_tx(tenant_id, card["cardId"], f"order-{order_id}")
        if done:  # a retry of the same order: the stored debit
            return {"ok": True, "debitedOre": -int(done["amountOre"]), "cardId": card["cardId"],
                    "cardLast4": card["last4"]}
        status = gc.shown_status(card)
        if status in ("expired", "void"):
            return {"ok": False, "status": 409, "code": f"card_{status}"}
        amount = min(int(card["balanceOre"]), int(total or 0))
        if amount <= 0:
            return {"ok": False, "status": 409, "code": "insufficient_balance"}
        card, tx, created = gc.change_balance(tenant_id, card["cardId"], f"order-{order_id}", -amount, "redeem",
                                              "online", event.get("locationId"), ref={"orderId": order_id})
        if created:
            _publish("GiftCard.Redeemed", {"tenantId": tenant_id, "cardId": card["cardId"],
                                           "locationId": tx["locationId"], "amountOre": amount,
                                           "balanceAfterOre": int(tx["balanceAfterOre"]), "channel": "online",
                                           "txId": tx["txId"]})
        return {"ok": True, "debitedOre": -int(tx["amountOre"]), "cardId": card["cardId"], "cardLast4": card["last4"]}
    if op in ("reverse", "refund"):
        # One credit per order whichever path asks (a failed order or a staff cancel after payment).
        other = "refund" if op == "reverse" else "reverse"
        card_id = gc.tx_card(tenant_id, f"order-{order_id}")
        if card_id and gc.get_tx(tenant_id, card_id, f"{other}-order-{order_id}"):
            return {"ok": True, "reversedOre": 0}
        card, after = _reverse(tenant_id, f"order-{order_id}", f"{op}-order-{order_id}", event.get("locationId"),
                               by="system")
        if not card:
            return {"ok": True, "reversedOre": 0}
        tx = gc.get_tx(tenant_id, card["cardId"], f"{op}-order-{order_id}")
        return {"ok": True, "reversedOre": int(tx["amountOre"]) if tx else 0}
    return {"ok": False, "status": 400, "code": "unknown_op"}


T_SOLD = "Sålda presentkort"
T_ONLINE_REDEEMED = "Presentkort i onlinebeställningar"
T_EXPIRED = "Presentkort som gått ut"
T_REFUNDED = "Återbetalda presentkort"
T_ORDER_REFUND = "Presentkort tillbaka vid återbetald beställning"


def accounting_hook(event):
    """Postings per day from the TX rows (byLocationDay). Online orders are booked in full to Stripe by the order export, so the
    card's part is moved from Stripe to 2421 here. NEEDS ACCOUNTANT REVIEW."""
    tenant_id, location_id, first, last = event["tenantId"], event.get("locationId"), event.get("first"), \
        event.get("last")
    out = []
    for day in gc.days_between(first, last):
        rows = gc.table().query(IndexName=gc.GSI1, KeyConditionExpression=Key("GSI1PK").eq(
            f"TENANT#{tenant_id}#LOC#{location_id}#{day}")).get("Items") or []
        sold = online = expired = refunded = order_refund = 0
        by_order = {}
        for tx in rows:
            amount, kind, channel = int(tx["amountOre"]), tx["kind"], tx.get("channel")
            if kind == "issue":
                sold += amount
            elif kind == "expire":
                expired += -amount
            elif kind in ("refund", "void"):
                refunded += -amount
            elif channel == "online" and kind in ("redeem", "reverse"):
                order_id = (tx.get("ref") or {}).get("orderId")
                if str(tx["txId"]).startswith("refund-order-"):
                    order_refund += amount
                else:
                    by_order[order_id] = by_order.get(order_id, 0) - amount
        online = sum(v for v in by_order.values() if v > 0)
        postings, warnings = [], []
        if sold:
            postings += [{"role": "stripe", "amount": sold, "text": T_SOLD},
                         {"role": "giftCardLiability", "amount": -sold, "text": T_SOLD}]
        if online:
            postings += [{"role": "giftCardLiability", "amount": online, "text": T_ONLINE_REDEEMED},
                         {"role": "stripe", "amount": -online, "text": T_ONLINE_REDEEMED}]
        if expired:
            postings += [{"role": "giftCardLiability", "amount": expired, "text": T_EXPIRED},
                         {"role": "giftCardBreakage", "amount": -expired, "text": T_EXPIRED}]
        if refunded:
            postings += [{"role": "giftCardLiability", "amount": refunded, "text": T_REFUNDED},
                         {"role": "stripe", "amount": -refunded, "text": T_REFUNDED}]
        if order_refund:
            postings += [{"role": "unclassified", "amount": order_refund, "text": T_ORDER_REFUND},
                         {"role": "giftCardLiability", "amount": -order_refund, "text": T_ORDER_REFUND}]
            warnings.append("Återbetald beställning delvis betald med presentkort: momsen på presentkortsdelen "
                            "är bokad på OBS-konto, kontrollera.")
        if postings:
            out.append({"day": day, "postings": postings, "warnings": warnings})
    return {"events": out}


def _cards_for(tenant_id, event):
    hashes = {gc.email_hash(tenant_id, event[k]) for k in ("email",) if isinstance(event.get(k), str) and event[k]}
    if not hashes:
        return []
    return [c for c in gc.all_cards(tenant_id) if c.get("buyerEmailHash") in hashes
            or c.get("recipientEmailHash") in hashes]


CONTACT_FIELDS = ("buyerEmail", "recipientName", "recipientEmail", "message")
CARD_CONTACT_FIELDS = CONTACT_FIELDS + ("buyerEmailHash", "recipientEmailHash")  # the hashes find a person too


def _strip_contact(card):
    gc.table().update_item(Key=gc.card_key(card["tenantId"], card["cardId"]),
                           UpdateExpression="REMOVE " + ", ".join(CARD_CONTACT_FIELDS))
    try:
        gc.table().update_item(Key=gc.purchase_key(card["tenantId"], card["purchaseId"]),
                               UpdateExpression="REMOVE " + ", ".join(CONTACT_FIELDS),
                               ConditionExpression="attribute_exists(PK)")
    except ClientError as exc:
        if not gc.conditional_failed(exc):
            raise


def privacy_hook(event, erase):
    cards = _cards_for(event["tenantId"], event)
    if not erase:
        mine = gc.email_hash(event["tenantId"], event.get("email") or "")
        return {"items": [{"kind": "gift_card", "cardId": c["cardId"], "last4": c["last4"],
                           "balanceOre": int(c["balanceOre"]), "status": gc.shown_status(c),
                           "expiresAt": c["expiresAt"], "role": "buyer" if c.get("buyerEmailHash") == mine
                           else "recipient"} for c in cards]}
    for card in cards:
        _strip_contact(card)
    # The card and its balance stay valid (bearer instrument, bookkeeping record).
    return {"erased": {"giftCardContacts": len(cards)}, "kept": {"giftCards": len(cards)}}


def purge_hook(event):
    tenant_id = event["tenantId"]
    rows = []
    kwargs = {"KeyConditionExpression": Key("PK").eq(gc.pk(tenant_id))}
    while True:
        page = gc.table().query(**kwargs)
        rows.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    last_tx = max([r.get("at") for r in rows if "#TX#" in r["SK"]] or [gc.iso(gc.now())])
    keep_until = int(gc.parse_iso(last_tx).replace(month=12, day=31, hour=23, minute=59).timestamp()) \
        + (gc.BOOKKEEPING_YEARS * 366 * 86400)
    deleted = kept = 0
    for row in rows:
        sk = row["SK"]
        if sk == "SETTINGS" or sk.startswith("PURCHASE#"):
            gc.table().delete_item(Key={"PK": row["PK"], "SK": sk})
            deleted += 1
            continue
        expr = "SET #ttl = :t"
        if sk.startswith("TXREF#"):
            expr += " REMOVE GSI2PK, GSI2SK"  # the nightly job must not reverse a purged tenant's redemptions
        if sk.startswith("CARD#") and "#TX#" not in sk:
            expr += " REMOVE " + ", ".join(CARD_CONTACT_FIELDS) + ", GSI2PK, GSI2SK"
        gc.table().update_item(Key={"PK": row["PK"], "SK": sk}, UpdateExpression=expr,
                               ExpressionAttributeNames={"#ttl": "ttl"}, ExpressionAttributeValues={":t": keep_until})
        kept += 1
    return {"done": True, "counts": {"deleted": deleted, "keptUntilBookkeepingEnds": kept}}


def _hook(event):
    hook, tenant_id = event.get("hook"), event.get("tenantId")
    if not isinstance(tenant_id, str) or not tenant_id:
        return {"ok": False, "status": 400, "code": "tenant_required"}
    try:
        if hook == "stripe.event":
            return {"ok": True, "result": stripe_event(event)}
        if hook == "checkout.giftcard":
            return checkout_hook(event)
        if hook == "accounting.events":
            return {"ok": True, **accounting_hook(event)}
        if hook in ("privacy.export", "privacy.erase"):
            return {"ok": True, **privacy_hook(event, erase=hook == "privacy.erase")}
        if hook == "tenant.purge":
            return {"ok": True, **purge_hook(event)}
    except gc.GiftCardError as exc:
        return {"ok": False, "status": exc.status, "code": exc.code.replace(" ", "_")}
    return {"ok": False, "status": 400, "code": "unknown_hook"}


# --- nightly: expiry and the contact clean-up ----------------------------------------------------

def _nightly_row(prefix, row, counts):
    tenant_id, _, card_id = str(row.get("GSI2SK") or "").partition("#")
    if prefix == "EXPIRY#":
        counts["expired"] += _expire(tenant_id, card_id)
        return
    card = gc.get_card(tenant_id, card_id)
    if card and card["status"] != "active":
        _strip_contact(card)
        gc.table().update_item(Key=gc.card_key(tenant_id, card_id), UpdateExpression="REMOVE GSI2PK, GSI2SK")
        counts["cleaned"] += 1


def run_nightly():
    """Resumable: JOB/NIGHTLY keeps the last day that finished with no
    failure, and only a run of finished days moves it (failures are counted
    per day; a failed card is retried the next night, its TX id is
    deterministic)."""
    at = gc.now()
    today = gc.local_day(at)
    state_key = {"PK": "JOB", "SK": "NIGHTLY"}
    state = gc.table().get_item(Key=state_key, ConsistentRead=True).get("Item") or {}
    start = state.get("lastDay") or (gc.parse_iso(f"{today}T00:00:00Z") - timedelta(days=31)).date().isoformat()
    first = (gc.parse_iso(f"{start}T00:00:00Z") + timedelta(days=1)).date().isoformat()
    yesterday = (gc.parse_iso(f"{today}T00:00:00Z") - timedelta(days=1)).date().isoformat()
    counts = {"expired": 0, "cleaned": 0, "failed": 0, "failedDays": []}
    advancing = True
    for day in (gc.days_between(first, yesterday) if first <= yesterday else []):
        day_failed = 0
        for prefix in ("EXPIRY#", "CLEANUP#"):
            kwargs = {"IndexName": gc.GSI2, "KeyConditionExpression": Key("GSI2PK").eq(f"{prefix}{day}")}
            while True:
                page = gc.table().query(**kwargs)
                for row in page.get("Items") or []:
                    try:
                        _nightly_row(prefix, row, counts)
                    except (ClientError, gc.GiftCardError) as exc:
                        day_failed += 1
                        logger.warning(json.dumps({"nightly": "card_failed", "day": day,
                                                   "type": type(exc).__name__}))
                if "LastEvaluatedKey" not in page:
                    break
                kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        counts["failed"] += day_failed
        if day_failed:
            counts["failedDays"].append(day)
            advancing = False  # this day is retried next night, so lastDay stays before it
        elif advancing:
            gc.table().put_item(Item={**state_key, "lastDay": day, "at": gc.iso(gc.now())})
    logger.info(json.dumps({"job": "nightly", **counts}))
    return counts


def _expire(tenant_id, card_id):
    card = gc.get_card(tenant_id, card_id)
    if not card or card["status"] != "active" or gc.parse_iso(card["expiresAt"]) > gc.now():
        return 0
    amount = int(card["balanceOre"])
    _, _, created = gc.change_balance(tenant_id, card_id, f"expire-{card_id}", -amount, "expire", "system",
                                      card["soldAtLocationId"], allow_expired=True)
    if created:
        _publish("GiftCard.Expired", {"tenantId": tenant_id, "cardId": card_id, "amountOre": amount})
    return 1 if created else 0


# --- dispatch --------------------------------------------------------------------------------------

ROUTES = {
    "GET /public/locations/{locationId}/gift-cards/offer": offer,
    "POST /public/locations/{locationId}/gift-cards/checkout": checkout,
    "POST /public/locations/{locationId}/gift-cards/balance": balance,
    "GET /tenant/gift-cards/settings": get_settings,
    "PUT /tenant/gift-cards/settings": put_settings,
    "GET /tenant/gift-cards": list_cards,
    "GET /tenant/gift-cards/{cardId}": get_card,
    "POST /tenant/gift-cards/{cardId}/adjust": adjust,
    "POST /tenant/gift-cards/{cardId}/refund": refund,
    "POST /tenant/gift-cards/{cardId}/resend": resend,
}


def handler(event, context=None):
    if isinstance(event, dict) and "requestContext" not in event:
        if "hook" in event:
            return _hook(event)
        if event.get("job") == "nightly":
            return run_nightly()
        return {"ok": False, "status": 400, "code": "unknown_event"}
    method, path = http.route(event)
    route = f"{method} {path}"
    if route not in ROUTES:
        return _err(404, "not found")
    try:
        if path.startswith("/public/"):
            return ROUTES[route](event)
        location_id = None
        if path.startswith("/locations/"):
            try:
                location_id = http.path_id(event, "locationId")
            except ValueError:
                return _err(404, "not found")
        ctx = tenant.for_jwt(event, location_id=location_id, feature=gc.FEATURE_KEY)
        return ROUTES[route](event, ctx)
    except tenant.TenantError as exc:
        return exc.response()
    except gc.GiftCardError as exc:
        if " " in exc.code:
            return http.respond(exc.status, {"error": exc.code, **({"field": exc.field} if exc.field else {})})
        return _err(exc.status, exc.code, exc.field)
    except turnstile.Unavailable:
        return _err(503, "service_unavailable")
    except ValueError as exc:
        return http.respond(400, {"error": str(exc)})
    except (BotoCoreError, ClientError) as exc:
        logger.error(json.dumps({"route": route, "aws": type(exc).__name__}))
        return _err(503, "service_unavailable")

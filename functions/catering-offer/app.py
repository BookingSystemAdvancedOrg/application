"""catering-offer

TRIGGER:
    API Gateway -- Auth: JWT (owner; staff may read)
    GET  /locations/{locationId}/catering/requests/{requestId}
    PUT  /locations/{locationId}/catering/requests/{requestId}/offer        save a draft
    POST /locations/{locationId}/catering/requests/{requestId}/offer/send   send version N
    POST /locations/{locationId}/catering/requests/{requestId}/decline      {"reason"}
    POST /locations/{locationId}/catering/requests/{requestId}/cancel       {"reason"}
    POST /locations/{locationId}/catering/requests/{requestId}/delivered
    GET  /locations/{locationId}/catering/requests/{requestId}/documents/{docId}
    GET  /locations/{locationId}/catering/invoices
    POST /locations/{locationId}/catering/invoices/{invoiceId}/mark-paid

PURPOSE:
    The restaurant's side of a catering request (shared/catering.py):

    - The owner adjusts the offer freely (lines, prices, discount, delivery
      fee, equipment return and cleaning fee, message, valid-until) and
      sends it: every send is a new, immutable OFFER#vN in the history; the
      guest can only sign the latest. The version keeps its own copy of the
      return choice and the fee, so changing the location's cleaning fee
      never touches an offer already sent or signed.
      Sending holds the portions on the day (capacity) - the difference
      when a new version replaces an older one.
    - Decline (before signing) releases the capacity and tells the guest.
    - Cancel (after signing, illness / force majeure) refunds a paid private
      order, voids an open company invoice (a paid one is refunded if it
      was paid through Stripe; Bankgiro payments are refunded by hand) and
      tells the guest.
    - Delivered: confirmed -> delivered, or closed when already paid.
    - Mark paid: a company paid the invoice by Bankgiro.
    - documents/signature: a 5-minute download link to the signature
      evidence (BankID's signature and OCSP response for the signed version).

    Every write is conditional on status + version (409 changed_retry when
    someone else changed it first) and appends an AUDIT# history entry.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    USER_TABLE_NAME, CATERING_REQUESTS_TABLE_NAME, CATERING_REQUEST_HISTORY_TABLE_NAME,
    CATERING_DOCUMENTS_BUCKET_NAME, STRIPE_SECRET_ARN, STRIPE_API_VERSION
"""

import json
import logging
import os
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from shared import catering as c
from shared import catering_stripe as cs
from shared import http, prices, tenant
from shared import orders as o
from shared import stripe_client as stripe

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_BASE = "/locations/{locationId}/catering/requests/{requestId}"
_ROUTES = {
    ("GET", _BASE): "view",
    ("PUT", _BASE + "/offer"): "draft",
    ("POST", _BASE + "/offer/send"): "send",
    ("POST", _BASE + "/decline"): "decline",
    ("POST", _BASE + "/cancel"): "cancel",
    ("POST", _BASE + "/delivered"): "delivered",
    ("GET", _BASE + "/documents/{docId}"): "document",
    ("GET", "/locations/{locationId}/catering/invoices"): "invoices",
    ("POST", "/locations/{locationId}/catering/invoices/{invoiceId}/mark-paid"): "mark_paid",
}
_EDITABLE = (c.REQUESTED, c.CHANGE_REQUESTED, c.OFFER_SENT)
_OFFER_FIELDS = {"lines", "discountPercent", "deliveryFee", "equipmentReturn", "cleaningFee", "message",
                 "validUntil"}



def _unit_price(value):
    unit = c.kronor_to_ore(value, "unitPrice")
    if unit > prices.MAX_ITEM_PRICE_ORE:
        raise ValueError(prices.TOO_HIGH)
    return unit

def _ctx(event, *, owner):
    location_id = http.path_id(event, "locationId")
    ctx = tenant.for_jwt(event, location_id=location_id, feature="catering", owner_only=owner)
    return location_id, ctx


def _load(event, location_id, ctx):
    item = c.load(location_id, http.path_id(event, "requestId"))
    if item.get("tenantId") != ctx.tenant_id:
        raise c.NotFound
    return item


def _offer_from(data, item, settings, location, now):
    unknown = sorted(set(data) - _OFFER_FIELDS)
    if unknown:
        raise ValueError(f"unsupported fields: {', '.join(unknown)}")
    raw_lines = data.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines or len(raw_lines) > c.MAX_LINES:
        raise ValueError(f"lines must be a list of 1-{c.MAX_LINES} lines")
    lines = []
    for raw in raw_lines:
        if not isinstance(raw, dict) or set(raw) - {"name", "unitPrice", "quantity", "menuItemId"}:
            raise ValueError("each line needs name, unitPrice (kr) and quantity")
        line = {
            "name": c.text(raw.get("name"), "line name", required=True, max_length=120),
            "unitPrice": _unit_price(raw.get("unitPrice")),
            "quantity": c.whole(raw.get("quantity"), 1, c.MAX_LINE_QTY, "quantity"),
        }
        if raw.get("menuItemId"):
            line["menuItemId"] = c.text(raw["menuItemId"], "menuItemId", max_length=128)
        lines.append(line)
    discount = c.whole(data.get("discountPercent", 0), 0, 50, "discountPercent")
    fee = c.kronor_to_ore(data.get("deliveryFee", 0), "deliveryFee")
    equipment = c.equipment_return(data.get("equipmentReturn", item.get("equipmentReturn")))
    if equipment == c.CLEANED:
        cleaning = 0
    elif data.get("cleaningFee") is None:
        cleaning = c.cleaning_fee(settings, equipment)  # the fee in force when the owner sends
    else:
        cleaning = c.kronor_to_ore(data["cleaningFee"], "cleaningFee")
    if cleaning > prices.MAX_ITEM_PRICE_ORE:
        raise ValueError(prices.TOO_HIGH)
    totals = c.totals(lines, discount, fee, cleaning)
    if totals["total"] <= 0:
        raise ValueError("offer total must be more than 0 kr")
    if totals["total"] > c.MAX_TOTAL_ORE:
        raise ValueError(f"offer total may be at most {c.MAX_TOTAL_ORE // 100} kr")
    tz = ZoneInfo(location.get("timezone") or "Europe/Stockholm")
    today = now.astimezone(tz).date()
    valid = data.get("validUntil") or (today + timedelta(days=settings["offerValidDays"])).isoformat()
    try:
        valid_day = date.fromisoformat(valid)
    except (TypeError, ValueError):
        raise ValueError("validUntil must be YYYY-MM-DD") from None
    if valid_day < today:
        raise ValueError("validUntil must not be in the past")
    if item.get("deliveryDate") and valid > item["deliveryDate"]:
        raise ValueError("validUntil must not be after the delivery date")
    return {
        "lines": lines,
        "totals": totals,
        "equipmentReturn": equipment,
        "portions": sum(line["quantity"] for line in lines if line.get("menuItemId")),
        "message": c.text(data.get("message"), "message", max_length=c.MESSAGE_MAX) or "",
        "validUntil": valid,
    }


def _view(event):
    location_id, ctx = _ctx(event, owner=False)
    item = _load(event, location_id, ctx)
    offers = []
    for v in range(1, int(item.get("currentOfferVersion") or 0) + 1):
        try:
            offer = c.load_offer(item["requestId"], v)
            offers.append({k: offer.get(k) for k in ("version", "lines", "totals", "equipmentReturn", "validUntil",
                                                      "message", "documentHash", "createdAt", "createdBy")})
        except c.NotFound:
            pass
    settings = c.settings_of(ctx.location)
    left = c.capacity_left(location_id, settings, item["deliveryDate"]) if item.get("deliveryDate") else None
    return http.respond(200, {**c.staff_view(item), "offers": offers,
                              "history": c.history(item["requestId"]),
                              "capacityLeft": left, "settings": c.public_settings(settings)})


def _draft(event):
    location_id, ctx = _ctx(event, owner=True)
    item = _load(event, location_id, ctx)
    if item["status"] not in _EDITABLE:
        raise c.InvalidState("not_editable")
    now = c.now_utc()
    draft = _offer_from(http.body(event), item, c.settings_of(ctx.location), ctx.location, now)
    c.transact([c.head_update(item, sets=["draft = :d", "updatedAt = :now"],
                              values={":d": draft, ":now": c.iso(now)})])
    return http.respond(200, c.staff_view(c.load(location_id, item["requestId"])))


def _send(event):
    location_id, ctx = _ctx(event, owner=True)
    item = _load(event, location_id, ctx)
    if item["status"] not in _EDITABLE:
        raise c.InvalidState("not_editable")
    now = c.now_utc()
    settings = c.settings_of(ctx.location)
    body = http.body(event, required=False)
    if body:
        offer = _offer_from(body, item, settings, ctx.location, now)
    elif item.get("draft"):
        offer = item["draft"]
    else:
        raise ValueError("send an offer (lines, ...) or save a draft first")
    version = int(item.get("currentOfferVersion") or 0) + 1
    offer = {**offer, "version": version, "terms": settings["terms"]}
    document = c.offer_document(item, offer, ctx.tenant, ctx.location, settings)
    doc_hash = c.document_hash(document)
    held = int(item.get("capacityHeld") or 0)
    delta = int(offer["portions"]) - held
    offer_item = {**c.offer_key(item["requestId"], version), **offer, "documentHash": doc_hash,
                  "document": document, "createdAt": c.iso(now), "createdBy": ctx.sub}
    summary = {k: offer[k] for k in ("version", "lines", "totals", "validUntil", "message", "portions")}
    summary["documentHash"] = doc_hash
    equipment = offer.get("equipmentReturn") or c.equipment_return(item.get("equipmentReturn"))
    summary["equipmentReturn"] = equipment
    c.transact([
        {"Put": {"TableName": c.history_table(), "Item": c.serialize(offer_item),
                 "ConditionExpression": "attribute_not_exists(SK)"}},
        c.head_update(item, to_status=c.OFFER_SENT, sets=[
            "currentOfferVersion = :v", "offer = :o", "validUntil = :valid", "capacityHeld = :held",
            "notice = :n", "updatedAt = :now", "offerSentAt = :now", "equipmentReturn = :eq"],
            removes=["draft", "changeMessage", "signing", "expiresAt"],
            values={":v": version, ":o": summary, ":valid": offer["validUntil"], ":held": int(offer["portions"]),
                    ":eq": equipment,
                    ":n": c.notice(c.N_OFFER_SENT, now, version=version), ":now": c.iso(now)}),
        c.capacity_change(location_id, settings, item["deliveryDate"], delta),
        c.audit_put(item["requestId"], "offer_sent", ctx.sub, now, version=version,
                    total=offer["totals"]["total"], documentHash=doc_hash),
    ])
    logger.info(json.dumps({"catering": "offer_sent", "requestId": item["requestId"], "version": version}))
    return http.respond(200, c.staff_view(c.load(location_id, item["requestId"])))


def _reason(event, required):
    data = http.body(event, allowed={"reason"}, required=required)
    return c.text(data.get("reason"), "reason", required=required, max_length=500)


def _decline(event):
    location_id, ctx = _ctx(event, owner=True)
    item = _load(event, location_id, ctx)
    if item["status"] not in _EDITABLE:
        raise c.InvalidState("not_declinable")
    reason = _reason(event, required=False)
    now = c.now_utc()
    c.transact([
        c.head_update(item, to_status=c.DECLINED, sets=["declineReason = :r", "notice = :n", "updatedAt = :now",
                                                         "capacityHeld = :zero"],
                      values={":r": reason or "", ":n": c.notice(c.N_DECLINED, now), ":now": c.iso(now),
                              ":zero": 0}),
        c.capacity_change(location_id, c.settings_of(ctx.location), item["deliveryDate"],
                          -int(item.get("capacityHeld") or 0)),
        c.audit_put(item["requestId"], "declined", ctx.sub, now, reason=reason),
    ])
    return http.respond(200, c.staff_view(c.load(location_id, item["requestId"])))


def _cancel(event):
    location_id, ctx = _ctx(event, owner=True)
    item = _load(event, location_id, ctx)
    if item["status"] not in (c.SIGNED, c.CONFIRMED, c.DELIVERED):
        raise c.InvalidState("not_cancellable")
    reason = _reason(event, required=True)
    now = c.now_utc()
    account = item.get("stripeAccountId") or o.stripe_account(ctx.tenant, ctx.location)
    refund_note = None
    if item.get("customerType") == c.PRIVATE and item.get("paid") and item.get("paymentIntentId"):
        cs.refund_payment(account, item["paymentIntentId"], item["requestId"])
        refund_note = "refunded"
    elif item.get("stripeInvoiceId"):
        invoice = cs.get_invoice(account, item["stripeInvoiceId"])
        if invoice.get("status") == "open":
            cs.void_invoice(account, item["stripeInvoiceId"])
            refund_note = "invoice_voided"
        elif invoice.get("status") == "paid" and invoice.get("payment_intent"):
            cs.refund_payment(account, invoice["payment_intent"], item["requestId"])
            refund_note = "refunded"
        elif invoice.get("status") == "paid":
            refund_note = "refund_manually"  # paid by Bankgiro - the restaurant pays back by hand
    c.transact([
        c.head_update(item, to_status=c.CANCELLED, sets=[
            "cancelReason = :r", "refundNote = :rn", "notice = :n", "updatedAt = :now", "capacityHeld = :zero"],
            values={":r": reason, ":rn": refund_note or "none", ":n": c.notice(c.N_CANCELLED, now),
                    ":now": c.iso(now), ":zero": 0}),
        c.capacity_change(location_id, c.settings_of(ctx.location), item["deliveryDate"],
                          -int(item.get("capacityHeld") or 0)),
        c.audit_put(item["requestId"], "cancelled", ctx.sub, now, reason=reason, refund=refund_note),
    ])
    return http.respond(200, c.staff_view(c.load(location_id, item["requestId"])))


def _delivered(event):
    location_id, ctx = _ctx(event, owner=False)
    item = _load(event, location_id, ctx)
    if item["status"] != c.CONFIRMED:
        raise c.InvalidState("not_confirmed")
    now = c.now_utc()
    target = c.CLOSED if item.get("paid") else c.DELIVERED
    c.transact([
        c.head_update(item, to_status=target, sets=["delivered = :t", "deliveredAt = :now", "updatedAt = :now"],
                      values={":t": True, ":now": c.iso(now)}),
        c.audit_put(item["requestId"], "delivered", ctx.sub, now, status=target),
    ])
    return http.respond(200, c.staff_view(c.load(location_id, item["requestId"])))


def _document(event):
    location_id, ctx = _ctx(event, owner=False)
    item = _load(event, location_id, ctx)
    if http.path_id(event, "docId") != "signature" or not (item.get("signature") or {}).get("evidenceKey"):
        raise c.NotFound
    url = boto3.client("s3").generate_presigned_url(
        "get_object", ExpiresIn=300,
        Params={"Bucket": os.environ["CATERING_DOCUMENTS_BUCKET_NAME"], "Key": item["signature"]["evidenceKey"]})
    return http.respond(200, {"url": url, "expiresIn": 300})


def _invoices(event):
    location_id, ctx = _ctx(event, owner=True)
    rows = [{k: i.get(k) for k in ("requestId", "companyName", "customerName", "deliveryDate", "stripeInvoiceId",
                                   "invoiceNumber", "invoiceStatus", "invoiceUrl", "paid", "status")}
            for i in c.list_requests(location_id) if i.get("stripeInvoiceId")]
    rows.sort(key=lambda r: r.get("deliveryDate") or "", reverse=True)
    return http.respond(200, {"invoices": rows})


def _mark_paid(event):
    location_id, ctx = _ctx(event, owner=True)
    invoice_id = http.path_id(event, "invoiceId")
    match = [i for i in c.list_requests(location_id) if i.get("stripeInvoiceId") == invoice_id]
    if not match or match[0].get("tenantId") != ctx.tenant_id:
        raise c.NotFound
    item = match[0]
    account = item.get("stripeAccountId") or o.stripe_account(ctx.tenant, ctx.location)
    cs.pay_out_of_band(account, invoice_id)
    # invoice.paid arrives through the webhook and sets paid / closes the request.
    c.transact([c.audit_put(item["requestId"], "invoice_marked_paid", ctx.sub, c.now_utc(), invoice=invoice_id)])
    return http.respond(202, {"status": "marking_paid"})


_HANDLERS = {"view": _view, "draft": _draft, "send": _send, "decline": _decline, "cancel": _cancel,
             "delivered": _delivered, "document": _document, "invoices": _invoices, "mark_paid": _mark_paid}


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
    except stripe.StripeError as exc:
        logger.error(json.dumps({"stripe": "rejected", "code": exc.code, "message": str(exc)}))
        return http.error(502, "payment_provider_rejected")
    except stripe.StripeUnavailable:
        return http.error(503, "payment_unavailable")
    except ValueError as exc:
        return http.error(400, str(exc))
    except (BotoCoreError, ClientError):
        logger.exception("catering service unavailable")
        return http.error(503, "catering service unavailable")

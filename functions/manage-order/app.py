"""manage-order

TRIGGER:
    API Gateway -- Auth: JWT (owner_user, staff_user of the location's tenant)
    GET  /locations/{locationId}/orders?date=YYYY-MM-DD[&all=true]
         The day's orders for the kitchen/counter screen, oldest first, plus
         whether ordering is open now. Orders still waiting for payment or
         never paid are left out unless all=true.
    PUT  /locations/{locationId}/orders/{orderId}/status   {"status": ...}
         preparing | ready | completed | cancelled (see orders.TRANSITIONS).
         preparing ("Påbörja") sends the guest the confirmation SMS - the
         restaurant has started on it, ready around now + prepMinutes (the
         estimate is set again from this moment); ready on a pickup order
         sends the "ready" SMS; cancelled refunds the whole order with
         Stripe and tells the guest.
    PUT  /locations/{locationId}/orders/pause   {"minutes": 0-240}
         Stop taking orders for a while (0 = take orders again).
    POST /locations/{locationId}/orders/table-codes
         {"tables": [{"tableId", "label"}]} -> the QR code value for each
         table (signed, so a guest can't make up a table) and the guest-site
         URL to print in the QR code (null while the restaurant has no site
         address).

PURPOSE:
    The restaurant's side of food orders. Status writes are conditional on
    the status the screen showed (or the order's version), so two staff
    pressing at once can't both act; the loser gets 409 and a fresh list.

    Multi-tenant: the token's tenant must own the location (else 404) and
    have the "ordering" feature; staff are limited to their own location.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    USER_TABLE_NAME, ORDER_TABLE_NAME, RESERVATION_LINK_KEY_SECRET_ARN,
    SITE_DOMAIN, CUSTOMER_SITE_URL (dev: QR code URLs with ?restaurang=),
    STRIPE_SECRET_ARN, STRIPE_API_VERSION (refunds)
"""

import json
import logging
import os
from datetime import timedelta
from urllib.parse import quote

from botocore.exceptions import BotoCoreError, ClientError

from shared import dynamo, http, manage_link, order_payments, site, tenant
from shared import orders as o
from shared import stripe_client as stripe

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_COLLECTION = "/locations/{locationId}/orders"
_ITEM = "/locations/{locationId}/orders/{proxy+}"
_MAX_TABLE_CODES = 200


def _ctx(event):
    location_id = http.path_id(event, "locationId")
    return location_id, tenant.for_jwt(event, location_id=location_id, feature="ordering")


def _list(event):
    location_id, ctx = _ctx(event)
    params = http.query(event)
    unknown = sorted(set(params) - {"date", "all"})
    if unknown:
        raise ValueError(f"unsupported query parameters: {', '.join(unknown)}")
    now = o.now_utc()
    date_str = params.get("date") or o.local_now(ctx.location, now).date().isoformat()
    show_all = params.get("all") == "true"
    items = o.list_day(location_id, date_str)
    if not show_all:
        items = [i for i in items if i.get("status") not in (o.PENDING_PAYMENT, o.FAILED)]
    items.sort(key=lambda i: (i.get("confirmedAt") or i.get("createdAt") or "", i.get("orderId")))
    return http.respond(200, {
        "date": date_str,
        "ordering": o.availability(ctx.location, ctx.tenant, now),
        "orders": [o.staff_view(i) for i in items],
    })


def _update_status(item, target, ctx, now, *, expected, extra_sets=None, extra_values=None,
                   notice_kind=None, history_extra=None):
    sets = ["#s = :target", "updatedAt = :now", "updatedBy = :by",
            "history = list_append(if_not_exists(history, :empty), :entry)",
            "version = if_not_exists(version, :zero) + :one"]
    values = {":target": target, ":now": o.iso(now), ":by": ctx.sub, ":empty": [], ":zero": 0,
              ":one": 1, ":expected": expected,
              ":entry": [o.history_entry(target, ctx.sub, now, **(history_extra or {}))]}
    stamp = {o.READY: "readyAt", o.COMPLETED: "completedAt", o.CANCELLED: "cancelledAt",
             o.PREPARING: "preparingAt"}.get(target)
    if stamp:
        sets.append(f"{stamp} = :now")
    if notice_kind:
        sets.append("notice = :notice")
        values[":notice"] = o.notice(notice_kind, now)
    sets.extend(extra_sets or [])
    values.update(extra_values or {})
    try:
        result = dynamo.table(o.order_table()).update_item(
            Key=o.order_key(item["locationId"], item["date"], item["orderId"]),
            UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression="attribute_exists(PK) AND #s = :expected",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise o.InvalidState("changed_retry") from None
        raise
    return result["Attributes"]


def _set_status(event, location_id, ctx, order_id):
    data = http.body(event, allowed={"status", "expectedStatus"})
    target = data.get("status")
    item = o.load(location_id, order_id)
    if item.get("tenantId") != ctx.tenant_id:
        raise o.NotFound
    current = item.get("status")
    expected = data.get("expectedStatus", current)
    if expected != current:
        raise o.InvalidState("changed_retry")
    if target not in TRANSITIONS_FOR_API:
        raise ValueError("status must be preparing, ready, completed or cancelled")
    if target not in o.TRANSITIONS.get(current, ()):
        raise o.InvalidState(f"cannot_move_{current}_to_{target}")
    now = o.now_utc()

    if target == o.CANCELLED:
        refund = order_payments.refund(item, now, ctx.sub)
        amount = int(refund.get("amount") or item["totalAmount"])
        updated = _update_status(
            item, o.CANCELLED, ctx, now, expected=current, notice_kind=o.NOTICE_CANCELLED,
            extra_sets=["paymentStatus = :refunded", "refund = :refund"],
            extra_values={":refunded": o.PAY_REFUNDED,
                          ":refund": {"amount": amount, "at": o.iso(now), "refundId": refund.get("id"),
                                      "status": refund.get("status")}},
            history_extra={"refunded": amount})
        logger.info(json.dumps({"order": "cancelled", "orderId": order_id, "refund": refund.get("id")}))
        return http.respond(200, o.staff_view(updated))

    if target == o.PREPARING:
        # The kitchen started: the guest's confirmation SMS, with a ready
        # time counted from now (the order may have waited in "Nya").
        prep = int(item.get("prepMinutes") or 20)
        updated = _update_status(
            item, target, ctx, now, expected=current, notice_kind=o.NOTICE_STARTED,
            extra_sets=["readyEstimate = :ready"],
            extra_values={":ready": o.iso(now + timedelta(minutes=prep))})
        return http.respond(200, o.staff_view(updated))
    notice_kind = o.NOTICE_READY if target == o.READY and item.get("mode") == o.PICKUP else None
    updated = _update_status(item, target, ctx, now, expected=current, notice_kind=notice_kind)
    return http.respond(200, o.staff_view(updated))


TRANSITIONS_FOR_API = (o.PREPARING, o.READY, o.COMPLETED, o.CANCELLED)


def _pause(event, location_id, ctx):
    data = http.body(event, allowed={"minutes"})
    minutes = o._whole(data.get("minutes"), 0, o.MAX_PAUSE_MINUTES, "minutes")
    now = o.now_utc()
    table = dynamo.table(os.environ["LOCATION_TABLE_NAME"])
    if minutes:
        until = o.iso(now + timedelta(minutes=minutes))
        table.update_item(Key=ctx.location_key, UpdateExpression="SET orderingPausedUntil = :u",
                          ConditionExpression="attribute_exists(PK)",
                          ExpressionAttributeValues={":u": until})
        location = {**ctx.location, "orderingPausedUntil": until}
    else:
        table.update_item(Key=ctx.location_key, UpdateExpression="REMOVE orderingPausedUntil",
                          ConditionExpression="attribute_exists(PK)")
        location = {k: v for k, v in ctx.location.items() if k != "orderingPausedUntil"}
    return http.respond(200, {"ordering": o.availability(location, ctx.tenant, now)})


def _table_codes(event, location_id, ctx):
    data = http.body(event, allowed={"tables"})
    tables = data.get("tables")
    if not isinstance(tables, list) or not tables or len(tables) > _MAX_TABLE_CODES:
        raise ValueError(f"tables must be a list of 1-{_MAX_TABLE_CODES} tables")
    base, slug = site.base(ctx.tenant_id, ctx.tenant)
    out = []
    for entry in tables:
        if not isinstance(entry, dict) or set(entry) - {"tableId", "label"}:
            raise ValueError("each table needs tableId and label")
        code = o.table_code(ctx.tenant_id, location_id, entry.get("tableId"), entry.get("label"))
        url = None
        if base:
            url = site.page(base, slug, "/bestall") + ("&" if slug else "?") + "bord=" + quote(code, safe="")
        out.append({"tableId": entry["tableId"], "label": o.clean_table_label(entry["label"]),
                    "code": code, "url": url})
    return http.respond(200, {"tables": out})


def _item_route(event, method):
    location_id, ctx = _ctx(event)
    proxy = ((event.get("pathParameters") or {}).get("proxy") or "").strip("/")
    parts = proxy.split("/") if proxy else []
    if parts == ["pause"]:
        if method != "PUT":
            return http.respond(405, {"error": "method not allowed"}, {"Allow": "PUT"})
        return _pause(event, location_id, ctx)
    if parts == ["table-codes"]:
        if method != "POST":
            return http.respond(405, {"error": "method not allowed"}, {"Allow": "POST"})
        return _table_codes(event, location_id, ctx)
    if len(parts) == 2 and parts[1] == "status" and o.valid_order_id(parts[0]):
        if method != "PUT":
            return http.respond(405, {"error": "method not allowed"}, {"Allow": "PUT"})
        return _set_status(event, location_id, ctx, parts[0])
    if len(parts) == 1 and o.valid_order_id(parts[0]):
        if method != "GET":
            return http.respond(405, {"error": "method not allowed"}, {"Allow": "GET"})
        item = o.load(location_id, parts[0])
        if item.get("tenantId") != ctx.tenant_id:
            raise o.NotFound
        return http.respond(200, o.staff_view(item))
    return http.error(404, "not found")


def handler(event, context):
    method, path = http.route(event)
    try:
        if path == _COLLECTION:
            if method != "GET":
                return http.respond(405, {"error": "method not allowed"}, {"Allow": "GET"})
            return _list(event)
        if path == _ITEM:
            return _item_route(event, method)
        return http.error(404, "not found")
    except tenant.TenantError as exc:
        return exc.response()
    except o.NotFound:
        return http.error(404, "not found")
    except o.InvalidState as exc:
        return http.error(409, exc.code)
    except stripe.StripeError as exc:
        logger.error(json.dumps({"refund": "rejected", "code": exc.code, "message": str(exc)}))
        return http.error(502, "refund_failed")
    except stripe.StripeUnavailable:
        return http.error(503, "payment_unavailable")
    except ValueError as exc:
        return http.error(400, str(exc))
    except (BotoCoreError, ClientError, manage_link.LinkKeyUnavailable):
        logger.exception("order service unavailable")
        return http.error(503, "order service unavailable")

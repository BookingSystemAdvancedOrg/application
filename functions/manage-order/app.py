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
         Stripe and tells the guest. An order paid partly or fully with a
         gift card gets the card's part back through the gift-cards feature
         (checkout.giftcard op refund, once: giftCard.refundedAt) and only
         the charged part from Stripe (nothing when the card covered it).
    POST /locations/{locationId}/orders/{orderId}/refund
         {"amount": öre (optional = what is left), "reason", "notifyGuest"}
         Owner only: (part of) the card payment back without cancelling, on
         confirmed / preparing / ready / completed paid orders. A gift
         card's part comes back only on cancel. paymentStatus becomes
         partly_refunded or refunded; the guest gets order_refunded (e-mail)
         unless notifyGuest is false. Rules: shared/refunds.py.
    PUT  /locations/{locationId}/orders/pause   {"minutes": 0-240}
         Stop taking orders for a while (0 = take orders again).
    GET|POST /locations/{locationId}/orders/table-codes
         The QR code of every table in the ACTIVE published floor plan
         (shared/table_qr.py: a random key per table, created on first use),
         with the table's current name and the guest-site URL to print in the
         QR code (null while the restaurant has no site address). A body from
         older admin builds ({"tables": [...]}) is accepted and ignored: the
         floor plan decides which tables exist and what they are called.
    POST /locations/{locationId}/orders/table-codes/{tableId}/regenerate
         A new code for one table (sticker stolen or worn): the old printed
         code stops working at once and shows the guest a clear message.

    Table bills (pay-later QR table orders, shared/table_bills.py):
    GET  /locations/{locationId}/orders/tabs
         Every table with an open bill, oldest first: {serviceDate, tabs:
         [{tableId, tableLabel, title, serviceDate, amount, count, oldestAt,
         orders, paying?, stale, mismatch?}]}. Area Beställningar or Layout;
         with Layout only, the orders carry no personal data (no name,
         phone, e-mail, notes or history).
    POST /locations/{locationId}/orders/tabs/{serviceDate}/{tableId}/pay-link
         {} -> {url, expiresAt, amount, count}: the link (QR code) the guest
         pays the whole bill with. 409 bill_empty.
    POST .../tabs/{serviceDate}/{tableId}/paid-outside
         {method card_terminal|cash|swish|other, note?, orderIds?,
         expectedAmount}: the guest paid outside Booqy (no Booqy receipt).
    POST .../tabs/{serviceDate}/{tableId}/write-off
         {reason, orderIds?, expectedAmount}: owner only, closed unpaid.
    POST .../tabs/{serviceDate}/{tableId}/mismatch-checked
         owner only: a flagged bill payment has been checked.
         The three answer {tab, orders}; 409 bill_changed (the amount or
         orders differ from what the screen showed), bill_being_paid,
         bill_paid, bill_processing. Every action writes an audit row.
    Cancelling an unpaid order (PUT .../status cancelled) refunds nothing:
    it leaves its table's bill, paymentStatus void.

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
    PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME (the tables behind the QR codes),
    STRIPE_SECRET_ARN, STRIPE_API_VERSION (refunds)
"""

import json
import logging
import os
import re
from datetime import timedelta
from urllib.parse import quote

from botocore.exceptions import BotoCoreError, ClientError

from shared import dynamo, feature_hooks, http, manage_link, order_payments, refunds, site, tenant
from shared import orders as o
from shared import table_bills, table_qr
from shared import stripe_client as stripe

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_COLLECTION = "/locations/{locationId}/orders"
_ITEM = "/locations/{locationId}/orders/{proxy+}"
_TABLE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _ctx(event, area="bestallningar"):
    location_id = http.path_id(event, "locationId")
    return location_id, tenant.for_jwt(event, location_id=location_id, feature="ordering", area=area)


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


def _refund_gift_card(item, now):
    """The gift card's part back to the card, once (giftCard.refundedAt;
    the feature is also idempotent per order). Regardless of the flag: the
    order carries the card (CONTRACTS.md 9.2)."""
    gift = item["giftCard"]
    if gift.get("refundedAt"):
        return item
    answer = feature_hooks.call("checkout.giftcard", {
        "tenantId": item["tenantId"], "op": "refund", "locationId": item["locationId"],
        "orderId": item["orderId"]}, targets=["gift-cards"]).get("gift-cards")
    if answer is None or not answer["ok"]:
        raise feature_hooks.HookFailed("gift-cards")
    try:
        result = dynamo.table(o.order_table()).update_item(
            Key=o.order_key(item["locationId"], item["date"], item["orderId"]),
            UpdateExpression="SET giftCard.refundedAt = :now, giftCard.refundedOre = :ore",
            ConditionExpression="attribute_exists(giftCard) AND attribute_not_exists(giftCard.refundedAt)",
            ExpressionAttributeValues={":now": o.iso(now), ":ore": int(answer.get("reversedOre") or 0)},
            ReturnValues="ALL_NEW")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        return o.load(item["locationId"], item["orderId"])
    return result["Attributes"]


def _cancel(item, ctx, now, current):
    if item.get("paymentStatus") == o.PAY_UNPAID:
        return table_bills.cancel_unpaid(item, ctx, now, current)
    if item.get("paymentStatus") in (o.PAY_OUTSIDE, o.PAY_WRITTEN_OFF):
        # Settled outside Booqy: there is nothing Booqy could refund.
        raise o.InvalidState("settled_outside_booqy")
    return _cancel_paid(item, ctx, now, current)


def _cancel_paid(item, ctx, now, current):
    """Stripe first (what is left of the card part, nothing when a gift card
    covered it or it is already refunded), recorded on the order
    (shared/refunds.py); then a gift card's part back, once; then cancelled.
    A Stripe failure leaves the order as it was and the card not credited."""
    if order_payments.charged_amount(item) > 0 and o.refundable_amount(item) > 0:
        item = order_payments.refund(item, None, now, ctx.sub, notify=False, notice_kind=None,
                                     statuses=o.OPEN_STATUSES)
    if item.get("giftCard"):
        item = _refund_gift_card(item, now)
    amount = int((item.get("refund") or {}).get("amount") or 0)
    gift = {"giftCardRefunded": int(item["giftCard"].get("refundedOre") or 0)} if item.get("giftCard") else {}
    updated = _update_status(
        item, o.CANCELLED, ctx, now, expected=current, notice_kind=o.NOTICE_CANCELLED,
        extra_sets=["paymentStatus = :refunded"], extra_values={":refunded": o.PAY_REFUNDED},
        history_extra={"refunded": amount, **gift})
    logger.info(json.dumps({"order": "cancelled", "orderId": item["orderId"], "refunded": amount,
                            "giftCard": bool(item.get("giftCard"))}))
    return updated


def _refund(event, location_id, ctx, order_id):
    """POST .../orders/{orderId}/refund {amount?, reason?, notifyGuest?}:
    owner only, (part of) the card payment back without cancelling."""
    if not ctx.is_owner:
        raise tenant.TenantError(403, "owner_only")
    data = http.body(event, allowed={"amount", "reason", "notifyGuest"})
    item = o.load(location_id, order_id)
    if item.get("tenantId") != ctx.tenant_id:
        raise o.NotFound
    updated = order_payments.refund(item, data.get("amount"), o.now_utc(), ctx.sub,
                                    reason=refunds.parse_reason(data.get("reason")),
                                    notify=refunds.parse_notify(data.get("notifyGuest")))
    logger.info(json.dumps({"order": "refunded", "orderId": order_id,
                            "total": int((updated.get("refund") or {}).get("amount") or 0)}))
    return http.respond(200, o.staff_view(updated))


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
        return http.respond(200, o.staff_view(_cancel(item, ctx, now, current)))

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


def _table_url(base, slug, code):
    if not base:
        return None
    return site.page(base, slug, "/bestall") + ("&" if slug else "?") + "bord=" + quote(code, safe="")


def _code_view(table, base, slug):
    return {"tableId": table["tableId"], "label": table["label"], "title": table_qr.table_title(table["label"]),
            "seats": table["seats"], "code": table["code"], "url": _table_url(base, slug, table["code"])}


def _table_codes(event, location_id, ctx):
    if event.get("body"):
        http.body(event, allowed={"tables"})  # older admin builds; the floor plan decides
    base, slug = site.base(ctx.tenant_id, ctx.tenant)
    tables = table_qr.codes_for(ctx.tenant_id, location_id, ctx.sub, o.now_utc())
    return http.respond(200, {"tables": [_code_view(t, base, slug) for t in tables]})


def _regenerate(location_id, ctx, table_id):
    table = table_qr.regenerate(ctx.tenant_id, location_id, table_id, ctx.sub, o.now_utc())
    logger.info(json.dumps({"tableQr": "regenerated", "locationId": location_id, "tableId": table_id}))
    base, slug = site.base(ctx.tenant_id, ctx.tenant)
    return http.respond(200, _code_view(table, base, slug))


# What staff with only the Layout area see of an order on a table's bill:
# the dishes and amounts, never who ordered (no name, phone, e-mail, notes or
# history with staff ids).
_FLOOR_HIDDEN = ("customerName", "customerPhone", "customerEmail", "notes", "history", "paidOutside",
                 "writtenOff", "refund", "giftCard")


def floor_view(item):
    view = o.staff_view(item)
    return {k: v for k, v in view.items() if k not in _FLOOR_HIDDEN}


def _tabs(location_id, ctx):
    now = o.now_utc()
    view = o.staff_view if ctx.allows("bestallningar") else floor_view
    return http.respond(200, table_bills.staff_tabs(ctx.location, ctx.tenant, location_id, now, staff_view=view))


def _tab_action(event, location_id, ctx, service_date, table_id, action):
    if not table_bills.valid_service_date(service_date) or not table_bills.valid_table_id(table_id):
        raise o.NotFound
    now = o.now_utc()
    if action == "pay-link":
        if event.get("body"):
            http.body(event, allowed=set())
        base, slug = site.base(ctx.tenant_id, ctx.tenant)
        return http.respond(200, table_bills.pay_link(ctx, location_id, service_date, table_id, now,
                                                      base=base, slug=slug))
    if action in ("write-off", "mismatch-checked") and not ctx.is_owner:
        raise tenant.TenantError(403, "owner_only")
    if action == "mismatch-checked":
        table_bills.clear_mismatch(ctx, location_id, service_date, table_id, now)
        return http.respond(200, {"ok": True})
    if action == "paid-outside":
        data = http.body(event, allowed={"method", "note", "orderIds", "expectedAmount"})
        kind = o.PAY_OUTSIDE
    else:
        data = http.body(event, allowed={"reason", "orderIds", "expectedAmount"})
        kind = o.PAY_WRITTEN_OFF
    updated = table_bills.close_without_booqy(ctx, location_id, service_date, table_id, kind, data, now)
    tab = table_bills.load_tab(location_id, service_date, table_id) or {
        "tableId": table_id, "serviceDate": service_date}
    today = o.service_date(ctx.location, now)
    closed = o.availability(ctx.location, ctx.tenant, now).get("reason") == "closed"
    open_ids = set(table_bills.entries(tab))
    orders = {i["orderId"]: i for i in (o.load(location_id, x) for x in open_ids)}
    return http.respond(200, {"tab": table_bills.tab_view(tab, orders, today, closed, o.staff_view),
                              "orders": [o.staff_view(i) for i in updated]})


_TAB_ACTIONS = ("pay-link", "paid-outside", "write-off", "mismatch-checked")


def _item_route(event, method):
    proxy = ((event.get("pathParameters") or {}).get("proxy") or "").strip("/")
    parts = proxy.split("/") if proxy else []
    # The table QR codes and the open table bills are shown on Layout too (the floor plan's tables).
    table_codes = bool(parts) and parts[0] == "table-codes"
    read_tabs = parts == ["tabs"]
    location_id, ctx = _ctx(event, ("bestallningar", "layout") if table_codes or read_tabs else "bestallningar")
    if read_tabs:
        if method != "GET":
            return http.respond(405, {"error": "method not allowed"}, {"Allow": "GET"})
        return _tabs(location_id, ctx)
    if len(parts) == 4 and parts[0] == "tabs" and parts[3] in _TAB_ACTIONS:
        if method != "POST":
            return http.respond(405, {"error": "method not allowed"}, {"Allow": "POST"})
        return _tab_action(event, location_id, ctx, parts[1], parts[2], parts[3])
    if parts == ["pause"]:
        if method != "PUT":
            return http.respond(405, {"error": "method not allowed"}, {"Allow": "PUT"})
        return _pause(event, location_id, ctx)
    if parts == ["table-codes"]:
        if method not in ("GET", "POST"):
            return http.respond(405, {"error": "method not allowed"}, {"Allow": "GET, POST"})
        return _table_codes(event, location_id, ctx)
    if len(parts) == 3 and parts[0] == "table-codes" and parts[2] == "regenerate" and _TABLE_ID.fullmatch(parts[1]):
        if method != "POST":
            return http.respond(405, {"error": "method not allowed"}, {"Allow": "POST"})
        return _regenerate(location_id, ctx, parts[1])
    if len(parts) == 2 and parts[1] == "status" and o.valid_order_id(parts[0]):
        if method != "PUT":
            return http.respond(405, {"error": "method not allowed"}, {"Allow": "PUT"})
        return _set_status(event, location_id, ctx, parts[0])
    if len(parts) == 2 and parts[1] == "refund" and o.valid_order_id(parts[0]):
        if method != "POST":
            return http.respond(405, {"error": "method not allowed"}, {"Allow": "POST"})
        return _refund(event, location_id, ctx, parts[0])
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
    except table_bills.BillError as exc:
        return http.respond(exc.status, {"error": exc.code, **exc.extra})
    except refunds.Conflict as exc:
        return http.error(409, exc.code)
    except stripe.StripeError as exc:
        logger.error(json.dumps({"refund": "rejected", "code": exc.code, "message": str(exc)}))
        return http.error(502, "refund_failed")
    except stripe.StripeUnavailable:
        return http.error(503, "payment_unavailable")
    except feature_hooks.HookFailed:
        logger.error(json.dumps({"refund": "gift_card_unavailable"}))
        return http.error(503, "gift_card_unavailable")
    except ValueError as exc:
        return http.error(400, str(exc))
    except (BotoCoreError, ClientError, manage_link.LinkKeyUnavailable):
        logger.exception("order service unavailable")
        return http.error(503, "order service unavailable")

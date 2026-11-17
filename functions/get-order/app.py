"""get-order

TRIGGER:
    API Gateway -- Auth: NONE (guests)
    GET /locations/{locationId}/ordering[?table=<QR code>]
        Can the guest order right now: {open, reason, pickup, table,
        prepMinutes, lastOrderAt, pausedUntil} plus, with a QR code, the
        table it belongs to ({forTable: {label, title}}, the table's current
        name in the active floor plan). 400 "table code is invalid" for a
        code that isn't this location's, "table code revoked" for a
        regenerated one, "table removed" when the table left the floor plan.
    GET /locations/{locationId}/order-status/{orderId}
        Header X-Order-Token (the status token from POST /order or the
        link in the guest's SMS/e-mail). The order as the guest sees it.
        While it is still pending_payment the Checkout Session is checked
        with Stripe, so the page right after payment shows "confirmed"
        even if the webhook is a few seconds behind.

    GET /locations/{locationId}/table-bill[?order=<orderId>]
        Header X-Order-Token: the staff pay link token, or the guest's
        order status token with ?order= (a pay-later table order). The
        table's open bill as anyone at the table may see it (no names,
        phones or e-mails): {tableTitle, serviceDate, status open | paying |
        empty, amount, count, orders [{number, createdAt, total, lines}],
        vat, paying? {startedAt, retryAfter}, location {name}}.
    GET /locations/{locationId}/table-bill/{billId}
        Header X-Order-Token: the bill token from POST .../table-bill/pay.
        One payment attempt; while it is paying the Checkout Session is
        checked with Stripe (like the status page). {billId, status paying
        | paid | expired | failed, amount, paidAt?, tableTitle, orders, vat}.

PURPOSE:
    The guest's side of ordering: open/closed before ordering, and the
    order's progress (confirmed -> preparing -> ready) afterwards. A wrong
    or missing token answers 404 like an order that doesn't exist.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    ORDER_TABLE_NAME, RESERVATION_LINK_KEY_SECRET_ARN,
    PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME (table QR codes),
    STRIPE_SECRET_ARN, STRIPE_API_VERSION (pending orders)
"""

import logging

from botocore.exceptions import BotoCoreError, ClientError

from shared import http, manage_link, order_payments, tenant
from shared import orders as o
from shared import table_bills, table_qr
from shared import stripe_client as stripe

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_ORDERING = "/locations/{locationId}/ordering"
_STATUS = "/locations/{locationId}/order-status/{orderId}"
_BILL = "/locations/{locationId}/table-bill"
_BILL_ONE = "/locations/{locationId}/table-bill/{billId}"


def _ordering(event):
    location_id = http.path_id(event, "locationId")
    ctx = tenant.for_public(location_id, feature="ordering")
    params = http.query(event)
    unknown = sorted(set(params) - {"table"})
    if unknown:
        raise ValueError(f"unsupported query parameters: {', '.join(unknown)}")
    state = o.availability(ctx.location, ctx.tenant, o.now_utc())
    if params.get("table"):
        _, label = table_qr.resolve(ctx.tenant_id, location_id, params["table"])
        state["forTable"] = {"label": label, "title": table_qr.table_title(label)}
    return http.respond(200, state)


def _status(event):
    location_id = http.path_id(event, "locationId")
    ctx = tenant.for_public(location_id, feature="ordering")
    item = o.load(location_id, http.path_id(event, "orderId"))
    if item.get("tenantId") != ctx.tenant_id or \
            not o.token_matches(item, http.header(event, "x-order-token")):
        raise o.NotFound
    if item.get("status") == o.PENDING_PAYMENT:
        try:
            item = order_payments.sync_from_stripe(item, o.now_utc(), "guest")
        except (stripe.StripeError, stripe.StripeUnavailable):
            logger.warning("checkout session lookup failed for %s", item["orderId"])
    view = o.guest_view(item, ctx.location)
    bill = table_bills.order_bill(item)
    if bill:
        view["bill"] = bill
    return http.respond(200, view)


def _bill(event):
    location_id = http.path_id(event, "locationId")
    ctx = tenant.for_public(location_id, feature="ordering")
    params = http.query(event)
    unknown = sorted(set(params) - {"order"})
    if unknown:
        raise ValueError(f"unsupported query parameters: {', '.join(unknown)}")
    now = o.now_utc()
    service_date, table_id = table_bills.resolve_guest(ctx, location_id, http.header(event, "x-order-token"),
                                                       params.get("order"), now)
    tab = table_bills.load_tab(location_id, service_date, table_id)
    if not tab:
        raise o.NotFound
    return http.respond(200, table_bills.guest_view(tab, ctx.location, now))


def _bill_one(event):
    location_id = http.path_id(event, "locationId")
    ctx = tenant.for_public(location_id, feature="ordering")
    bill_id = http.path_id(event, "billId")
    if not table_bills.bill_token_matches(ctx.tenant_id, location_id, bill_id, http.header(event, "x-order-token")):
        raise o.NotFound
    bill = table_bills.load_bill(location_id, bill_id)
    if not bill or bill.get("tenantId") != ctx.tenant_id:
        raise o.NotFound
    if bill.get("status") == table_bills.PAYING:
        try:
            bill = table_bills.sync(bill, o.now_utc(), "guest")
        except (stripe.StripeError, stripe.StripeUnavailable):
            logger.warning("checkout session lookup failed for bill %s", bill_id)
    return http.respond(200, table_bills.bill_view(bill))


_ROUTES = {_ORDERING: _ordering, _STATUS: _status, _BILL: _bill, _BILL_ONE: _bill_one}


def handler(event, context):
    method, path = http.route(event)
    if path not in _ROUTES:
        return http.error(404, "not found")
    if method != "GET":
        return http.respond(405, {"error": "method not allowed"}, {"Allow": "GET"})
    try:
        return _ROUTES[path](event)
    except tenant.TenantError as exc:
        return exc.response()
    except o.NotFound:
        return http.error(404, "not found")
    except ValueError as exc:
        return http.error(400, str(exc))
    except (BotoCoreError, ClientError, manage_link.LinkKeyUnavailable):
        logger.exception("order service unavailable")
        return http.error(503, "order service unavailable")

"""get-order

TRIGGER:
    API Gateway -- Auth: NONE (guests)
    GET /locations/{locationId}/ordering[?table=<QR code>]
        Can the guest order right now: {open, reason, pickup, table,
        prepMinutes, lastOrderAt, pausedUntil} plus, with a QR code, the
        table it belongs to ({forTable: {label}}; 400 for a code that isn't
        this location's).
    GET /locations/{locationId}/order-status/{orderId}
        Header X-Order-Token (the status token from POST /order or the
        link in the guest's SMS/e-mail). The order as the guest sees it.
        While it is still pending_payment the Checkout Session is checked
        with Stripe, so the page right after payment shows "confirmed"
        even if the webhook is a few seconds behind.

PURPOSE:
    The guest's side of ordering: open/closed before ordering, and the
    order's progress (confirmed -> preparing -> ready) afterwards. A wrong
    or missing token answers 404 like an order that doesn't exist.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    ORDER_TABLE_NAME, RESERVATION_LINK_KEY_SECRET_ARN,
    STRIPE_SECRET_ARN, STRIPE_API_VERSION (pending orders)
"""

import logging

from botocore.exceptions import BotoCoreError, ClientError

from shared import http, manage_link, order_payments, tenant
from shared import orders as o
from shared import stripe_client as stripe

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_ORDERING = "/locations/{locationId}/ordering"
_STATUS = "/locations/{locationId}/order-status/{orderId}"


def _ordering(event):
    location_id = http.path_id(event, "locationId")
    ctx = tenant.for_public(location_id, feature="ordering")
    params = http.query(event)
    unknown = sorted(set(params) - {"table"})
    if unknown:
        raise ValueError(f"unsupported query parameters: {', '.join(unknown)}")
    state = o.availability(ctx.location, ctx.tenant, o.now_utc())
    if params.get("table"):
        table_id, label = o.read_table_code(ctx.tenant_id, location_id, params["table"])
        state["forTable"] = {"label": label}
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
    return http.respond(200, o.guest_view(item, ctx.location))


def handler(event, context):
    method, path = http.route(event)
    if path not in (_ORDERING, _STATUS):
        return http.error(404, "not found")
    if method != "GET":
        return http.respond(405, {"error": "method not allowed"}, {"Allow": "GET"})
    try:
        return _ordering(event) if path == _ORDERING else _status(event)
    except tenant.TenantError as exc:
        return exc.response()
    except o.NotFound:
        return http.error(404, "not found")
    except ValueError as exc:
        return http.error(400, str(exc))
    except (BotoCoreError, ClientError, manage_link.LinkKeyUnavailable):
        logger.exception("order service unavailable")
        return http.error(503, "order service unavailable")

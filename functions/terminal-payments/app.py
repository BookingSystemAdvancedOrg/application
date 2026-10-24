"""terminal-payments

TRIGGER:
    * API Gateway -- Auth: JWT (owner_user, staff_user of the location's tenant)
      GET  /locations/{locationId}/card-payments?date=YYYY-MM-DD   the day's card payments
      POST /locations/{locationId}/card-payments
           {checkoutId, readerId, amount (öre) | orderId, note?}
           -> 201 the payment, already sent to the reader (status
           processing, or failed with reader_offline / reader_busy)
      GET  /locations/{locationId}/card-payments/readers           the location's readers
      GET  /locations/{locationId}/card-payments/{paymentId}       poll (asks Stripe when late)
      POST /locations/{locationId}/card-payments/{paymentId}/cancel   "Avbryt" on the reader
      POST /locations/{locationId}/card-payments/{paymentId}/retry    {readerId?} "Försök igen"
      POST /locations/{locationId}/card-payments/{paymentId}/refund   owner: "Återbetala"
      GET  /locations/{locationId}/card-payments/{paymentId}/receipt  the receipt (Kvitto)
      POST /locations/{locationId}/card-payments/{paymentId}/receipt  {email}: "Skicka kvitto"
      POST /locations/{locationId}/card-payments/{paymentId}/simulate {outcome: approve|decline}
           test mode only (not in prod): the simulated reader taps a card
    * Lambda Function URL (POST) -- Auth: NONE, the Stripe Connect endpoint
      for card readers (infrastructure payments/stripe "terminal"). Trust
      comes only from the Stripe-Signature header.
        payment_intent.succeeded            -> succeeded (+ the order settles)
        payment_intent.payment_failed       -> failed (current try only)
        terminal.reader.action_succeeded    -> the PaymentIntent is read, as above
        terminal.reader.action_failed       -> failed / canceled (current try only)
        charge.refunded                     -> refund recorded
      Events are matched by the PaymentIntent's metadata (locationId,
      paymentId) AND the event's connected account must be the payment's
      stripeAccountId AND the PaymentIntent id and amount must match. All
      writes are conditional, so duplicates and out-of-order deliveries are
      no-ops. Answers 200 when handled or ignored, 400 for a bad signature,
      500 on a transient failure (Stripe retries).

PURPOSE:
    "Ta betalt" in the admin app: in-person card payments on the
    restaurant's Stripe Terminal readers (shared/card_payments.py). Staff
    pick a reader of their own location; readers are registered in Booqy
    Console, never here.

    Multi-tenant: the token's tenant must own the location (else 404) and
    have the "terminal" feature; staff are limited to their own location;
    the Stripe account and Terminal Location come from the location row.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    USER_TABLE_NAME, ORDER_TABLE_NAME, STRIPE_SECRET_ARN, STRIPE_API_VERSION,
    TERMINAL_STRIPE_WEBHOOK_SECRET_ARN (whsec_..., plain string)

AWS RESOURCE ACCESS:
    Order table GetItem/PutItem/UpdateItem/Query (card payment records and
    the order being paid); the platform Stripe key and this endpoint's
    signing secret (Secrets Manager); tenant-context read policy.
"""

import base64
import json
import logging
import os
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from shared import card_messages, http, tenant
from shared import card_payments as cp
from shared import orders as o
from shared import stripe_client as stripe

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_COLLECTION = "/locations/{locationId}/card-payments"
_ITEM = "/locations/{locationId}/card-payments/{proxy+}"
_FEATURE = "terminal"

_SECRET_CACHE_SECONDS = 300
_secret = {"value": None, "at": 0.0}


# --- admin API -------------------------------------------------------------------------

def _ctx(event, *, owner_only=False):
    location_id = http.path_id(event, "locationId")
    return location_id, tenant.for_jwt(event, location_id=location_id, feature=_FEATURE, owner_only=owner_only)


def _payment(location_id, ctx, payment_id):
    item = cp.load(location_id, payment_id)
    if item.get("tenantId") != ctx.tenant_id:
        raise cp.NotFound
    return item


def _list(event):
    location_id, ctx = _ctx(event)
    params = http.query(event)
    unknown = sorted(set(params) - {"date"})
    if unknown:
        raise ValueError(f"unsupported query parameters: {', '.join(unknown)}")
    date_str = params.get("date") or o.local_now(ctx.location, o.now_utc()).date().isoformat()
    items = sorted(cp.list_day(location_id, date_str), key=lambda i: i.get("createdAt") or "", reverse=True)
    return http.respond(200, {"date": date_str, "enabled": bool((ctx.location.get("terminal") or {}).get("locationId")),
                              "payments": [cp.view(i) for i in items]})


def _create(event):
    location_id, ctx = _ctx(event)
    data = http.body(event, allowed={"checkoutId", "readerId", "amount", "orderId", "note"})
    if data.get("amount") is None and data.get("orderId") is None:
        raise ValueError("amount or orderId is required")
    item, created = cp.create(ctx, checkout_id=data.get("checkoutId"), reader_id=data.get("readerId"),
                              amount=data.get("amount"), order_id=data.get("orderId"), note=data.get("note"),
                              actor=ctx.sub, now=o.now_utc())
    logger.info(json.dumps({"card": "created" if created else "existing", "paymentId": item["paymentId"],
                            "status": item.get("status")}))
    return http.respond(201 if created else 200, cp.view(item))


def _readers(ctx):
    enabled = bool((ctx.location.get("terminal") or {}).get("locationId"))
    return http.respond(200, {"enabled": enabled, "readers": cp.list_readers(ctx.location) if enabled else []})


def _action(event, method, location_id, ctx, payment_id, action):
    now = o.now_utc()
    if action is None:
        _allow(method, "GET")
        return http.respond(200, cp.view(cp.sync(_payment(location_id, ctx, payment_id), now)))
    if action == "receipt" and method == "GET":
        item = _payment(location_id, ctx, payment_id)
        if item.get("status") == cp.SUCCEEDED and not item.get("card"):
            item = cp.fill_card(item)
        return http.respond(200, {"payment": cp.view(item),
                                  "receipt": card_messages.receipt(item, ctx.tenant, ctx.location)})
    _allow(method, "POST")
    if action == "cancel":
        http.body(event, allowed=set(), required=False)
        return http.respond(200, cp.view(cp.cancel(_payment(location_id, ctx, payment_id), actor=ctx.sub, now=now)))
    if action == "retry":
        data = http.body(event, allowed={"readerId"}, required=False)
        item = cp.retry(_payment(location_id, ctx, payment_id), ctx.location, reader_id=data.get("readerId"),
                        actor=ctx.sub, now=now)
        return http.respond(200, cp.view(item))
    if action == "refund":
        if not ctx.is_owner:
            raise tenant.TenantError(403, "owner_only")
        http.body(event, allowed=set(), required=False)
        item = cp.refund(_payment(location_id, ctx, payment_id), actor=ctx.sub, now=now)
        logger.info(json.dumps({"card": "refunded", "paymentId": payment_id}))
        return http.respond(200, cp.view(item))
    if action == "receipt":
        data = http.body(event, allowed={"email"})
        item = cp.send_receipt(_payment(location_id, ctx, payment_id), data.get("email"), actor=ctx.sub, now=now)
        return http.respond(200, cp.view(item))
    if action == "simulate":
        if os.environ.get("ENVIRONMENT") == "prod":
            return http.error(404, "not found")
        data = http.body(event, allowed={"outcome"})
        if data.get("outcome") not in ("approve", "decline"):
            raise ValueError("outcome must be approve or decline")
        cp.simulate(_payment(location_id, ctx, payment_id), outcome=data["outcome"])
        return http.respond(202, {"simulated": data["outcome"]})
    return http.error(404, "not found")


class _MethodNotAllowed(Exception):
    def __init__(self, allow):
        super().__init__(allow)
        self.allow = allow


def _allow(method, allowed):
    if method != allowed:
        raise _MethodNotAllowed(allowed)


def _item_route(event, method):
    location_id, ctx = _ctx(event)
    proxy = ((event.get("pathParameters") or {}).get("proxy") or "").strip("/")
    parts = proxy.split("/") if proxy else []
    if parts == ["readers"]:
        _allow(method, "GET")
        return _readers(ctx)
    if 1 <= len(parts) <= 2 and cp.valid_id(parts[0]):
        return _action(event, method, location_id, ctx, parts[0], parts[1] if len(parts) == 2 else None)
    return http.error(404, "not found")


def _api(event):
    method, path = http.route(event)
    try:
        if path == _COLLECTION:
            if method == "GET":
                return _list(event)
            if method == "POST":
                return _create(event)
            return http.respond(405, {"error": "method not allowed"}, {"Allow": "GET, POST"})
        if path == _ITEM:
            return _item_route(event, method)
        return http.error(404, "not found")
    except tenant.TenantError as exc:
        return exc.response()
    except _MethodNotAllowed as exc:
        return http.respond(405, {"error": "method not allowed"}, {"Allow": exc.allow})
    except (cp.NotFound, o.NotFound):
        return http.error(404, "not found")
    except (cp.InvalidState, o.InvalidState) as exc:
        return http.error(409, exc.code)
    except stripe.StripeError as exc:
        logger.error(json.dumps({"stripe": "rejected", "path": path, "code": exc.code, "message": str(exc)}))
        proxy = str((event.get("pathParameters") or {}).get("proxy") or "")
        return http.error(502, "refund_failed" if proxy.endswith("/refund") else "stripe_refused")
    except stripe.StripeUnavailable:
        return http.error(503, "payment_unavailable")
    except ValueError as exc:
        return http.error(400, str(exc))
    except (BotoCoreError, ClientError):
        logger.exception("card payment service unavailable")
        return http.error(503, "service unavailable")


# --- Stripe webhook ---------------------------------------------------------------------

def _signing_secret(force=False):
    now = time.monotonic()
    if force or _secret["value"] is None or now - _secret["at"] > _SECRET_CACHE_SECONDS:
        value = boto3.client("secretsmanager").get_secret_value(
            SecretId=os.environ["TERMINAL_STRIPE_WEBHOOK_SECRET_ARN"])["SecretString"]
        _secret.update(value=value.strip(), at=now)
    return _secret["value"]


def _webhook_response(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def _verify(event):
    body = event.get("body") or ""
    payload = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode("utf-8")
    header = http.header(event, "stripe-signature") or ""
    try:
        return stripe.verify_webhook(payload, header, _signing_secret())
    except stripe.InvalidSignature:
        # The endpoint may have been re-created with a new secret.
        return stripe.verify_webhook(payload, header, _signing_secret(force=True))


def _record(meta, account):
    meta = meta or {}
    location_id, payment_id = meta.get("locationId"), meta.get("paymentId")
    if not location_id or not cp.valid_id(payment_id):
        return None
    try:
        item = cp.load(location_id, payment_id)
    except cp.NotFound:
        return None
    if item.get("stripeAccountId") != account:
        logger.warning(json.dumps({"ignored": "account_mismatch", "paymentId": payment_id}))
        return None
    return item


def _on_intent(kind, intent, account, now, event_at):
    item = _record(intent.get("metadata"), account)
    if not item:
        return "ignored"
    if not cp.intent_matches(item, intent):
        logger.warning(json.dumps({"ignored": "intent_mismatch", "paymentId": item["paymentId"]}))
        return "ignored:mismatch"
    if intent.get("status") == "succeeded":
        return "succeeded" if cp.succeed(item, intent, now, actor="stripe") else "already_settled"
    if kind == "payment_intent.payment_failed" or intent.get("last_payment_error"):
        error = intent.get("last_payment_error") or {}
        code = error.get("decline_code") or error.get("code") or "card_declined"
        return "failed" if cp.fail(item, now, code=code, message=error.get("message"), event_at=event_at) \
            else "stale"
    return "waiting"


def _on_reader(kind, reader, account, now, event_at):
    action = reader.get("action") or {}
    intent_id = (action.get("process_payment_intent") or {}).get("payment_intent")
    if action.get("type") != "process_payment_intent" or not isinstance(intent_id, str):
        return "ignored:action"
    # Reader events carry no metadata: the PaymentIntent says whose it is.
    try:
        intent = stripe.request("GET", f"/v1/payment_intents/{intent_id}", account=account)
    except stripe.StripeError:
        return "ignored:unknown_intent"
    item = _record(intent.get("metadata"), account)
    if not item or not cp.intent_matches(item, intent) or item.get("readerId") != reader.get("id"):
        return "ignored"
    if intent.get("status") == "succeeded":
        return "succeeded" if cp.succeed(item, intent, now, actor="stripe") else "already_settled"
    if kind == "terminal.reader.action_failed":
        code = action.get("failure_code") or "reader_failed"
        return "failed" if cp.fail(item, now, code=code, message=action.get("failure_message"),
                                   event_at=event_at) else "stale"
    return "waiting"


def _on_refund(charge, account, now):
    item = _record(charge.get("metadata"), account)
    if not item or item.get("paymentIntentId") != charge.get("payment_intent"):
        return "ignored"
    return "recorded" if cp.record_refund(item, charge, now) else "already_recorded"


def _webhook(event):
    if ((event.get("requestContext") or {}).get("http") or {}).get("method", "POST") != "POST":
        return _webhook_response(405, {"error": "method not allowed"})
    try:
        stripe_event = _verify(event)
    except stripe.InvalidSignature as exc:
        logger.warning("rejected webhook: %s", exc)
        return _webhook_response(400, {"error": "invalid signature"})
    except (BotoCoreError, ClientError):
        logger.exception("signing secret unavailable")
        return _webhook_response(500, {"error": "unavailable"})

    if bool(stripe_event.get("livemode")) != (os.environ.get("ENVIRONMENT") == "prod"):
        return _webhook_response(200, {"ignored": "livemode"})
    account = stripe_event.get("account")
    obj = ((stripe_event.get("data") or {}).get("object")) or {}
    kind = stripe_event.get("type") or ""
    event_at = int(stripe_event.get("created") or 0) or None
    now = o.now_utc()
    try:
        if not account:
            result = "ignored:no_account"
        elif kind in ("payment_intent.succeeded", "payment_intent.payment_failed"):
            result = _on_intent(kind, obj, account, now, event_at)
        elif kind in ("terminal.reader.action_succeeded", "terminal.reader.action_failed"):
            result = _on_reader(kind, obj, account, now, event_at)
        elif kind == "charge.refunded":
            result = _on_refund(obj, account, now)
        else:
            result = "ignored:type"
    except (BotoCoreError, ClientError, stripe.StripeUnavailable):
        logger.exception("webhook %s failed transiently", stripe_event.get("id"))
        return _webhook_response(500, {"error": "retry"})
    logger.info(json.dumps({"event": stripe_event.get("id"), "type": kind, "result": result}))
    return _webhook_response(200, {"received": True, "result": result})


def handler(event, context):
    # A Function URL invocation always has routeKey "$default"; API Gateway
    # routes carry "<METHOD> <path>". The caller can't choose either.
    if (event or {}).get("routeKey") == "$default":
        return _webhook(event)
    return _api(event)

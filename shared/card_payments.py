"""In-person card payments on the restaurant's Stripe Terminal readers
("Ta betalt" in the admin app). Server-driven integration: smart readers
(S700, S710, WisePOS E) take their orders from the Stripe API, so the web
admin and these Lambdas are the whole till - no SDK, no native app, and no
card data ever reaches us (the reader encrypts everything for Stripe).

Readers and payments live on the restaurant's connected account, the one in
the location's ``terminal`` (set up in Booqy Console, see infrastructure
docs/handoff/TERMINAL.md): ``terminal.accountId`` and ``terminal.locationId``
(the Stripe Terminal Location). They are read from the location row, never
from a request, and every reader is checked against that Terminal Location,
so staff can only charge on their own restaurant's readers.

Record (order table, next to the location's orders):
    PK LOCATION#<locationId>   SK CARD#<date>#<paymentId>   the payment
    PK LOCATION#<locationId>   SK CID#<paymentId>           pointer -> date

    paymentId        32 hex, the till's checkoutId: a double click or a
                     retried POST finds the same record
    amount           öre (SEK)
    orderId          optional: an unpaid (pending_payment) order of the location;
                     once paid its lines, vat and orderNumber are copied here
                     for the receipt
    note             optional (free amount)
    readerId, readerLabel, readerType
    stripeAccountId, paymentIntentId   ONE PaymentIntent per checkout
    status           processing -> succeeded | failed | canceled
                     failed | canceled -> processing        "Försök igen"
                     processing | failed | canceled -> succeeded
                         (a late success always wins: the money is taken)
                     succeeded -> refunded
    attempt, attemptAt   the current try on a reader; events from an
                     earlier try never change a newer one
    failure          {code, message}
    card             brand, last4 and the EMV fields a card receipt needs
    refund           {amount, at, refundId}
    receiptEmail, notice   "Skicka kvitto" (notification sends it)

Never a ``paymentStatus`` attribute: that is the order's field, and the
order stream's readers (marketing-sync, print-jobs) filter on it.

Idempotency: the PaymentIntent is created with key card-pi-<paymentId>,
each reader try with card-process-<paymentId>-<attempt>, the refund with
card-refund-<paymentId>. A PaymentIntent can only succeed once, so retries
on it can never charge twice; an order gets one card payment for good
(``cardPaymentId`` on the order).

Paying an order: the order's own Checkout Session is expired first, so the
guest can't also pay online; the order then settles exactly like an online
payment (order_payments.settle_paid): confirmed, the day's number, the
receipt e-mail and the kitchen ticket (print-jobs) all follow from it.
"""

import re
from datetime import date, timedelta

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from shared import dynamo, order_payments, orders as o
from shared import stripe_client as stripe

MIN_ORE = 300            # Stripe's minimum charge in SEK (3 kr)
MAX_ORE = o.MAX_ORDER_ORE
NOTE_MAX = 120
MAX_RECEIPTS = 5         # receipt e-mails per payment
SYNC_AFTER_SECONDS = 4   # the till's poll asks Stripe once a try is this old
ORDER_HOLD_MINUTES = 60  # a pending order being paid at the till is kept this long

PROCESSING = "processing"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELED = "canceled"
REFUNDED = "refunded"
RETRYABLE = (FAILED, CANCELED)

NOTICE_RECEIPT = "card_receipt"

_ID = re.compile(r"[0-9a-f]{32}")
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,24}$")

# Stripe error codes -> what the till shows.
_READER_ERRORS = {
    "terminal_reader_offline": "reader_offline",
    "terminal_reader_busy": "reader_busy",
    "terminal_reader_timeout": "reader_timeout",
    "terminal_reader_hardware_fault": "reader_fault",
}


class NotFound(Exception):
    pass


class InvalidState(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


# --- small helpers -------------------------------------------------------------------

def valid_id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def card_key(location_id, date_str, payment_id):
    return {"PK": o.location_pk(location_id), "SK": f"CARD#{date_str}#{payment_id}"}


def pointer_key(location_id, payment_id):
    return {"PK": o.location_pk(location_id), "SK": f"CID#{payment_id}"}


def _table():
    return dynamo.table(o.order_table())


def _conditional_failed(exc):
    return isinstance(exc, ClientError) and \
        exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _epoch(iso):
    try:
        return int(o.parse_iso(iso).timestamp())
    except (AttributeError, TypeError, ValueError):
        return 0


def terminal_of(location):
    """(accountId, Stripe Terminal Location id) of the location, or
    InvalidState("terminal_not_enabled")."""
    term = (location or {}).get("terminal") or {}
    account, terminal_location = term.get("accountId"), term.get("locationId")
    if not account or not terminal_location:
        raise InvalidState("terminal_not_enabled")
    return account, terminal_location


def validate_amount(value):
    """Öre, a whole number (the till converts "125,50 kr")."""
    return o._whole(value, MIN_ORE, MAX_ORE, "amount")


def clean_note(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("note must be text")
    value = " ".join(value.split())
    if len(value) > NOTE_MAX:
        raise ValueError(f"note must be at most {NOTE_MAX} characters")
    return value or None


def clean_email(value):
    if not isinstance(value, str) or not _EMAIL.fullmatch(value.strip()):
        raise ValueError("email is invalid")
    return value.strip().lower()


# --- readers ---------------------------------------------------------------------------

def _reader_view(reader):
    return {
        "id": reader.get("id"),
        "label": reader.get("label") or reader.get("serial_number") or reader.get("id"),
        "deviceType": reader.get("device_type"),
        "status": reader.get("status") or "offline",
        "simulated": str(reader.get("device_type") or "").startswith("simulated"),
    }


def list_readers(location):
    account, terminal_location = terminal_of(location)
    page = stripe.request("GET", "/v1/terminal/readers", {"location": terminal_location, "limit": 100},
                          account=account)
    return [_reader_view(r) for r in page.get("data") or [] if not r.get("deleted")]


def reader_of(location, reader_id):
    """The reader, if it belongs to this location's Terminal Location on
    this restaurant's account - else NotFound (another restaurant's reader
    is not even visible on this account)."""
    if not isinstance(reader_id, str) or not re.fullmatch(r"tmr_[A-Za-z0-9]{1,64}", reader_id):
        raise ValueError("readerId is invalid")
    account, terminal_location = terminal_of(location)
    try:
        reader = stripe.request("GET", f"/v1/terminal/readers/{reader_id}", account=account)
    except stripe.StripeError as exc:
        if exc.status == 404:
            raise NotFound from None
        raise
    if reader.get("deleted") or reader.get("location") != terminal_location:
        raise NotFound
    return reader


# --- record ----------------------------------------------------------------------------

def load(location_id, payment_id):
    if not valid_id(payment_id):
        raise NotFound
    pointer = _table().get_item(Key=pointer_key(location_id, payment_id), ConsistentRead=True).get("Item")
    if not pointer or not isinstance(pointer.get("date"), str):
        raise NotFound
    item = _table().get_item(Key=card_key(location_id, pointer["date"], payment_id),
                             ConsistentRead=True).get("Item")
    if not item or item.get("locationId") != location_id:
        raise NotFound
    return item


def list_day(location_id, date_str):
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str or ""):
            raise ValueError
        date.fromisoformat(date_str)
    except (TypeError, ValueError):
        raise ValueError("date must be YYYY-MM-DD") from None
    request = {"KeyConditionExpression": Key("PK").eq(o.location_pk(location_id))
               & Key("SK").begins_with(f"CARD#{date_str}#"), "ConsistentRead": True}
    items = []
    while True:
        page = _table().query(**request)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return items
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def view(item):
    out = {k: item.get(k) for k in (
        "paymentId", "locationId", "date", "amount", "currency", "orderId", "orderNumber", "note",
        "readerId", "readerLabel", "status", "attempt", "attemptAt", "failure", "card", "refund",
        "createdAt", "createdBy", "paidAt", "receiptsSent")}
    out["simulated"] = str(item.get("readerType") or "").startswith("simulated")
    return {k: v for k, v in out.items() if v is not None}


def _update(item, now, sets, values, *, condition, names=None, removes=()):
    names = {"#s": "status", **(names or {})}
    values = {**values, ":now": o.iso(now)}
    expression = "SET " + ", ".join([*sets, "updatedAt = :now", "version = if_not_exists(version, :zero) + :one"])
    if removes:
        expression += " REMOVE " + ", ".join(removes)
    try:
        result = _table().update_item(
            Key=card_key(item["locationId"], item["date"], item["paymentId"]),
            UpdateExpression=expression,
            ConditionExpression="attribute_exists(PK) AND " + condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues={":zero": 0, ":one": 1, **values},
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if _conditional_failed(exc):
            return None
        raise
    return result["Attributes"]


def _entry(action, actor, now, **extra):
    return [o.history_entry(action, actor, now, **extra)]


# --- the order being paid --------------------------------------------------------------

def _claim_order(order, payment_id, account, now):
    """Marks the order as paid at the till by this checkout (one card
    payment per order, ever) and closes its online checkout. Returns the
    payment id that owns the order: this one, or the one that already did."""
    if order.get("cardPaymentId"):
        return order["cardPaymentId"]
    if order.get("status") != o.PENDING_PAYMENT:
        raise InvalidState("order_not_payable")
    if order.get("stripeAccountId") and order["stripeAccountId"] != account:
        raise InvalidState("order_other_account")
    try:
        _table().update_item(
            Key=o.order_key(order["locationId"], order["date"], order["orderId"]),
            UpdateExpression="SET cardPaymentId = :p, #ttl = :ttl, updatedAt = :now",
            ConditionExpression="attribute_exists(PK) AND #s = :pending AND attribute_not_exists(cardPaymentId)",
            ExpressionAttributeNames={"#s": "status", "#ttl": "ttl"},
            ExpressionAttributeValues={":p": payment_id, ":pending": o.PENDING_PAYMENT, ":now": o.iso(now),
                                       ":ttl": int((now + timedelta(minutes=ORDER_HOLD_MINUTES)).timestamp())},
        )
    except ClientError as exc:
        if not _conditional_failed(exc):
            raise
        fresh = o.load(order["locationId"], order["orderId"])
        if fresh.get("cardPaymentId"):
            return fresh["cardPaymentId"]
        raise InvalidState("order_not_payable") from None
    if order.get("checkoutSessionId") and order.get("stripeAccountId"):
        _close_checkout(order, payment_id, now)
    return payment_id


def _close_checkout(order, payment_id, now):
    """Expires the order's online Checkout Session. If the guest has paid
    online meanwhile, the order is settled from it and this checkout is
    refused (order_already_paid) - never two payments for one order."""
    try:
        stripe.request("POST", f"/v1/checkout/sessions/{order['checkoutSessionId']}/expire",
                       account=order["stripeAccountId"])
        return
    except stripe.StripeError:
        session = order_payments.retrieve_session(order)
    if session.get("status") == "expired":
        return
    if session.get("payment_status") == "paid" and order_payments.session_matches(session, order):
        _table().update_item(Key=o.order_key(order["locationId"], order["date"], order["orderId"]),
                             UpdateExpression="REMOVE cardPaymentId",
                             ConditionExpression="cardPaymentId = :p",
                             ExpressionAttributeValues={":p": payment_id})
        order_payments.settle_paid(order, session, now, "stripe")
        raise InvalidState("order_already_paid")
    raise InvalidState("order_checkout_open")


def _settle_order(item, now):
    if not item.get("orderId") or item.get("status") not in (SUCCEEDED, REFUNDED):
        return None
    try:
        order = o.load(item["locationId"], item["orderId"])
    except o.NotFound:
        return None
    if order.get("cardPaymentId") != item["paymentId"]:
        return None
    settled = order_payments.settle_paid(order, {"payment_intent": item["paymentIntentId"]}, now, "terminal",
                                         method="card_present") or order
    if settled.get("status") != o.PENDING_PAYMENT and "lines" not in item:
        # The receipt's lines, VAT and order number travel with the payment.
        lines = [{k: line[k] for k in ("name", "quantity", "total", "vatRate") if k in line}
                 for line in settled.get("lines") or []]
        _update(item, now, ["#l = :lines", "vat = :vat", "orderNumber = :number"],
                {":lines": lines, ":vat": settled.get("vat") or [], ":number": settled.get("number") or 0},
                names={"#l": "lines"}, condition="attribute_exists(#s)")
    return settled


# --- create and send to the reader --------------------------------------------------------

def create(ctx, *, checkout_id, reader_id, amount=None, order_id=None, note=None, actor, now):
    """POST /card-payments: the checkout's PaymentIntent, sent to the reader.
    Returns (item, created)."""
    if not valid_id(checkout_id):
        raise ValueError("checkoutId must be 32 hex characters")
    location = ctx.location
    location_id = location["locationId"]
    try:
        existing = load(location_id, checkout_id)
    except NotFound:
        existing = None
    if existing:
        if existing.get("tenantId") != ctx.tenant_id:
            raise NotFound
        return existing, False

    account, _ = terminal_of(location)
    reader = reader_of(location, reader_id)
    order = None
    payment_id = checkout_id
    if order_id is not None:
        if amount is not None:
            raise ValueError("send either amount or orderId")
        order = o.load(location_id, order_id)
        if order.get("tenantId") != ctx.tenant_id:
            raise o.NotFound
        owner = _claim_order(order, payment_id, account, now)
        if owner != payment_id:
            # The order is already being paid by another checkout: carry on with that one.
            try:
                return load(location_id, owner), False
            except NotFound:
                raise InvalidState("order_being_paid") from None
        amount = int(order["totalAmount"])
    else:
        amount = validate_amount(amount)
    note = clean_note(note)
    date_str = o.local_now(location, now).date().isoformat()

    try:
        _table().put_item(Item={**pointer_key(location_id, payment_id), "date": date_str,
                                "tenantId": ctx.tenant_id, "locationId": location_id},
                          ConditionExpression="attribute_not_exists(PK)")
    except ClientError as exc:
        if not _conditional_failed(exc):
            raise
        return load(location_id, payment_id), False

    metadata = {"tenantId": ctx.tenant_id, "locationId": location_id, "paymentId": payment_id,
                "date": date_str, "source": "booqy-terminal"}
    if order:
        metadata["orderId"] = order["orderId"]
    description = f"Beställning {order['orderId'][:8]}" if order else (note or "Kortbetalning")
    intent = stripe.request("POST", "/v1/payment_intents", {
        "amount": amount, "currency": o.CURRENCY, "payment_method_types": ["card_present"],
        "capture_method": "automatic", "description": description[:200], "metadata": metadata,
    }, account=account, idempotency_key=f"card-pi-{payment_id}")

    item = {
        **card_key(location_id, date_str, payment_id),
        "paymentId": payment_id, "tenantId": ctx.tenant_id, "locationId": location_id, "date": date_str,
        "timezone": location.get("timezone") or "Europe/Stockholm",
        "amount": amount, "currency": o.CURRENCY, "orderId": order["orderId"] if order else None,
        "orderNumber": order.get("number") if order else None, "note": note,
        "readerId": reader["id"], "readerLabel": reader.get("label") or reader["id"],
        "readerType": reader.get("device_type"),
        "stripeAccountId": account, "paymentIntentId": intent["id"],
        "status": PROCESSING, "attempt": 1, "attemptAt": o.iso(now),
        "createdAt": o.iso(now), "createdBy": actor, "updatedAt": o.iso(now), "version": 1,
        "history": _entry("created", actor, now, amount=amount, reader=reader["id"]),
    }
    item = {k: v for k, v in item.items() if v is not None}
    _table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    return _process(item, reader, now), True


def _process(item, reader, now):
    """Sends the record's current try to its reader. A refusal (offline,
    busy) makes the record failed with that code; the till offers
    "Försök igen"."""
    try:
        stripe.request("POST", f"/v1/terminal/readers/{reader['id']}/process_payment_intent",
                       {"payment_intent": item["paymentIntentId"]}, account=item["stripeAccountId"],
                       idempotency_key=f"card-process-{item['paymentId']}-{item['attempt']}")
    except stripe.StripeError as exc:
        code = _READER_ERRORS.get(exc.code or "", "reader_refused")
        if reader.get("status") == "offline":
            code = "reader_offline"
        failed = fail(item, now, code=code, message=str(exc)[:200], attempt=int(item["attempt"]))
        return failed or load(item["locationId"], item["paymentId"])
    return item


def retry(item, location, *, reader_id=None, actor, now):
    """"Försök igen": the same PaymentIntent on the same (or another)
    reader. If Stripe already took the money, settles instead."""
    if item.get("status") not in RETRYABLE:
        raise InvalidState(f"cannot_retry_{item.get('status')}")
    intent = _intent(item)
    if intent.get("status") == "succeeded":
        return succeed(item, intent, now, actor="terminal") or load(item["locationId"], item["paymentId"])
    if intent.get("status") == "canceled":
        raise InvalidState("payment_canceled")
    reader = reader_of(location, reader_id or item["readerId"])
    attempt = int(item.get("attempt") or 1) + 1
    updated = _update(item, now, [
        "#s = :processing", "attempt = :attempt", "attemptAt = :now", "readerId = :reader",
        "readerLabel = :label", "readerType = :type",
        "history = list_append(if_not_exists(history, :empty), :entry)"],
        {":processing": PROCESSING, ":attempt": attempt, ":now": o.iso(now), ":reader": reader["id"],
         ":label": reader.get("label") or reader["id"], ":type": reader.get("device_type") or "-",
         ":empty": [], ":entry": _entry("retry", actor, now, attempt=attempt, reader=reader["id"]),
         ":expected": item["status"], ":prev": int(item.get("attempt") or 1)},
        condition="#s = :expected AND attempt = :prev", removes=("failure",))
    if not updated:
        raise InvalidState("changed_retry")
    return _process(updated, reader, now)


def cancel(item, *, actor, now):
    """"Avbryt": clears the reader's screen. If the card went through in the
    meantime, the payment is settled instead (the money is taken)."""
    if item.get("status") != PROCESSING:
        raise InvalidState(f"cannot_cancel_{item.get('status')}")
    try:
        stripe.request("POST", f"/v1/terminal/readers/{item['readerId']}/cancel_action",
                       account=item["stripeAccountId"])
    except stripe.StripeError:
        pass  # nothing on the reader any more (finished or timed out): Stripe decides below
    intent = _intent(item)
    if intent.get("status") == "succeeded":
        return succeed(item, intent, now, actor="terminal") or load(item["locationId"], item["paymentId"])
    updated = _update(item, now, ["#s = :canceled", "history = list_append(if_not_exists(history, :empty), :entry)"],
                      {":canceled": CANCELED, ":processing": PROCESSING, ":attempt": int(item["attempt"]),
                       ":empty": [], ":entry": _entry("canceled", actor, now)},
                      condition="#s = :processing AND attempt = :attempt")
    return updated or load(item["locationId"], item["paymentId"])


# --- outcomes ---------------------------------------------------------------------------

def _intent(item, *, expand=False):
    params = {"expand": ["latest_charge"]} if expand else None
    return stripe.request("GET", f"/v1/payment_intents/{item['paymentIntentId']}", params,
                          account=item["stripeAccountId"])


def card_details(charge):
    """What a Swedish card receipt shows, from the charge's card_present
    details (no card number beyond the last four digits)."""
    if not isinstance(charge, dict):
        return None
    cp = ((charge.get("payment_method_details") or {}).get("card_present")) or {}
    receipt = cp.get("receipt") or {}
    out = {
        "brand": cp.get("brand"), "last4": cp.get("last4"), "readMethod": cp.get("read_method"),
        "wallet": ((cp.get("wallet") or {}).get("type")),
        "applicationName": receipt.get("application_preferred_name"),
        "aid": receipt.get("dedicated_file_name"),
        "authorizationCode": receipt.get("authorization_code") or charge.get("authorization_code"),
        "authorizationResponseCode": receipt.get("authorization_response_code"),
        "accountType": receipt.get("account_type"),
        "verification": receipt.get("cardholder_verification_method"),
        "tvr": receipt.get("terminal_verification_results"),
        "tsi": receipt.get("transaction_status_information"),
        "chargeId": charge.get("id"),
    }
    return {k: v for k, v in out.items() if v}


def intent_matches(item, intent):
    return (intent.get("id") == item.get("paymentIntentId")
            and int(intent.get("amount") or 0) == int(item.get("amount") or -1)
            and (intent.get("currency") or "").lower() == o.CURRENCY)


def succeed(item, intent, now, *, actor):
    """Any not-yet-paid state -> succeeded (the money is taken; a late
    success beats a cancel or an earlier failure). Then the order, if any,
    settles. Returns the updated item, or None when it already was."""
    if not intent_matches(item, intent):
        return None
    charge = intent.get("latest_charge")
    if not isinstance(charge, dict):
        try:
            charge = _intent(item, expand=True).get("latest_charge")
        except (stripe.StripeError, stripe.StripeUnavailable):
            charge = None  # the receipt fetches it later
    card = card_details(charge) or {}
    updated = _update(item, now, ["#s = :succeeded", "paidAt = if_not_exists(paidAt, :now)", "card = :card",
                             "history = list_append(if_not_exists(history, :empty), :entry)"],
                      {":succeeded": SUCCEEDED, ":now": o.iso(now), ":card": card, ":empty": [],
                       ":entry": _entry("paid", actor, now), ":p": PROCESSING, ":f": FAILED, ":c": CANCELED},
                      condition="#s IN (:p, :f, :c)", removes=("failure",))
    _settle_order(updated or load(item["locationId"], item["paymentId"]), now)
    return updated


def fail(item, now, *, code, message=None, attempt=None, event_at=None):
    """processing -> failed (or canceled), only for the current try: an
    event older than the try, or for an earlier try, changes nothing."""
    if item.get("status") != PROCESSING:
        return None
    if event_at is not None and event_at < _epoch(item.get("attemptAt")):
        return None
    target = CANCELED if "cancel" in (code or "") else FAILED
    return _update(item, now, ["#s = :target", "failure = :failure",
                          "history = list_append(if_not_exists(history, :empty), :entry)"],
                   {":target": target, ":failure": {"code": code, "message": message or ""},
                    ":processing": PROCESSING, ":attempt": int(attempt or item.get("attempt") or 1),
                    ":empty": [], ":entry": _entry(target, "stripe", now, code=code)},
                   condition="#s = :processing AND attempt = :attempt")


def _failure_of(intent):
    error = intent.get("last_payment_error") or {}
    return error.get("decline_code") or error.get("code") or "card_declined", error.get("message")


def sync(item, now):
    """The till's poll, a fallback for a late webhook: once the current try
    is a few seconds old, ask Stripe. Returns the current item."""
    if item.get("status") != PROCESSING or now.timestamp() - _epoch(item.get("attemptAt")) < SYNC_AFTER_SECONDS:
        if item.get("status") == SUCCEEDED and not item.get("card"):
            return fill_card(item)
        return item
    intent = _intent(item, expand=True)
    if intent.get("status") == "succeeded":
        return succeed(item, intent, now, actor="terminal") or load(item["locationId"], item["paymentId"])
    try:
        reader = stripe.request("GET", f"/v1/terminal/readers/{item['readerId']}", account=item["stripeAccountId"])
    except stripe.StripeError:
        reader = {}
    action = reader.get("action") or {}
    mine = ((action.get("process_payment_intent") or {}).get("payment_intent")) == item["paymentIntentId"]
    if mine and action.get("status") == "failed":
        result = fail(item, now, code=action.get("failure_code") or "reader_failed",
                      message=action.get("failure_message"))
        return result or load(item["locationId"], item["paymentId"])
    if not action and intent.get("last_payment_error"):
        code, message = _failure_of(intent)
        return fail(item, now, code=code, message=message) or load(item["locationId"], item["paymentId"])
    return item


def fill_card(item):
    """Card details for the receipt when the success came without them."""
    try:
        charge = _intent(item, expand=True).get("latest_charge")
    except (stripe.StripeError, stripe.StripeUnavailable):
        return item
    card = card_details(charge)
    if not card:
        return item
    updated = _update(item, o.now_utc(), ["card = :card"], {":card": card},
                      condition="attribute_exists(#s)")
    return updated or item


# --- refunds and receipts -------------------------------------------------------------------

def refund(item, *, actor, now):
    """Owner: the whole payment back to the card. A payment for an order is
    refunded by cancelling the order (Beställningar), so the kitchen and the
    guest's messages stay right."""
    if item.get("orderId"):
        raise InvalidState("refund_via_order")
    if item.get("status") != SUCCEEDED:
        raise InvalidState(f"cannot_refund_{item.get('status')}")
    result = stripe.request("POST", "/v1/refunds", {
        "payment_intent": item["paymentIntentId"], "reason": "requested_by_customer",
        "metadata": {"paymentId": item["paymentId"], "locationId": item["locationId"], "by": actor},
    }, account=item["stripeAccountId"], idempotency_key=f"card-refund-{item['paymentId']}")
    amount = int(result.get("amount") or item["amount"])
    updated = _update(item, now, ["#s = :refunded", "#rf = :refund",
                             "history = list_append(if_not_exists(history, :empty), :entry)"],
                      {":refunded": REFUNDED, ":succeeded": SUCCEEDED, ":empty": [],
                       ":refund": {"amount": amount, "at": o.iso(now), "refundId": result.get("id"),
                                   "status": result.get("status")},
                       ":entry": _entry("refunded", actor, now, amount=amount)},
                      names={"#rf": "refund"}, condition="#s = :succeeded")
    return updated or load(item["locationId"], item["paymentId"])


def record_refund(item, charge, now):
    """charge.refunded (also refunds made by cancelling the order, or in the
    Stripe Dashboard). Returns the updated item or None."""
    refunded = int(charge.get("amount_refunded") or 0)
    if refunded <= int((item.get("refund") or {}).get("amount") or 0):
        return None
    sets = ["#rf = :refund", "history = list_append(if_not_exists(history, :empty), :entry)"]
    values = {":refund": {"amount": refunded, "at": o.iso(now)}, ":empty": [], ":amount": refunded,
              ":entry": _entry("refunded", "stripe", now, amount=refunded), ":now": o.iso(now),
              ":succeeded": SUCCEEDED, ":refunded": REFUNDED}
    if refunded >= int(item.get("amount") or 0):
        sets.append("#s = :refunded")
    return _update(item, now, sets, values, names={"#rf": "refund"},
                   condition="#s IN (:succeeded, :refunded) AND (attribute_not_exists(#rf) OR #rf.amount < :amount)")


def send_receipt(item, email, *, actor, now):
    """"Skicka kvitto": the notification function e-mails the receipt (the
    order table's stream, a notice like the orders' own)."""
    if item.get("status") not in (SUCCEEDED, REFUNDED):
        raise InvalidState("not_paid")
    email = clean_email(email)
    updated = _update(item, now, ["receiptEmail = :email", "notice = :notice",
                             "receiptsSent = if_not_exists(receiptsSent, :zero) + :one",
                             "history = list_append(if_not_exists(history, :empty), :entry)"],
                      {":email": email, ":notice": {"id": o.new_id(), "type": NOTICE_RECEIPT, "at": o.iso(now)},
                       ":max": MAX_RECEIPTS, ":empty": [], ":succeeded": SUCCEEDED, ":refunded": REFUNDED,
                       ":entry": _entry("receipt_sent", actor, now)},
                      condition="#s IN (:succeeded, :refunded) AND "
                                "(attribute_not_exists(receiptsSent) OR receiptsSent < :max)")
    if not updated:
        raise InvalidState("receipt_limit")
    return updated


def simulate(item, *, outcome):
    """Test mode only: the simulated reader "taps" a card (approved or
    declined). Stripe answers 4xx for this on a live key."""
    if item.get("status") != PROCESSING:
        raise InvalidState(f"cannot_simulate_{item.get('status')}")
    if not str(item.get("readerType") or "").startswith("simulated"):
        raise InvalidState("not_simulated")
    number = "4000000000000002" if outcome == "decline" else "4242424242424242"
    return stripe.request("POST", f"/v1/test_helpers/terminal/readers/{item['readerId']}/present_payment_method",
                          {"type": "card_present", "card_present": {"number": number}},
                          account=item["stripeAccountId"])


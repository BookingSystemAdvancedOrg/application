"""Pay-later QR table orders: the table's open bill ("nota") and paying it.

A table order placed with ``payLater`` goes to the kitchen at once
(status confirmed, paymentStatus unpaid). Every unpaid order of one table and
one service day (orders.service_date) is on that table's open bill. Staff
charge it before the guests leave:

    * the guest pays the whole bill with the phone (Stripe Checkout, Payment
      Element, the restaurant's connected account, direct charge),
    * staff record that the guest paid outside Booqy (own card terminal,
      cash, Swish, other): paymentStatus paid_outside, no Booqy receipt,
    * the owner writes it off (written_off), or staff cancel an unpaid
      order (void, nothing to refund).

Records (order table, partition LOCATION#<locationId>):

    TABLETAB#<serviceDate>#<tableId>   the open bill of a table: one item, the
        single lock point and the cap counter.
        recordType "tableTab", tenantId, locationId, tableId, tableLabel,
        serviceDate, open_<orderId> {date, number, total, at} per unpaid
        order, openCount, openTotal (öre), paying {billId, orderIds, amount,
        at, expiresAt} only while a guest pays, lastPaidAt, mismatch?,
        updatedAt, version, ttl (only when empty: now + 3 days).
    BILL#<billId>   one payment attempt: a snapshot of the orders it pays
        (number, lines, totals), amount, vat, stripeAccountId,
        checkoutSessionId, paymentIntentId, payerEmail?, status
        paying | paid | expired | failed, notice bill_paid (receipt e-mail
        to payerEmail), ttl while not paid (now + 2 days).
    AUDIT#ORDER#<orderId>#<iso>#<rand>   staff actions on unpaid orders
        (pay_link, paid_outside, written_off, cancelled_unpaid).

Concurrency: placing an order adds to the table's bill atomically (ADD with
a cap condition). Paying locks the bill (``paying``) to the amount and the
orders shown; an order placed meanwhile is added but not in the locked set,
so it stays on the bill after the payment (the next bill). The Checkout
Session has idempotency key bill-checkout-<billId>, and a lock older than
LOCK_SECONDS can be taken over only after its session is expired at Stripe
(or found paid, which settles it): nobody is charged twice. Settling writes
the bill, the table and every order in one transaction; the webhook, the
guest page and a takeover may all try, the first wins.

Gift cards and welcome codes are not used on table bills: their holds are
per order and a bill covers several orders (docs/ORDERS.md).
"""

import json
import logging
import re
import secrets
from datetime import timedelta

from boto3.dynamodb.conditions import Attr, Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from shared import dynamo, order_payments, safe_compare, table_qr
from shared import orders as o
from shared import stripe_client as stripe

logger = logging.getLogger()

TAB_PREFIX = "TABLETAB#"
BILL_PREFIX = "BILL#"
AUDIT_PREFIX = "AUDIT#ORDER#"
ENTRY = "open_"                 # attribute prefix of one unpaid order on the bill
LOCK_SECONDS = 120              # an abandoned payment holds the bill at most this long
LINK_HOURS = 12                 # the staff pay link (QR code) is valid this long
TAB_TTL_DAYS = 3
BILL_TTL_DAYS = 2
MIN_CHARGE_ORE = 300            # Stripe's smallest card charge in SEK
KIND = "table_bill"             # Checkout metadata kind

PAYING, PAID, EXPIRED, FAILED = "paying", "paid", "expired", "failed"
NOTICE_BILL_PAID = "bill_paid"
NOTICE_TYPES = (NOTICE_BILL_PAID,)

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TABLE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_NOTE_MAX = 200
_ser = TypeSerializer()


class BillError(Exception):
    """A refused bill action: 409 (or ``status``) with ``code``; ``extra``
    goes into the answer (e.g. the fresh bill on bill_changed)."""

    def __init__(self, code, *, status=409, **extra):
        super().__init__(code)
        self.code = code
        self.status = status
        self.extra = extra


# --- keys and small helpers --------------------------------------------------------------

def valid_service_date(value):
    return isinstance(value, str) and _DATE.fullmatch(value) is not None


def valid_table_id(value):
    return isinstance(value, str) and _TABLE_ID.fullmatch(value) is not None


def tab_key(location_id, service_date, table_id):
    return {"PK": o.location_pk(location_id), "SK": f"{TAB_PREFIX}{service_date}#{table_id}"}


def bill_key(location_id, bill_id):
    return {"PK": o.location_pk(location_id), "SK": f"{BILL_PREFIX}{bill_id}"}


def _table():
    return dynamo.table(o.order_table())


def _av_key(key):
    return {k: {"S": v} for k, v in key.items()}


def _avs(values):
    return {k: _ser.serialize(v) for k, v in values.items()}


def _update(key, expression, condition, names, values):
    """A TransactWriteItems Update (low-level attribute values)."""
    op = {"TableName": o.order_table(), "Key": _av_key(key), "UpdateExpression": expression,
          "ExpressionAttributeNames": names}
    if values:
        op["ExpressionAttributeValues"] = _avs(values)
    if condition:
        op["ConditionExpression"] = condition
    return {"Update": op}


def _cancelled(exc):
    return isinstance(exc, ClientError) and \
        exc.response.get("Error", {}).get("Code") in ("TransactionCanceledException", "ConditionalCheckFailedException")


def _epoch(at):
    return int(at.timestamp())


def entries(tab):
    """{orderId: {date, number, total, at}} of the unpaid orders on the bill."""
    return {k[len(ENTRY):]: v for k, v in (tab or {}).items() if k.startswith(ENTRY) and isinstance(v, dict)}


def load_tab(location_id, service_date, table_id):
    return _table().get_item(Key=tab_key(location_id, service_date, table_id), ConsistentRead=True).get("Item")


def load_bill(location_id, bill_id):
    if not o.valid_order_id(bill_id):
        return None
    return _table().get_item(Key=bill_key(location_id, bill_id), ConsistentRead=True).get("Item")


def _load_orders(location_id, open_entries):
    """The orders on the bill (GetItem each: at most TABLE_MAX_OPEN_ORDERS)."""
    out = []
    for order_id, entry in open_entries.items():
        item = _table().get_item(Key=o.order_key(location_id, entry.get("date"), order_id),
                                 ConsistentRead=True).get("Item")
        if item:
            out.append(item)
    out.sort(key=lambda i: (i.get("createdAt") or "", int(i.get("number") or 0), i.get("orderId")))
    return out


def title(tab_or_bill):
    return table_qr.table_title(str((tab_or_bill or {}).get("tableLabel") or ""))


# --- tokens -------------------------------------------------------------------------------

def link_token(tenant_id, location_id, service_date, table_id, now):
    """The pay link staff show as a QR code: L.<payload>.<signature>, valid LINK_HOURS."""
    expires = now + timedelta(hours=LINK_HOURS)
    payload = o._b64(json.dumps({"d": service_date, "t": table_id, "e": _epoch(expires)},
                                separators=(",", ":")).encode("utf-8"))
    return f"L.{payload}.{o._sign('table-bill-link', 'v1', tenant_id, location_id, payload)}", expires


def read_link_token(tenant_id, location_id, token, now):
    """(serviceDate, tableId) of a valid pay link, else o.NotFound."""
    if not isinstance(token, str) or len(token) > 512 or not token.startswith("L.") or token.count(".") != 2:
        raise o.NotFound
    _, payload, signature = token.split(".")
    if not safe_compare.equal(o._sign("table-bill-link", "v1", tenant_id, location_id, payload), signature):
        raise o.NotFound
    try:
        data = json.loads(o._unb64(payload))
        service_date, table_id, expires = data["d"], data["t"], int(data["e"])
    except (ValueError, KeyError, TypeError):
        raise o.NotFound from None
    if expires < _epoch(now) or not valid_service_date(service_date) or not valid_table_id(table_id):
        raise o.NotFound
    return service_date, table_id


def bill_token(tenant_id, location_id, bill_id):
    return o._sign("table-bill", "v1", tenant_id, location_id, bill_id)


def bill_token_matches(tenant_id, location_id, bill_id, token):
    if not isinstance(token, str) or not token or len(token) > 256:
        return False
    return safe_compare.equal(bill_token(tenant_id, location_id, bill_id), token)


def resolve_guest(ctx, location_id, token, order_id, now):
    """The table bill a guest may see: (serviceDate, tableId) from the
    order's status token (``order`` given) or the staff pay link. o.NotFound
    otherwise (the same answer as a wrong token)."""
    if order_id is not None:
        item = o.load(location_id, order_id)
        if item.get("tenantId") != ctx.tenant_id or not o.token_matches(item, token) \
                or not item.get("payLater") or not item.get("serviceDate") or not item.get("tableId"):
            raise o.NotFound
        return item["serviceDate"], item["tableId"]
    return read_link_token(ctx.tenant_id, location_id, token, now)


# --- placing a pay-later order (payment-intent) ----------------------------------------------

def add_order(item, now):
    """The TransactWriteItems Update that puts a new unpaid order on its
    table's bill, refused (condition) when the bill is full."""
    total = int(item["totalAmount"])
    names = {"#e": ENTRY + item["orderId"], "#n": "openCount", "#t": "openTotal", "#v": "version",
             "#ttl": "ttl", "#rt": "recordType", "#tn": "tenantId", "#lo": "locationId", "#ti": "tableId",
             "#tl": "tableLabel", "#sd": "serviceDate", "#u": "updatedAt"}
    values = {":entry": {"date": item["date"], "number": int(item["number"]), "total": total, "at": o.iso(now)},
              ":one": 1, ":total": total, ":maxN": o.TABLE_MAX_OPEN_ORDERS,
              ":room": o.TABLE_MAX_OPEN_ORE - total, ":rt": "tableTab", ":tn": item["tenantId"],
              ":lo": item["locationId"], ":ti": item["tableId"], ":tl": item.get("tableLabel") or "",
              ":sd": item["serviceDate"], ":now": o.iso(now)}
    return _update(
        tab_key(item["locationId"], item["serviceDate"], item["tableId"]),
        "SET #e = :entry, #rt = :rt, #tn = :tn, #lo = :lo, #ti = :ti, #tl = :tl, #sd = :sd, #u = :now "
        "ADD #n :one, #t :total, #v :one REMOVE #ttl",
        "(attribute_not_exists(#n) OR #n < :maxN) AND (attribute_not_exists(#t) OR #t <= :room)",
        names, values)


def bill_full(item):
    """After a refused placement: is the table's bill the reason?"""
    if int(item["totalAmount"]) > o.TABLE_MAX_OPEN_ORE:
        return True
    tab = load_tab(item["locationId"], item["serviceDate"], item["tableId"]) or {}
    return int(tab.get("openCount") or 0) >= o.TABLE_MAX_OPEN_ORDERS or \
        int(tab.get("openTotal") or 0) + int(item["totalAmount"]) > o.TABLE_MAX_OPEN_ORE


# --- views -----------------------------------------------------------------------------------

def _bill_lines(order):
    return [{"name": o.line_label(line)[:160], "quantity": int(line.get("quantity") or 1),
             "total": int(line.get("total") or 0), "vatRate": int(line.get("vatRate") or 0)}
            for line in order.get("lines") or []]


def _seconds_since(value, now):
    try:
        return (now - o.parse_iso(value)).total_seconds()
    except (TypeError, ValueError, AttributeError):
        return LOCK_SECONDS


def guest_view(tab, location, now, orders=None):
    """What anyone at the table may see: no names, phones or e-mails."""
    tab = tab or {}
    open_ = entries(tab)
    if orders is None:
        orders = _load_orders(tab.get("locationId"), open_) if open_ else []
    lines = [line for order in orders for line in _bill_lines(order)]
    count = int(tab.get("openCount") or 0)
    paying = tab.get("paying")
    status = PAYING if paying else ("open" if count > 0 else "empty")
    view = {
        "tableTitle": title(tab) if tab.get("tableLabel") else None,
        "serviceDate": tab.get("serviceDate"),
        "status": status,
        "amount": int(tab.get("openTotal") or 0),
        "count": count,
        "currency": o.CURRENCY,
        "minChargeOre": MIN_CHARGE_ORE,
        "orders": [{"number": order.get("number"), "createdAt": order.get("createdAt"),
                    "total": int(order.get("totalAmount") or 0), "lines": _bill_lines(order)} for order in orders],
        "vat": o.vat_breakdown(lines) if lines else [],
        "location": {"name": (location or {}).get("name")},
    }
    if paying:
        view["paying"] = {"startedAt": paying.get("at"),
                          "retryAfter": max(0, int(LOCK_SECONDS - _seconds_since(paying.get("at"), now)))}
    return {k: v for k, v in view.items() if v is not None}


def bill_view(bill):
    return {k: v for k, v in {
        "billId": bill["billId"], "status": bill.get("status"), "amount": int(bill.get("amount") or 0),
        "count": len(bill.get("orders") or []), "paidAt": bill.get("paidAt"), "tableTitle": title(bill),
        "serviceDate": bill.get("serviceDate"), "currency": bill.get("currency", o.CURRENCY),
        "orders": [{k2: order.get(k2) for k2 in ("number", "createdAt", "total", "lines")}
                   for order in bill.get("orders") or []],
        "vat": bill.get("vat") or [],
    }.items() if v is not None}


def order_bill(item):
    """{amount, count, status} of the bill an unpaid pay-later order is on
    (the guest's status page), or None."""
    if not item.get("payLater") or item.get("paymentStatus") != o.PAY_UNPAID:
        return None
    tab = load_tab(item["locationId"], item.get("serviceDate"), item.get("tableId"))
    if not tab:
        return None
    count = int(tab.get("openCount") or 0)
    return {"amount": int(tab.get("openTotal") or 0), "count": count,
            "status": PAYING if tab.get("paying") else ("open" if count > 0 else "empty")}


# --- paying with the phone (payment-intent) --------------------------------------------------

def _clean_email(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 254 or not _EMAIL.fullmatch(value.strip()):
        raise ValueError("email must be a valid email address")
    return value.strip().lower()


def _whole_ore(value, field):
    if isinstance(value, bool) or not isinstance(value, (int,)) and not (
            hasattr(value, "to_integral_value") and value == value.to_integral_value()):
        raise ValueError(f"{field} must be a whole number of öre")
    value = int(value)
    if value < 0:
        raise ValueError(f"{field} must be a whole number of öre")
    return value


def _session_answer(ctx, location_id, bill, session, account):
    return {"billId": bill["billId"], "billToken": bill_token(ctx.tenant_id, location_id, bill["billId"]),
            "clientSecret": session["client_secret"], "stripeAccount": account,
            "amount": int(bill["amount"]), "count": len(bill.get("orders") or [])}


def pay(ctx, location_id, service_date, table_id, data, *, base, slug, now, ensure_domains=None):
    """Starts (or resumes) paying the table's whole open bill. Returns
    (status, body). Raises BillError / ValueError."""
    expected = _whole_ore(data.get("expectedAmount"), "expectedAmount")
    email = _clean_email(data.get("email"))
    resume_id, resume_token = data.get("resumeBillId"), data.get("resumeToken")
    account = o.stripe_account(ctx.tenant, ctx.location)
    if not account:
        raise BillError("ordering_no_payments")
    tab = load_tab(location_id, service_date, table_id)
    if not tab or (int(tab.get("openCount") or 0) <= 0 and not tab.get("paying")):
        raise BillError("bill_empty")
    paying = tab.get("paying")
    if paying:
        if isinstance(resume_id, str) and resume_id == paying.get("billId") and \
                bill_token_matches(ctx.tenant_id, location_id, resume_id, resume_token):
            resumed = _resume(ctx, location_id, resume_id, expected, account, now)
            if resumed:
                return 200, resumed
            tab = load_tab(location_id, service_date, table_id) or {}
            paying = tab.get("paying")
        if paying:
            age = _seconds_since(paying.get("at"), now)
            if age < LOCK_SECONDS:
                raise BillError("bill_being_paid", retryAfter=max(1, int(LOCK_SECONDS - age)))
            outcome = release(tab, now, "guest")
            if outcome == PAID:
                raise BillError("bill_paid")
            if outcome == "processing":
                raise BillError("bill_processing")
            if outcome == "locked":
                raise BillError("bill_being_paid", retryAfter=30)
            tab = load_tab(location_id, service_date, table_id) or {}
            if tab.get("paying"):
                raise BillError("bill_being_paid", retryAfter=LOCK_SECONDS)
    amount, count = int(tab.get("openTotal") or 0), int(tab.get("openCount") or 0)
    if count <= 0:
        raise BillError("bill_empty")
    open_ = entries(tab)
    orders = _load_orders(location_id, open_)
    if expected != amount:
        raise BillError("bill_changed", bill=guest_view(tab, ctx.location, now, orders))
    if amount < MIN_CHARGE_ORE:
        raise BillError("bill_below_minimum")
    if len(orders) != count or sum(int(i.get("totalAmount") or 0) for i in orders) != amount \
            or any(i.get("paymentStatus") != o.PAY_UNPAID for i in orders):
        logger.error(json.dumps({"bill": "tab_out_of_step", "locationId": location_id, "tableId": table_id}))
        raise BillError("bill_changed", bill=guest_view(tab, ctx.location, now, orders))
    if ensure_domains:
        ensure_domains(account)
    bill = _new_bill(ctx, location_id, tab, orders, amount, account, email, now)
    lock = {"billId": bill["billId"], "orderIds": [i["orderId"] for i in orders], "amount": amount,
            "at": o.iso(now), "expiresAt": bill["expiresAt"]}
    # The lock holds only if the table's bill is exactly what was read: the
    # version (every add, removal and lock change bumps it), not just count
    # and total - one order cancelled and another of the same price placed
    # meanwhile would otherwise charge the cancelled one and leave the new one.
    try:
        dynamo.client().transact_write_items(TransactItems=[
            _update(tab_key(location_id, service_date, table_id),
                    "SET #p = :lock, #u = :now ADD #v :one REMOVE #ttl",
                    "attribute_exists(PK) AND attribute_not_exists(#p) AND #t = :amount AND #n = :count "
                    "AND #v = :version",
                    {"#p": "paying", "#u": "updatedAt", "#v": "version", "#ttl": "ttl", "#t": "openTotal",
                     "#n": "openCount"},
                    {":lock": lock, ":now": o.iso(now), ":one": 1, ":amount": amount, ":count": count,
                     ":version": int(tab.get("version") or 0)}),
            {"Put": {"TableName": o.order_table(), "Item": _avs(bill),
                     "ConditionExpression": "attribute_not_exists(PK)"}},
        ])
    except ClientError as exc:
        if not _cancelled(exc):
            raise
        fresh = load_tab(location_id, service_date, table_id) or {}
        if fresh.get("paying"):
            raise BillError("bill_being_paid", retryAfter=LOCK_SECONDS) from None
        raise BillError("bill_changed", bill=guest_view(fresh, ctx.location, now)) from None
    token = bill_token(ctx.tenant_id, location_id, bill["billId"])
    from shared import site  # noqa: PLC0415 - only the pay path needs the site address
    return_url = site.page(base, slug, f"/notan/{location_id}") + ("&" if slug else "?") + \
        f"nota={bill['billId']}#{token}"
    try:
        session = create_checkout(bill, account, return_url, now)
    except (stripe.StripeError, stripe.StripeUnavailable) as exc:
        logger.error(json.dumps({"bill": "checkout_rejected", "billId": bill["billId"],
                                 "code": getattr(exc, "code", None) or type(exc).__name__}))
        settle_failed(bill, now, "system", "checkout_rejected")
        raise BillError("payment_unavailable", status=503) from None
    _table().update_item(
        Key=bill_key(location_id, bill["billId"]),
        UpdateExpression="SET checkoutSessionId = :id",
        ConditionExpression="attribute_exists(PK)",
        ExpressionAttributeValues={":id": session["id"]})
    bill["checkoutSessionId"] = session["id"]
    logger.info(json.dumps({"bill": "created", "billId": bill["billId"], "tenantId": ctx.tenant_id,
                            "amount": amount, "orders": count}))
    return 201, _session_answer(ctx, location_id, bill, session, account)


def _resume(ctx, location_id, bill_id, expected, account, now):
    """Same device back on the pay page: the session it already has, or
    None when it can't be resumed (raises bill_paid when it was paid)."""
    bill = load_bill(location_id, bill_id)
    if not bill or bill.get("status") != PAYING or not bill.get("checkoutSessionId") \
            or int(bill.get("amount") or 0) != expected:
        return None
    session = retrieve_session(bill)
    if session.get("payment_status") == "paid":
        settle_paid(bill, session, now, "guest")
        raise BillError("bill_paid")
    if session.get("status") == "open" and session.get("client_secret"):
        return _session_answer(ctx, location_id, bill, session, account)
    if session.get("status") == "expired":
        settle_failed(bill, now, "guest", "checkout_expired")
    return None


def _new_bill(ctx, location_id, tab, orders, amount, account, email, now):
    bill_id = o.new_id()
    snapshot = [{"orderId": i["orderId"], "date": i["date"], "number": i.get("number"),
                 "createdAt": i.get("createdAt"), "total": int(i.get("totalAmount") or 0),
                 "lines": _bill_lines(i)} for i in orders]
    lines = [line for order in snapshot for line in order["lines"]]
    bill = {
        **bill_key(location_id, bill_id),
        "recordType": "tableBill", "billId": bill_id, "tenantId": ctx.tenant_id, "locationId": location_id,
        "tableId": tab["tableId"], "tableLabel": tab.get("tableLabel") or "", "serviceDate": tab["serviceDate"],
        "timezone": (ctx.location or {}).get("timezone") or "Europe/Stockholm",
        "orders": snapshot, "amount": amount, "vat": o.vat_breakdown(lines), "currency": o.CURRENCY,
        "stripeAccountId": account, "status": PAYING, "createdAt": o.iso(now), "updatedAt": o.iso(now),
        "expiresAt": o.iso(now + timedelta(minutes=o.CHECKOUT_MINUTES + 1)),
        "history": [o.history_entry("created", "guest", now, amount=amount)], "version": 1,
        "ttl": _epoch(now + timedelta(days=BILL_TTL_DAYS)),
    }
    if email:
        bill["payerEmail"] = email
    return bill


def create_checkout(bill, account, return_url, now):
    """Checkout Session (Payment Element) for the whole bill: one line per
    order. Idempotency key bill-checkout-<billId>: one charge per bill."""
    metadata = {"kind": KIND, "billId": bill["billId"], "locationId": bill["locationId"],
                "tenantId": bill["tenantId"], "serviceDate": bill["serviceDate"], "tableId": bill["tableId"]}
    params = {
        "mode": "payment",
        "line_items": [{
            "price_data": {"currency": o.CURRENCY, "unit_amount": int(order["total"]),
                           "product_data": {"name": f"Beställning #{order.get('number') or ''}".strip()}},
            "quantity": 1,
        } for order in bill["orders"]],
        "client_reference_id": bill["billId"],
        "customer_email": bill.get("payerEmail"),
        "wallet_options": {"link": {"display": "never"}},
        "metadata": metadata,
        "payment_intent_data": {"metadata": metadata,
                                "description": f"Nota {bill.get('tableLabel') or ''} {bill['serviceDate']}"[:200]},
        "expires_at": int((now + timedelta(minutes=o.CHECKOUT_MINUTES + 1)).timestamp()),
        "ui_mode": "elements",
        "return_url": return_url,
    }
    return stripe.request("POST", "/v1/checkout/sessions", params, account=account,
                          idempotency_key=f"bill-checkout-{bill['billId']}")


def retrieve_session(bill):
    return stripe.request("GET", f"/v1/checkout/sessions/{bill['checkoutSessionId']}",
                          account=bill["stripeAccountId"])


def session_matches(session, bill, account=None):
    meta = (session or {}).get("metadata") or {}
    return (meta.get("kind") == KIND and meta.get("billId") == bill.get("billId")
            and meta.get("locationId") == bill.get("locationId")
            and (not bill.get("checkoutSessionId") or session.get("id") == bill["checkoutSessionId"])
            and session.get("amount_total") == int(bill.get("amount") or 0)
            and (session.get("currency") or "").lower() == o.CURRENCY
            and (account is None or account == bill.get("stripeAccountId")))


# --- settling --------------------------------------------------------------------------------

def _order_paid_update(location_id, order, bill_id, pi, actor, now):
    return _update(
        o.order_key(location_id, order["date"], order["orderId"]),
        "SET paymentStatus = :paid, paymentMethod = :online, paymentIntentId = :pi, paidAt = :now, "
        "billId = :bid, updatedAt = :now, history = list_append(if_not_exists(history, :empty), :entry), "
        "#v = if_not_exists(#v, :zero) + :one",
        "attribute_exists(PK) AND paymentStatus = :unpaid",
        {"#v": "version"},
        {":paid": o.PAY_PAID, ":online": "online", ":pi": pi, ":now": o.iso(now), ":bid": bill_id,
         ":empty": [], ":entry": [o.history_entry("paid", actor, now, billId=bill_id)], ":zero": 0, ":one": 1,
         ":unpaid": o.PAY_UNPAID})


def _remove_entries_update(location_id, tab, order_ids, totals, now, *, lock_bill=None, lock_free_for=None,
                           paid=False):
    """Update that takes ``order_ids`` off the table's bill. ``lock_bill``:
    the caller holds the payment lock (cleared here); ``lock_free_for``: no
    payment lock may cover these orders."""
    names = {"#n": "openCount", "#t": "openTotal", "#v": "version", "#u": "updatedAt"}
    values = {":negn": -len(order_ids), ":negt": -sum(totals), ":one": 1, ":now": o.iso(now)}
    removes, conditions = [], ["attribute_exists(PK)"]
    for i, order_id in enumerate(order_ids):
        names[f"#e{i}"] = ENTRY + order_id
        removes.append(f"#e{i}")
        conditions.append(f"attribute_exists(#e{i})")
    sets = ["#u = :now"]
    if paid:
        sets.append("lastPaidAt = :now")
    if lock_bill:
        names["#p"] = "paying"
        removes.append("#p")
        values[":bid"] = lock_bill
        conditions.append("#p.billId = :bid")
    if lock_free_for:
        names["#p"] = "paying"
        free = []
        for i, order_id in enumerate(lock_free_for):
            values[f":o{i}"] = order_id
            free.append(f"NOT contains(#p.orderIds, :o{i})")
        conditions.append(f"(attribute_not_exists(#p) OR ({' AND '.join(free)}))")
    expression = "SET " + ", ".join(sets) + " ADD #n :negn, #t :negt, #v :one"
    if removes:
        expression += " REMOVE " + ", ".join(removes)
    return _update(tab_key(location_id, tab["serviceDate"], tab["tableId"]), expression,
                   " AND ".join(conditions), names, values)


def _tidy_tab(location_id, service_date, table_id, now):
    """An empty, unlocked bill expires (TTL) - best effort."""
    try:
        _table().update_item(
            Key=tab_key(location_id, service_date, table_id),
            UpdateExpression="SET #ttl = :ttl",
            # A flag for the owner (mismatch) keeps the record until it is checked.
            ConditionExpression="attribute_exists(PK) AND #n <= :zero AND attribute_not_exists(paying) "
                                "AND attribute_not_exists(mismatch)",
            ExpressionAttributeNames={"#ttl": "ttl", "#n": "openCount"},
            ExpressionAttributeValues={":ttl": _epoch(now + timedelta(days=TAB_TTL_DAYS)), ":zero": 0})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


def _bill_paid_update(bill, session, now, actor, *, mismatch=None):
    sid = session.get("id") or bill.get("checkoutSessionId") or "-"
    sets = ["#s = :paid", "paidAt = :now", "paymentIntentId = :pi", "checkoutSessionId = :sid", "updatedAt = :now",
            "history = list_append(if_not_exists(history, :empty), :entry)", "#v = if_not_exists(#v, :zero) + :one"]
    values = {":paid": PAID, ":now": o.iso(now), ":pi": session.get("payment_intent") or "-", ":sid": sid,
              ":empty": [], ":entry": [o.history_entry("paid", actor, now)], ":zero": 0, ":one": 1,
              ":paying": PAYING}
    if bill.get("payerEmail"):
        sets.append("notice = :notice")
        values[":notice"] = {"id": o.new_id(), "type": NOTICE_BILL_PAID, "at": o.iso(now)}
    if mismatch:
        sets.append("mismatch = :mismatch")
        values[":mismatch"] = mismatch
    return _update(
        bill_key(bill["locationId"], bill["billId"]),
        "SET " + ", ".join(sets) + " REMOVE #ttl",
        "#s = :paying AND (attribute_not_exists(checkoutSessionId) OR checkoutSessionId = :sid)",
        {"#s": "status", "#v": "version", "#ttl": "ttl"}, values)


def settle_paid(bill, session, now, actor):
    """paying -> paid: the bill, the table's bill and every order at once.
    Returns the updated bill, or None when it was already settled."""
    if bill.get("status") != PAYING:
        return None
    location_id = bill["locationId"]
    pi = session.get("payment_intent") or "-"
    orders = bill.get("orders") or []
    tab = {"serviceDate": bill["serviceDate"], "tableId": bill["tableId"]}
    items = [_bill_paid_update(bill, session, now, actor),
             _remove_entries_update(location_id, tab, [x["orderId"] for x in orders],
                                    [int(x["total"]) for x in orders], now, lock_bill=bill["billId"], paid=True)]
    items += [_order_paid_update(location_id, x, bill["billId"], pi, actor, now) for x in orders]
    try:
        dynamo.client().transact_write_items(TransactItems=items)
    except ClientError as exc:
        if not _cancelled(exc):
            raise
        fresh = load_bill(location_id, bill["billId"])
        if not fresh or fresh.get("status") != PAYING:
            return None
        return _settle_mismatch(fresh, session, now, actor)
    _tidy_tab(location_id, bill["serviceDate"], bill["tableId"], now)
    logger.info(json.dumps({"bill": "paid", "billId": bill["billId"], "amount": int(bill.get("amount") or 0),
                            "orders": len(orders), "by": actor}))
    return load_bill(location_id, bill["billId"])


def _settle_mismatch(bill, session, now, actor):
    """The money is in, but an order on the bill is no longer unpaid (which
    the lock should make impossible). The bill is paid; the orders that
    still are unpaid are settled one by one; the rest is flagged for the
    owner (refund the difference on this payment in the Stripe Dashboard)."""
    location_id = bill["locationId"]
    pi = session.get("payment_intent") or "-"
    settled, missed = [], []
    for order in bill.get("orders") or []:
        try:
            dynamo.client().transact_write_items(
                TransactItems=[_order_paid_update(location_id, order, bill["billId"], pi, actor, now)])
            settled.append(order)
        except ClientError as exc:
            if not _cancelled(exc):
                raise
            missed.append(order["orderId"])
    try:
        dynamo.client().transact_write_items(TransactItems=[_bill_paid_update(bill, session, now, actor,
                                                                              mismatch=missed or None)])
    except ClientError as exc:
        if not _cancelled(exc):
            raise
        return None
    tab = load_tab(location_id, bill["serviceDate"], bill["tableId"]) or {}
    on_tab = [x for x in settled if ENTRY + x["orderId"] in tab]
    if tab:
        names = {"#p": "paying", "#u": "updatedAt"}
        values = {":bid": bill["billId"], ":now": o.iso(now)}
        try:
            update = _remove_entries_update(location_id, tab, [x["orderId"] for x in on_tab],
                                            [int(x["total"]) for x in on_tab], now, paid=True)["Update"]
            _table().update_item(Key={k: v["S"] for k, v in update["Key"].items()},
                                 UpdateExpression=update["UpdateExpression"],
                                 ConditionExpression=update["ConditionExpression"],
                                 ExpressionAttributeNames=update["ExpressionAttributeNames"],
                                 ExpressionAttributeValues={k: _plain(v) for k, v in
                                                            update["ExpressionAttributeValues"].items()})
            if missed:
                _table().update_item(
                    Key=tab_key(location_id, bill["serviceDate"], bill["tableId"]),
                    UpdateExpression="SET mismatch = :m",
                    ExpressionAttributeValues={":m": {"billId": bill["billId"], "orderIds": missed,
                                                      "at": o.iso(now)}})
            _table().update_item(
                Key=tab_key(location_id, bill["serviceDate"], bill["tableId"]),
                UpdateExpression="SET #u = :now REMOVE #p", ConditionExpression="#p.billId = :bid",
                ExpressionAttributeNames=names, ExpressionAttributeValues=values)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
    logger.error(json.dumps({"bill": "mismatch", "billId": bill["billId"], "locationId": location_id,
                             "orders": missed}))
    return load_bill(location_id, bill["billId"])


def _plain(av):
    from boto3.dynamodb.types import TypeDeserializer  # noqa: PLC0415
    return TypeDeserializer().deserialize(av)


def settle_failed(bill, now, actor, reason):
    """paying -> expired | failed and the table's lock released. Orders untouched."""
    if bill.get("status") != PAYING:
        return None
    status = EXPIRED if reason in ("checkout_expired", "released") else FAILED
    try:
        result = _table().update_item(
            Key=bill_key(bill["locationId"], bill["billId"]),
            UpdateExpression=("SET #s = :st, failureReason = :reason, updatedAt = :now, "
                              "history = list_append(if_not_exists(history, :empty), :entry), "
                              "#v = if_not_exists(#v, :zero) + :one"),
            ConditionExpression="#s = :paying",
            ExpressionAttributeNames={"#s": "status", "#v": "version"},
            ExpressionAttributeValues={":st": status, ":reason": reason, ":now": o.iso(now), ":empty": [],
                                       ":entry": [o.history_entry(status, actor, now, reason=reason)],
                                       ":zero": 0, ":one": 1, ":paying": PAYING},
            ReturnValues="ALL_NEW")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise
    _clear_lock(bill, now)
    return result["Attributes"]


def _clear_lock(bill, now):
    try:
        _table().update_item(
            Key=tab_key(bill["locationId"], bill["serviceDate"], bill["tableId"]),
            UpdateExpression="SET #u = :now ADD #v :one REMOVE #p",
            ConditionExpression="#p.billId = :bid",
            ExpressionAttributeNames={"#p": "paying", "#u": "updatedAt", "#v": "version"},
            ExpressionAttributeValues={":bid": bill["billId"], ":now": o.iso(now), ":one": 1})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        return
    _tidy_tab(bill["locationId"], bill["serviceDate"], bill["tableId"], now)


def release(tab, now, actor):
    """Frees a payment lock so someone else can act: the Checkout Session is
    expired at Stripe first. Returns "released", "paid" (it was paid: now
    settled), "processing" (complete, an async method still deciding) or
    "locked" (too fresh to tell)."""
    paying = (tab or {}).get("paying") or {}
    location_id = tab.get("locationId")
    bill = load_bill(location_id, paying.get("billId")) if paying.get("billId") else None
    if not bill:
        _clear_lock({"locationId": location_id, "serviceDate": tab["serviceDate"], "tableId": tab["tableId"],
                     "billId": paying.get("billId")}, now)
        return "released"
    if bill.get("status") == PAID:
        return PAID
    if bill.get("status") != PAYING:
        _clear_lock(bill, now)
        return "released"
    if not bill.get("checkoutSessionId"):
        if _seconds_since(bill.get("createdAt"), now) >= LOCK_SECONDS:
            settle_failed(bill, now, actor, "released")
            return "released"
        return "locked"
    try:
        session = stripe.request("POST", f"/v1/checkout/sessions/{bill['checkoutSessionId']}/expire",
                                 account=bill["stripeAccountId"])
    except stripe.StripeError:
        session = retrieve_session(bill)  # already completed or expired
    if session.get("payment_status") == "paid":
        settle_paid(bill, session, now, actor)
        return PAID
    if session.get("status") == "expired":
        settle_failed(bill, now, actor, "released")
        return "released"
    if session.get("status") == "complete":
        return "processing"
    return "locked"


def sync(bill, now, actor):
    """The guest's pay page asks while the bill is paying: settle it if
    Stripe already knows. Returns the current bill."""
    if bill.get("status") != PAYING or not bill.get("checkoutSessionId"):
        return bill
    session = retrieve_session(bill)
    if not session_matches(session, bill):
        return bill
    if session.get("payment_status") == "paid":
        return settle_paid(bill, session, now, actor) or load_bill(bill["locationId"], bill["billId"])
    if session.get("status") == "expired":
        return settle_failed(bill, now, actor, "checkout_expired") or load_bill(bill["locationId"], bill["billId"])
    return bill


# --- webhook ---------------------------------------------------------------------------------

def is_bill_event(meta):
    meta = meta or {}
    return meta.get("kind") == KIND or (bool(meta.get("billId")) and not meta.get("orderId"))


def _bill_for_event(meta, account):
    location_id, bill_id = (meta or {}).get("locationId"), (meta or {}).get("billId")
    if not isinstance(location_id, str) or not location_id or "#" in location_id or not bill_id:
        return None
    bill = load_bill(location_id, bill_id)
    if not bill:
        return None
    if bill.get("stripeAccountId") != account:
        logger.warning(json.dumps({"ignored": "account_mismatch", "billId": bill_id}))
        return None
    return bill


def on_session_event(kind, session, account, now):
    bill = _bill_for_event(session.get("metadata"), account)
    if not bill:
        return "ignored"
    if not session_matches(session, bill, account):
        logger.warning(json.dumps({"ignored": "session_mismatch", "billId": bill["billId"]}))
        return "ignored:mismatch"
    if kind in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if session.get("payment_status") != "paid":
            return "waiting"
        return "bill_paid" if settle_paid(bill, session, now, "stripe") else "already_settled"
    reason = "checkout_expired" if kind == "checkout.session.expired" else "payment_failed"
    return "bill_failed" if settle_failed(bill, now, "stripe", reason) else "already_settled"


def on_charge_refunded(charge, account, now):
    """charge.refunded on a bill's payment: a backstop. Order refunds made in
    Booqy are recorded when they are made (or by the pending-refund replay);
    an owner refund whose answer was lost is completed here when exactly
    one order of the bill waits for one; anything else (a Stripe Dashboard
    refund) is recorded on the bill, not split across its orders."""
    bill = _bill_for_event(charge.get("metadata"), account)
    if not bill or bill.get("paymentIntentId") != charge.get("payment_intent"):
        return "ignored"
    stripe_total = int(charge.get("amount_refunded") or 0)
    items = []
    for x in bill.get("orders") or []:
        item = _table().get_item(Key=o.order_key(bill["locationId"], x["date"], x["orderId"]),
                                 ConsistentRead=True).get("Item")
        if item and item.get("billId") == bill["billId"]:
            items.append(item)
    recorded = sum(int((i.get("refund") or {}).get("amount") or 0) for i in items)
    if stripe_total <= recorded + int((bill.get("externalRefund") or {}).get("amount") or 0):
        return "already_recorded"
    diff = stripe_total - recorded
    waiting = [i for i in items if (i.get("refund") or {}).get("pending")]
    result = "external"
    if len(waiting) == 1:
        item = waiting[0]
        pending = int(item["refund"]["pending"].get("amount") or 0)
        if 0 < pending <= diff:
            own = int((item.get("refund") or {}).get("amount") or 0)
            order_payments.record_stripe_refund(item, own + pending, now)
            diff -= pending
            result = "recorded"
    if diff > 0:
        _table().update_item(
            Key=bill_key(bill["locationId"], bill["billId"]),
            UpdateExpression="SET externalRefund = :r, updatedAt = :now",
            ExpressionAttributeValues={":r": {"amount": diff, "at": o.iso(now)}, ":now": o.iso(now)})
        logger.warning(json.dumps({"bill": "external_refund", "billId": bill["billId"], "amount": diff}))
    return result


# --- staff (manage-order) --------------------------------------------------------------------

def _audit(tenant_id, location_id, order_id, action, by, now, amount, **extra):
    stamp = o.iso(now)
    key = {"PK": o.location_pk(location_id),
           "SK": f"{AUDIT_PREFIX}{order_id}#{stamp}#{secrets.token_hex(4)}"}
    fields = {"recordType": "orderAudit", "tenantId": tenant_id, "locationId": location_id, "orderId": order_id,
              "action": action, "by": by, "at": stamp, "amount": int(amount),
              **{k: v for k, v in extra.items() if v is not None}}
    names = {f"#f{i}": k for i, k in enumerate(fields)}
    values = {f":f{i}": v for i, v in enumerate(fields.values())}
    return _update(key, "SET " + ", ".join(f"#f{i} = :f{i}" for i in range(len(fields))),
                   "attribute_not_exists(PK)", names, values)


def write_audit(tenant_id, location_id, order_id, action, by, now, amount, **extra):
    dynamo.client().transact_write_items(
        TransactItems=[_audit(tenant_id, location_id, order_id, action, by, now, amount, **extra)])


def list_open_tabs(location_id):
    request = {"KeyConditionExpression": Key("PK").eq(o.location_pk(location_id)) & Key("SK").begins_with(TAB_PREFIX),
               "FilterExpression": Attr("openCount").gt(0) | Attr("paying").exists() | Attr("mismatch").exists(),
               "ConsistentRead": True}
    out = []
    while True:
        page = _table().query(**request)
        out.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return out
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def staff_tabs(location, tenant_row, location_id, now, *, staff_view):
    """{serviceDate, tabs} for the restaurant's screen: every table with an
    open bill (or a payment lock / a flag for the owner), oldest first."""
    today = o.service_date(location, now)
    closed = o.availability(location, tenant_row, now).get("reason") == "closed"
    tabs = list_open_tabs(location_id)
    by_date = {}
    for tab in tabs:
        for entry in entries(tab).values():
            by_date.setdefault(entry.get("date"), None)
    orders = {}
    for day in by_date:
        if isinstance(day, str) and _DATE.fullmatch(day):
            for item in o.list_day(location_id, day):
                orders[item["orderId"]] = item
    out = [tab_view(tab, orders, today, closed, staff_view) for tab in tabs]
    out.sort(key=lambda t: (t.get("oldestAt") or "~", t["tableId"]))
    return {"serviceDate": today, "tabs": out}


def tab_view(tab, orders_by_id, today, closed, staff_view):
    open_ = entries(tab)
    ats = [e.get("at") for e in open_.values() if e.get("at")]
    listed = [orders_by_id[oid] for oid in open_ if oid in orders_by_id]
    listed.sort(key=lambda i: (i.get("createdAt") or "", int(i.get("number") or 0), i["orderId"]))
    view = {
        "tableId": tab["tableId"], "tableLabel": tab.get("tableLabel"), "title": title(tab),
        "serviceDate": tab["serviceDate"], "amount": int(tab.get("openTotal") or 0),
        "count": int(tab.get("openCount") or 0), "oldestAt": min(ats) if ats else None,
        "orders": [staff_view(i) for i in listed],
        "stale": tab["serviceDate"] < today or closed,
    }
    if tab.get("paying"):
        view["paying"] = {"billId": tab["paying"].get("billId"), "amount": int(tab["paying"].get("amount") or 0),
                          "startedAt": tab["paying"].get("at")}
    if tab.get("mismatch"):
        view["mismatch"] = tab["mismatch"]
    return {k: v for k, v in view.items() if v is not None}


def pay_link(ctx, location_id, service_date, table_id, now, *, base, slug):
    from shared import site  # noqa: PLC0415
    tab = load_tab(location_id, service_date, table_id)
    if not tab or int(tab.get("openCount") or 0) <= 0:
        raise BillError("bill_empty")
    if not base:
        raise BillError("ordering_no_site")
    token, expires = link_token(ctx.tenant_id, location_id, service_date, table_id, now)
    url = site.page(base, slug, f"/notan/{location_id}") + "#" + token
    for order_id in entries(tab):
        try:
            write_audit(ctx.tenant_id, location_id, order_id, "pay_link", ctx.sub, now,
                        int(entries(tab)[order_id].get("total") or 0))
        except ClientError:
            logger.warning(json.dumps({"audit": "pay_link_failed", "orderId": order_id}))
    return {"url": url, "expiresAt": o.iso(expires), "amount": int(tab.get("openTotal") or 0),
            "count": int(tab.get("openCount") or 0)}


def _clean_text(value, field, *, required):
    if value in (None, "") and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    text = " ".join("".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in value).split())
    if required and not text:
        raise ValueError(f"{field} is required")
    if len(text) > _NOTE_MAX:
        raise ValueError(f"{field} must be at most {_NOTE_MAX} characters")
    return text or None


def close_without_booqy(ctx, location_id, service_date, table_id, kind, data, now):
    """Staff "Markera som betald" (kind paid_outside: method, note?) or the
    owner's "Skriv av" (kind written_off: reason). The orders leave the bill
    in one transaction with an audit row each. Returns the updated orders."""
    if kind == o.PAY_OUTSIDE:
        method = data.get("method")
        if method not in o.OUTSIDE_METHODS:
            raise ValueError("method must be card_terminal, cash, swish or other")
        record = {"method": method, "by": ctx.sub, "at": o.iso(now)}
        note = _clean_text(data.get("note"), "note", required=False)
        if note:
            record["note"] = note
        field, audit_action, extra = "paidOutside", "paid_outside", {"method": method}
    else:
        reason = _clean_text(data.get("reason"), "reason", required=True)
        record = {"reason": reason, "by": ctx.sub, "at": o.iso(now)}
        field, audit_action, extra = "writtenOff", "written_off", {"reason": reason}
    expected = _whole_ore(data.get("expectedAmount"), "expectedAmount")
    wanted = data.get("orderIds")
    if wanted is not None and (not isinstance(wanted, list) or not wanted or len(wanted) > o.TABLE_MAX_OPEN_ORDERS
                               or not all(o.valid_order_id(x) for x in wanted)):
        raise ValueError("orderIds must be a list of order ids")
    tab = load_tab(location_id, service_date, table_id)
    if not tab or tab.get("tenantId") != ctx.tenant_id:
        raise o.NotFound
    for attempt in range(2):
        open_ = entries(tab)
        ids = list(dict.fromkeys(wanted)) if wanted is not None else list(open_)
        if not ids:
            raise BillError("bill_empty")
        if any(x not in open_ for x in ids):
            raise BillError("bill_changed")
        totals = [int(open_[x].get("total") or 0) for x in ids]
        if sum(totals) != expected:
            raise BillError("bill_changed")
        paying = tab.get("paying")
        if paying and set(ids) & set(paying.get("orderIds") or []):
            if attempt:
                raise BillError("bill_being_paid")
            outcome = release(tab, now, ctx.sub)
            if outcome == PAID:
                raise BillError("bill_paid")
            if outcome == "processing":
                raise BillError("bill_processing")
            if outcome == "locked":
                raise BillError("bill_being_paid")
            tab = load_tab(location_id, service_date, table_id) or tab
            continue
        break
    stamp = o.iso(now)
    items = [_remove_entries_update(location_id, tab, ids, totals, now, lock_free_for=ids)]
    for order_id in ids:
        entry = open_[order_id]
        sets = [f"paymentStatus = :ps", f"{field} = :rec", "updatedAt = :now", "updatedBy = :by",
                "history = list_append(if_not_exists(history, :empty), :entry)",
                "#v = if_not_exists(#v, :zero) + :one"]
        if kind == o.PAY_OUTSIDE:
            sets.append("paidAt = :now")
        items.append(_update(
            o.order_key(location_id, entry["date"], order_id), "SET " + ", ".join(sets),
            "attribute_exists(PK) AND paymentStatus = :unpaid", {"#v": "version"},
            {":ps": kind, ":rec": record, ":now": stamp, ":by": ctx.sub, ":empty": [],
             ":entry": [o.history_entry(audit_action, ctx.sub, now, **extra)], ":zero": 0, ":one": 1,
             ":unpaid": o.PAY_UNPAID}))
        items.append(_audit(ctx.tenant_id, location_id, order_id, audit_action, ctx.sub, now,
                            int(entry.get("total") or 0), **extra))
    try:
        dynamo.client().transact_write_items(TransactItems=items)
    except ClientError as exc:
        if not _cancelled(exc):
            raise
        fresh = load_tab(location_id, service_date, table_id) or {}
        if fresh.get("paying") and set(ids) & set(fresh["paying"].get("orderIds") or []):
            raise BillError("bill_being_paid") from None
        raise BillError("bill_changed") from None
    _tidy_tab(location_id, service_date, table_id, now)
    logger.info(json.dumps({"bill": audit_action, "locationId": location_id, "tableId": table_id,
                            "orders": len(ids), "amount": expected}))
    return [_table().get_item(Key=o.order_key(location_id, open_[x]["date"], x), ConsistentRead=True)["Item"]
            for x in ids]


def cancel_unpaid(item, ctx, now, expected_status):
    """Staff cancel an unpaid pay-later order: nothing to refund. It leaves
    its table's bill (after the payment lock is released if it covers it);
    status cancelled, paymentStatus void. Returns the updated order."""
    location_id, order_id = item["locationId"], item["orderId"]
    tab = load_tab(location_id, item.get("serviceDate"), item.get("tableId")) if item.get("serviceDate") else None
    if tab and tab.get("paying") and order_id in (tab["paying"].get("orderIds") or []):
        outcome = release(tab, now, ctx.sub)
        if outcome == PAID:
            raise BillError("bill_paid")
        if outcome == "processing":
            raise BillError("bill_processing")
        if outcome == "locked":
            raise BillError("bill_being_paid")
        tab = load_tab(location_id, item["serviceDate"], item["tableId"])
    total = int(item.get("totalAmount") or 0)
    sets = ["#s = :cancelled", "paymentStatus = :void", "cancelledAt = :now", "updatedAt = :now", "updatedBy = :by",
            "history = list_append(if_not_exists(history, :empty), :entry)", "#v = if_not_exists(#v, :zero) + :one"]
    values = {":cancelled": o.CANCELLED, ":void": o.PAY_VOID, ":now": o.iso(now), ":by": ctx.sub, ":empty": [],
              ":entry": [o.history_entry(o.CANCELLED, ctx.sub, now, refunded=0)], ":zero": 0, ":one": 1,
              ":expected": expected_status, ":unpaid": o.PAY_UNPAID}
    if item.get("customerEmail"):
        sets.append("notice = :notice")
        values[":notice"] = o.notice(o.NOTICE_CANCELLED, now)
    items = [_update(o.order_key(location_id, item["date"], order_id), "SET " + ", ".join(sets),
                     "attribute_exists(PK) AND #s = :expected AND paymentStatus = :unpaid",
                     {"#s": "status", "#v": "version"}, values),
             _audit(ctx.tenant_id, location_id, order_id, "cancelled_unpaid", ctx.sub, now, total)]
    if tab and (ENTRY + order_id) in tab:
        items.insert(0, _remove_entries_update(location_id, tab, [order_id],
                                               [int(entries(tab)[order_id].get("total") or 0)], now,
                                               lock_free_for=[order_id]))
    try:
        dynamo.client().transact_write_items(TransactItems=items)
    except ClientError as exc:
        if not _cancelled(exc):
            raise
        raise o.InvalidState("changed_retry") from None
    if tab:
        _tidy_tab(location_id, item["serviceDate"], item["tableId"], now)
    logger.info(json.dumps({"order": "cancelled_unpaid", "orderId": order_id}))
    return o.load(location_id, order_id)


def clear_mismatch(ctx, location_id, service_date, table_id, now=None):
    """Owner: the flagged bill payment has been checked (and refunded in Stripe).
    An empty bill then expires like any other."""
    try:
        _table().update_item(Key=tab_key(location_id, service_date, table_id), UpdateExpression="REMOVE mismatch",
                             ConditionExpression="attribute_exists(PK) AND tenantId = :t",
                             ExpressionAttributeValues={":t": ctx.tenant_id})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise o.NotFound from None
        raise
    _tidy_tab(location_id, service_date, table_id, now or o.now_utc())

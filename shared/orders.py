"""Food orders: the location's ordering policy, pricing from the menu, VAT,
signed table QR codes and status links, the order record and its views.

Two ways to order:
    pickup  the guest orders from anywhere (the restaurant's site) and
            collects at the counter when the "ready" SMS arrives
    table   the guest scans the QR code on a table; staff bring the food
            to that table

Order record (order table, one partition per location):
    PK LOCATION#<locationId>   SK ORDER#<date>#<orderId>       the order
    PK LOCATION#<locationId>   SK OID#<orderId>                 pointer -> date
    PK LOCATION#<locationId>   SK COUNTER#<date>                daily order number

Lifecycle:
    status         pending_payment -> confirmed -> preparing -> ready -> completed
                   pending_payment -> failed       (checkout expired / payment failed)
                   confirmed|preparing|ready -> cancelled   (staff; refunded)
    paymentStatus  processing -> paid -> refunded | processing -> failed

Prices are always read from the menu here - what the browser sent is never
trusted. Amounts are öre (Stripe's minor unit for SEK).

VAT (Sweden): alcohol 25 %; other food and drink 12 % when served at the
table (restaurant service) and 6 % for takeaway between 2026-04-01 and
2027-12-31 (temporary food VAT cut), 12 % otherwise. Rates are per line, so
a receipt shows the split.

Location policy (location item, ``ordering``, edited by the owner):
    enabled          bool
    pickup           bool  online orders for pickup
    table            bool  QR orders at the table
    prepMinutes      5-180  estimate shown to the guest
    lastOrderMinutes 0-120  stop taking orders this long before closing
Staff can pause ordering for a while (``orderingPausedUntil`` on the
location) when the kitchen is full.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

from shared import booking_rules, dynamo, manage_link, menu

_deserializer = TypeDeserializer()

CURRENCY = "sek"
MAX_QUANTITY = 20
MAX_LINES = 30
MAX_ORDER_ORE = 5_000_000  # 50 000 kr - a typo guard, not a business rule
CHECKOUT_MINUTES = 30
PENDING_TTL_MINUTES = 2 * CHECKOUT_MINUTES
MAX_PAUSE_MINUTES = 240

PICKUP = "pickup"
TABLE = "table"
MODES = (PICKUP, TABLE)

PENDING_PAYMENT = "pending_payment"
CONFIRMED = "confirmed"
PREPARING = "preparing"
READY = "ready"
COMPLETED = "completed"
CANCELLED = "cancelled"
FAILED = "failed"
OPEN_STATUSES = (CONFIRMED, PREPARING, READY)
# Staff moves: from -> allowed targets
TRANSITIONS = {
    CONFIRMED: (PREPARING, READY, COMPLETED, CANCELLED),
    PREPARING: (READY, COMPLETED, CANCELLED),
    READY: (COMPLETED, CANCELLED),
}

PAY_PROCESSING = "processing"
PAY_PAID = "paid"
PAY_FAILED = "failed"
PAY_REFUNDED = "refunded"

NOTICE_CONFIRMED = "order_confirmed"   # paid: receipt email (no SMS - the kitchen hasn't seen it yet)
NOTICE_STARTED = "order_started"       # staff pressed "Påbörja": the confirmation SMS (email if no SMS possible)
NOTICE_READY = "order_ready"           # SMS (email if no SMS possible) - pickup only
NOTICE_CANCELLED = "order_cancelled"   # SMS + email, refund
NOTICE_TYPES = (NOTICE_CONFIRMED, NOTICE_STARTED, NOTICE_READY, NOTICE_CANCELLED)

VAT_ALCOHOL = 25
VAT_RESTAURANT = 12
VAT_FOOD_REDUCED = 6
_REDUCED_FROM = date(2026, 4, 1)
_REDUCED_UNTIL = date(2027, 12, 31)

_ID = re.compile(r"[0-9a-f]{32}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_MENU_ID_MAX = 128
_TABLE_ID_MAX = 128
_TABLE_LABEL_MAX = 40
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class NotFound(Exception):
    pass


class InvalidState(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Closed(Exception):
    """Ordering is not possible right now (409 with the reason)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


# --- small helpers ---------------------------------------------------------------------

def order_table():
    return os.environ["ORDER_TABLE_NAME"]


def now_utc():
    return datetime.now(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def new_id():
    return uuid.uuid4().hex


def valid_order_id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def location_pk(location_id):
    return f"LOCATION#{location_id}"


def order_key(location_id, date_str, order_id):
    return {"PK": location_pk(location_id), "SK": f"ORDER#{date_str}#{order_id}"}


def pointer_key(location_id, order_id):
    return {"PK": location_pk(location_id), "SK": f"OID#{order_id}"}


def counter_key(location_id, date_str):
    return {"PK": location_pk(location_id), "SK": f"COUNTER#{date_str}"}


def notice(kind, at):
    if kind not in NOTICE_TYPES:
        raise ValueError(f"unknown notice type {kind}")
    return {"id": new_id(), "type": kind, "at": iso(at)}


def history_entry(action, actor, at, **extra):
    entry = {"action": action, "by": actor, "at": iso(at)}
    entry.update({k: v for k, v in extra.items() if v is not None})
    return entry


def _whole(value, low, high, field):
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a whole number")
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise ValueError(f"{field} must be a whole number")
        value = int(value)
    if not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{field} must be a whole number between {low} and {high}")
    return value


# --- policy --------------------------------------------------------------------------

def validate_policy(raw):
    """Owner input -> stored policy (raises ValueError). None removes it."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("ordering must be an object")
    allowed = {"enabled", "pickup", "table", "prepMinutes", "lastOrderMinutes"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"unsupported ordering fields: {', '.join(unknown)}")
    policy = {}
    for flag, default in (("enabled", False), ("pickup", True), ("table", True)):
        value = raw.get(flag, default)
        if not isinstance(value, bool):
            raise ValueError(f"ordering.{flag} must be true or false")
        policy[flag] = value
    policy["prepMinutes"] = _whole(raw.get("prepMinutes", 20), 5, 180, "ordering.prepMinutes")
    policy["lastOrderMinutes"] = _whole(raw.get("lastOrderMinutes", 15), 0, 120,
                                        "ordering.lastOrderMinutes")
    if policy["enabled"] and not (policy["pickup"] or policy["table"]):
        raise ValueError("enabled ordering needs pickup or table orders")
    return policy


def policy_of(location):
    """The location's policy, or None when off / missing / unreadable."""
    try:
        policy = validate_policy((location or {}).get("ordering"))
    except ValueError:
        return None
    return policy if policy and policy["enabled"] else None


def stripe_account(tenant_row, location=None):
    """Connected account the order is paid to, or None (ordering impossible):
    the location's own account (a separate company in the group) or the
    tenant's when it can take charges."""
    own = (location or {}).get("stripeAccountId")
    if isinstance(own, str) and own.startswith("acct_"):
        return own
    state = (tenant_row or {}).get("stripe") or {}
    account = state.get("accountId")
    if isinstance(account, str) and account.startswith("acct_") and state.get("chargesEnabled"):
        return account
    return None


def _timezone(location):
    try:
        return ZoneInfo((location or {}).get("timezone") or "Europe/Stockholm")
    except Exception:  # noqa: BLE001 - a broken stored zone falls back to Sweden
        return ZoneInfo("Europe/Stockholm")


def local_now(location, now):
    return now.astimezone(_timezone(location))


def paused_until(location, now):
    raw = (location or {}).get("orderingPausedUntil")
    if not isinstance(raw, str):
        return None
    try:
        until = parse_iso(raw)
    except ValueError:
        return None
    return until if until > now else None


def _intervals_today(location, local):
    hours = (location or {}).get("businessHours")
    if not isinstance(hours, dict):
        return []
    try:
        special_days = booking_rules.special_days_by_date(
            booking_rules.validate_special_days((location or {}).get("specialDays")))
    except ValueError:
        special_days = {}
    special = special_days.get(local.date().isoformat())
    weekday = _WEEKDAYS[local.weekday()]
    try:
        return list(booking_rules.day_intervals(hours, weekday, special) or [])
    except (KeyError, TypeError):
        return []


def availability(location, tenant_row, now):
    """What the guest site shows: {open, reason, pickup, table, prepMinutes,
    closesAt, pausedUntil}. ``reason`` when closed: disabled | no_payments
    | paused | closed."""
    policy = policy_of(location)
    if not policy:
        return {"open": False, "reason": "disabled", "pickup": False, "table": False}
    base = {"pickup": policy["pickup"], "table": policy["table"],
            "prepMinutes": policy["prepMinutes"]}
    if not stripe_account(tenant_row, location):
        return {**base, "open": False, "reason": "no_payments"}
    paused = paused_until(location, now)
    if paused:
        return {**base, "open": False, "reason": "paused", "pausedUntil": iso(paused)}
    local = local_now(location, now)
    minute = local.hour * 60 + local.minute
    for interval in _intervals_today(location, local):
        try:
            opens = _minutes(interval["opensAt"])
            closes = _minutes(interval["closesAt"])
        except (KeyError, TypeError, ValueError):
            continue
        last = closes - policy["lastOrderMinutes"]
        if opens <= minute < last:
            return {**base, "open": True, "closesAt": interval["closesAt"],
                    "lastOrderAt": f"{last // 60:02d}:{last % 60:02d}"}
    return {**base, "open": False, "reason": "closed"}


def _minutes(value):
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def require_open(location, tenant_row, mode, now):
    """Raises Closed unless an order in ``mode`` can be taken now; returns
    the policy."""
    state = availability(location, tenant_row, now)
    if not state["open"]:
        raise Closed(f"ordering_{state['reason']}")
    if not state.get(mode):
        raise Closed(f"ordering_{mode}_off")
    return policy_of(location)


# --- VAT -----------------------------------------------------------------------------

def vat_rate(alcohol, mode, on_date):
    if alcohol:
        return VAT_ALCOHOL
    if mode == TABLE:
        return VAT_RESTAURANT
    return VAT_FOOD_REDUCED if _REDUCED_FROM <= on_date <= _REDUCED_UNTIL else VAT_RESTAURANT


def vat_breakdown(lines):
    """[{rate, gross, net, vat}] per rate, öre. VAT is computed on the gross
    (prices include VAT): vat = gross * rate / (100 + rate), rounded."""
    by_rate = {}
    for line in lines:
        by_rate[line["vatRate"]] = by_rate.get(line["vatRate"], 0) + line["total"]
    out = []
    for rate in sorted(by_rate):
        gross = by_rate[rate]
        vat = int((Decimal(gross) * rate / (100 + rate)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        out.append({"rate": rate, "gross": gross, "net": gross - vat, "vat": vat})
    return out


# --- request validation --------------------------------------------------------------------

def validate_lines(raw):
    """[{menuItemId, quantity}] -> [(menuItemId, quantity)] with each item
    once (repeats are summed, and the sum must still be 1-20)."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("items must be a non-empty list")
    if len(raw) > MAX_LINES:
        raise ValueError(f"items may have at most {MAX_LINES} lines")
    quantities = {}
    order = []
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) - {"menuItemId", "quantity"}:
            raise ValueError("each item needs menuItemId and quantity")
        item_id = entry.get("menuItemId")
        if not isinstance(item_id, str) or not item_id.strip() or len(item_id) > _MENU_ID_MAX \
                or "#" in item_id:
            raise ValueError("menuItemId is invalid")
        quantity = _whole(entry.get("quantity"), 1, MAX_QUANTITY, "quantity")
        if item_id not in quantities:
            order.append(item_id)
            quantities[item_id] = 0
        quantities[item_id] += quantity
        if quantities[item_id] > MAX_QUANTITY:
            raise ValueError(f"quantity must be at most {MAX_QUANTITY} per dish")
    return [(item_id, quantities[item_id]) for item_id in order]


def _price_ore(value):
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError("price")
    ore = Decimal(value) * 100
    if ore != ore.to_integral_value() or ore < 0:
        raise ValueError("price")
    return int(ore)


def price_lines(location_id, wanted, mode, on_date):
    """Reads the dishes from the menu and prices the order. Unknown,
    inactive or out-of-schedule dishes -> ValueError("menu_item_unavailable")."""
    menu_table = os.environ["MENU_TABLE_NAME"]
    keys = [{"PK": {"S": location_pk(location_id)}, "SK": {"S": f"MENU#{item_id}"}}
            for item_id, _ in wanted]
    found = {}
    request = {menu_table: {"Keys": keys, "ConsistentRead": True}}
    for _ in range(5):
        response = dynamo.client().batch_get_item(RequestItems=request)
        for raw in (response.get("Responses") or {}).get(menu_table, []):
            item = {k: _deserializer.deserialize(v) for k, v in raw.items()}
            found[item.get("menuItemId")] = item
        request = response.get("UnprocessedKeys") or {}
        if not request:
            break
    else:
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException",
                                     "Message": "menu read incomplete"}}, "BatchGetItem")
    now = now_utc()
    lines = []
    for item_id, quantity in wanted:
        item = found.get(item_id)
        if not item or item.get("SK") != f"MENU#{item_id}":
            raise ValueError("menu_item_unavailable")
        try:
            available = menu.is_available(item, now)
        except ValueError:
            available = False  # a corrupt stored bound is unavailable, not a 400 with its text
        if not available:
            raise ValueError("menu_item_unavailable")
        try:
            unit = _price_ore(item.get("price"))
        except ValueError:
            raise ValueError("menu_item_unavailable") from None
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("menu_item_unavailable")
        alcohol = item.get("alcohol") is True
        if alcohol and mode == PICKUP:
            # Restaurants may serve alcohol, not sell it to take away.
            raise ValueError("alcohol_not_for_pickup")
        lines.append({
            "menuItemId": item_id,
            "name": name.strip()[:120],
            "unitPrice": unit,
            "quantity": quantity,
            "total": unit * quantity,
            "vatRate": vat_rate(alcohol, mode, on_date),
            **({"alcohol": True} if alcohol else {}),
        })
    total = sum(line["total"] for line in lines)
    if total <= 0:
        raise ValueError("order total must be more than 0 kr")
    if total > MAX_ORDER_ORE:
        raise ValueError("order total is too large")
    return lines, total


# --- signed codes (table QR, guest status link) ----------------------------------------------

def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(*parts):
    message = "|".join(str(p) for p in parts).encode("utf-8")
    # The guest-link key (shared/manage_link.py) with its own prefix per use,
    # so a signature made for one purpose never verifies for another.
    return _b64(hmac.new(manage_link._key(), message, hashlib.sha256).digest())


def clean_table_label(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("table label is required")
    value = " ".join(value.split())
    if len(value) > _TABLE_LABEL_MAX:
        raise ValueError(f"table label may be at most {_TABLE_LABEL_MAX} characters")
    return value


def table_code(tenant_id, location_id, table_id, label):
    """The value printed in a table's QR code: payload.signature. The table's
    label is inside, so it prints on the kitchen ticket without a lookup."""
    if not isinstance(table_id, str) or not table_id.strip() or len(table_id) > _TABLE_ID_MAX:
        raise ValueError("tableId is invalid")
    label = clean_table_label(label)
    payload = _b64(json.dumps({"t": table_id, "n": label}, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8"))
    return f"{payload}.{_sign('table-qr', 'v1', tenant_id, location_id, payload)}"


def read_table_code(tenant_id, location_id, code):
    """(tableId, label) from a QR code of this location, else ValueError."""
    if not isinstance(code, str) or len(code) > 512 or code.count(".") != 1:
        raise ValueError("table code is invalid")
    payload, signature = code.split(".")
    expected = _sign("table-qr", "v1", tenant_id, location_id, payload)
    if not hmac.compare_digest(expected, signature):
        raise ValueError("table code is invalid")
    try:
        data = json.loads(_unb64(payload))
        return data["t"], clean_table_label(data["n"])
    except (ValueError, KeyError, TypeError):
        raise ValueError("table code is invalid") from None


def status_token(item):
    return _sign("order-status", "v1", item["tenantId"], item["locationId"], item["orderId"])


def token_matches(item, token):
    if not isinstance(token, str) or not token or len(token) > 256 or not item.get("tenantId"):
        return False
    return hmac.compare_digest(status_token(item), token)


def status_url(site_base, item, *, slug=None):
    if not site_base:
        return None
    query = f"?restaurang={slug}" if slug else ""
    return (f"{site_base.rstrip('/')}/bestallning/{item['locationId']}/{item['orderId']}"
            f"{query}#{status_token(item)}")


# --- reading ----------------------------------------------------------------------------

def load(location_id, order_id):
    if not valid_order_id(order_id):
        raise NotFound
    tbl = dynamo.table(order_table())
    pointer = tbl.get_item(Key=pointer_key(location_id, order_id), ConsistentRead=True).get("Item")
    if not pointer or not isinstance(pointer.get("date"), str):
        raise NotFound
    item = tbl.get_item(Key=order_key(location_id, pointer["date"], order_id),
                        ConsistentRead=True).get("Item")
    if not item or item.get("locationId") != location_id:
        raise NotFound
    return item


def list_day(location_id, date_str):
    if not isinstance(date_str, str) or not _DATE.fullmatch(date_str):
        raise ValueError("date must be YYYY-MM-DD")
    try:
        date.fromisoformat(date_str)
    except ValueError:
        raise ValueError("date must be YYYY-MM-DD") from None
    tbl = dynamo.table(order_table())
    request = {
        "KeyConditionExpression": Key("PK").eq(location_pk(location_id))
        & Key("SK").begins_with(f"ORDER#{date_str}#"),
        "ConsistentRead": True,
    }
    items = []
    while True:
        page = tbl.query(**request)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return items
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def next_number(location_id, date_str):
    """The day's next order number (1, 2, ...) - shown to the guest and on
    the kitchen screen."""
    result = dynamo.table(order_table()).update_item(
        Key=counter_key(location_id, date_str),
        UpdateExpression="ADD #n :one",
        ExpressionAttributeNames={"#n": "lastNumber"},
        ExpressionAttributeValues={":one": 1},
        ReturnValues="UPDATED_NEW",
    )
    return int(result["Attributes"]["lastNumber"])


# --- views -----------------------------------------------------------------------------

def _lines_view(item):
    return [{k: line[k] for k in ("menuItemId", "name", "unitPrice", "quantity", "total", "vatRate")
             if k in line} for line in item.get("lines") or []]


def guest_view(item, location=None):
    view = {
        "orderId": item["orderId"],
        "locationId": item["locationId"],
        "number": item.get("number"),
        "status": item["status"],
        "paymentStatus": item.get("paymentStatus"),
        "mode": item["mode"],
        "tableLabel": item.get("tableLabel"),
        "lines": _lines_view(item),
        "totalAmount": item["totalAmount"],
        # Welcome-offer code: percent, saved amount (the lines are already discounted).
        "discount": item.get("discount"),
        "vat": item.get("vat") or [],
        "currency": item.get("currency", CURRENCY),
        "customerName": item.get("customerName"),
        "createdAt": item.get("createdAt"),
        "confirmedAt": item.get("confirmedAt"),
        "readyEstimate": item.get("readyEstimate"),
        "readyAt": item.get("readyAt"),
        "language": item.get("language", "sv"),
    }
    if location:
        view["location"] = {k: location.get(k) for k in ("name", "address", "phoneNumber")}
    return {k: v for k, v in view.items() if v is not None}


def staff_view(item):
    view = guest_view(item)
    view.update({
        "date": item.get("date"),
        "customerPhone": item.get("customerPhone"),
        "customerEmail": item.get("customerEmail"),
        "notes": item.get("notes"),
        "tableId": item.get("tableId"),
        "completedAt": item.get("completedAt"),
        "cancelledAt": item.get("cancelledAt"),
        "refund": item.get("refund"),
        "history": item.get("history") or [],
    })
    return {k: v for k, v in view.items() if v is not None}

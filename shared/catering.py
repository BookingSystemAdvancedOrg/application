"""Catering: settings, discount tiers, pricing, the request record and its
status machine, magic links and history (infrastructure docs/CATERING-FLOW.md).

Flow:
    requested         guest sends a request from the calculator (owner: SMS + e-mail)
    offer_sent        owner sends offer version N (capacity for the date held)
    change_requested  guest asks for changes (owner revises and sends N+1)
    signed            guest signed version N with BankID; a private guest pays next
    confirmed         private guest paid (Stripe Checkout) / company invoiced (Stripe)
    delivered         owner marked it delivered (closed once it is also paid)
    closed            delivered and paid
    declined          owner declined the request
    rejected          guest declined the offer
    expired           request or offer ran out
    cancelled         restaurant cancelled after confirmation (refund / credit note)

Every status write is conditional on `status` and `version` (version + 1) and
appends an AUDIT# entry to catering-request-history. A write the guest or the
restaurant should hear about sets `notice` on the head item; the table's
stream delivers it to the notification function (same contract as
reservations and orders).

Amounts are öre. Prices include VAT (12 % - catering is a restaurant service;
the delivery and cleaning fees follow the food).

Equipment return: the guest chooses to bring dishes and equipment back
cleaned (free) or not cleaned (the location's `cleaningFee`). The request and
every offer version keep the choice and the fee (öre), so a signed
version never changes when the owner changes the fee later.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from shared import dynamo

CURRENCY = "sek"
VAT_RATE = 12
MAX_LINES = 40
MAX_LINE_QTY = 5000
MAX_TOTAL_ORE = 50_000_000  # 500 000 kr - typo guard
NOTES_MAX = 1000
MESSAGE_MAX = 1000
TERMS_MAX = 4000

REQUESTED = "requested"
OFFER_SENT = "offer_sent"
CHANGE_REQUESTED = "change_requested"
SIGNED = "signed"
CONFIRMED = "confirmed"
DELIVERED = "delivered"
CLOSED = "closed"
DECLINED = "declined"
REJECTED = "rejected"
EXPIRED = "expired"
CANCELLED = "cancelled"
OPEN = (REQUESTED, OFFER_SENT, CHANGE_REQUESTED, SIGNED, CONFIRMED, DELIVERED)
HOLDS_CAPACITY = (OFFER_SENT, CHANGE_REQUESTED, SIGNED, CONFIRMED, DELIVERED)

PRIVATE = "private"
COMPANY = "company"
PICKUP = "pickup"
DELIVERY = "delivery"
CLEANED = "cleaned"
NOT_CLEANED = "not_cleaned"
EQUIPMENT_RETURNS = (CLEANED, NOT_CLEANED)

# notice types (guest and/or owner messages, shared/catering_messages.py)
N_REQUEST_RECEIVED = "catering_request_received"
N_OFFER_SENT = "catering_offer_sent"
N_CHANGE_REQUESTED = "catering_change_requested"
N_REJECTED = "catering_rejected"
N_DECLINED = "catering_declined"
N_SIGNED = "catering_signed"
N_CONFIRMED = "catering_confirmed"
N_CANCELLED = "catering_cancelled"
N_EXPIRED = "catering_expired"
NOTICE_TYPES = (N_REQUEST_RECEIVED, N_OFFER_SENT, N_CHANGE_REQUESTED, N_REJECTED, N_DECLINED,
                N_SIGNED, N_CONFIRMED, N_CANCELLED, N_EXPIRED)

_ID = re.compile(r"[0-9a-f]{32}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_ORG = re.compile(r"\d{6}-?\d{4}")
_POSTAL = re.compile(r"\d{3} ?\d{2}")
_ser = TypeSerializer()
_deser = TypeDeserializer()


class NotFound(Exception):
    pass


class InvalidState(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Unavailable(Exception):
    """Catering can't be requested (409 with the reason)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


# --- basics ----------------------------------------------------------------------------

def requests_table():
    return os.environ["CATERING_REQUESTS_TABLE_NAME"]


def history_table():
    return os.environ["CATERING_REQUEST_HISTORY_TABLE_NAME"]


def now_utc():
    return datetime.now(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def new_id():
    return uuid.uuid4().hex


def valid_id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def location_pk(location_id):
    return f"LOCATION#{location_id}"


def request_key(location_id, request_id):
    return {"PK": location_pk(location_id), "SK": f"REQUEST#{request_id}"}


def capacity_key(location_id, day):
    return {"PK": location_pk(location_id), "SK": f"CAPACITY#{day}"}


def serialize(item):
    return {k: _ser.serialize(v) for k, v in item.items() if v is not None}


def deserialize(raw):
    return {k: _deser.deserialize(v) for k, v in (raw or {}).items()}


def notice(kind, at, **extra):
    if kind not in NOTICE_TYPES:
        raise ValueError(f"unknown notice {kind}")
    return {"id": new_id(), "type": kind, "at": iso(at), **extra}


def whole(value, low, high, field):
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a whole number")
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise ValueError(f"{field} must be a whole number")
        value = int(value)
    if not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{field} must be a whole number between {low} and {high}")
    return value


def text(value, field, *, required=False, max_length=200):
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if len(value) > max_length:
        raise ValueError(f"{field} may be at most {max_length} characters")
    return value


def kronor_to_ore(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError(f"{field} must be a number")
    ore = Decimal(value) * 100
    if ore != ore.to_integral_value() or ore < 0:
        raise ValueError(f"{field} must be at least 0 with at most two decimals")
    return int(ore)


def vat_of(gross):
    return int((Decimal(gross) * VAT_RATE / (100 + VAT_RATE)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


# --- settings (location item `cateringSettings`) -------------------------------------------

DEFAULT_SETTINGS = {
    "enabled": False,
    "minPortions": 10,
    "maxPortions": 300,
    "advanceDays": 5,
    "maxPortionsPerDay": None,
    "pickup": True,
    "delivery": True,
    "deliveryArea": "",
    "maxDistanceKm": 25,
    "priceWithinLimit": 395,
    "priceBeyondLimit": 795,
    "allowOutsideArea": False,
    "cleaningFee": 0,
    "offerValidDays": 7,
    "paymentTermsDays": 30,
    "requestExpiryHours": 72,
    "terms": "",
    "notifyEmail": None,
    "notifyPhone": None,
}


def validate_settings(raw, *, phone_normalizer=None):
    """Owner input (merged over the defaults) -> stored settings."""
    if not isinstance(raw, dict):
        raise ValueError("cateringSettings must be an object")
    unknown = sorted(set(raw) - set(DEFAULT_SETTINGS))
    if unknown:
        raise ValueError(f"unsupported catering settings: {', '.join(unknown)}")
    s = {**DEFAULT_SETTINGS, **raw}
    out = {}
    for flag in ("enabled", "pickup", "delivery", "allowOutsideArea"):
        if not isinstance(s[flag], bool):
            raise ValueError(f"{flag} must be true or false")
        out[flag] = s[flag]
    out["minPortions"] = whole(s["minPortions"], 1, 5000, "minPortions")
    out["maxPortions"] = whole(s["maxPortions"], 1, 5000, "maxPortions")
    if out["minPortions"] > out["maxPortions"]:
        raise ValueError("minPortions must not be more than maxPortions")
    out["advanceDays"] = whole(s["advanceDays"], 0, 90, "advanceDays")
    out["maxPortionsPerDay"] = (None if s["maxPortionsPerDay"] is None
                                else whole(s["maxPortionsPerDay"], 1, 100000, "maxPortionsPerDay"))
    out["deliveryArea"] = text(s["deliveryArea"], "deliveryArea", max_length=80) or ""
    out["maxDistanceKm"] = whole(s["maxDistanceKm"], 0, 1000, "maxDistanceKm")
    out["priceWithinLimit"] = whole(s["priceWithinLimit"], 0, 100000, "priceWithinLimit")
    out["priceBeyondLimit"] = whole(s["priceBeyondLimit"], 0, 100000, "priceBeyondLimit")
    out["cleaningFee"] = whole(s["cleaningFee"], 0, 100000, "cleaningFee")
    out["offerValidDays"] = whole(s["offerValidDays"], 1, 60, "offerValidDays")
    out["paymentTermsDays"] = whole(s["paymentTermsDays"], 0, 90, "paymentTermsDays")
    out["requestExpiryHours"] = whole(s["requestExpiryHours"], 12, 720, "requestExpiryHours")
    out["terms"] = text(s["terms"], "terms", max_length=TERMS_MAX) or ""
    email = text(s["notifyEmail"], "notifyEmail", max_length=254)
    if email and not _EMAIL.fullmatch(email):
        raise ValueError("notifyEmail must be a valid email address")
    out["notifyEmail"] = email.lower() if email else None
    phone = text(s["notifyPhone"], "notifyPhone", max_length=30)
    out["notifyPhone"] = phone_normalizer(phone) if phone and phone_normalizer else phone
    if out["enabled"] and not (out["pickup"] or out["delivery"]):
        raise ValueError("enabled catering needs pickup or delivery")
    return out


def settings_of(location):
    raw = (location or {}).get("cateringSettings")
    try:
        return validate_settings(raw if isinstance(raw, dict) else {})
    except ValueError:
        return dict(DEFAULT_SETTINGS)


def public_settings(settings):
    keys = ("enabled", "minPortions", "maxPortions", "advanceDays", "pickup", "delivery", "deliveryArea",
            "maxDistanceKm", "priceWithinLimit", "priceBeyondLimit", "allowOutsideArea", "cleaningFee", "terms",
            "offerValidDays", "paymentTermsDays")
    return {k: settings[k] for k in keys}


# --- discount tiers (catering-discount-tiers table) ---------------------------------------

def tiers_table():
    return os.environ["CATERING_DISCOUNT_TIERS_TABLE_NAME"]


def validate_tier(raw):
    if not isinstance(raw, dict):
        raise ValueError("tier must be an object")
    unknown = sorted(set(raw) - {"name", "fromPortions", "toPortions", "discountPercent", "active"})
    if unknown:
        raise ValueError(f"unsupported fields: {', '.join(unknown)}")
    tier = {
        "name": text(raw.get("name"), "name", required=True, max_length=40),
        "fromPortions": whole(raw.get("fromPortions"), 1, 5000, "fromPortions"),
        "toPortions": None if raw.get("toPortions") is None else whole(raw["toPortions"], 1, 5000, "toPortions"),
        "discountPercent": whole(raw.get("discountPercent", 0), 0, 50, "discountPercent"),
        "active": raw.get("active", True),
    }
    if not isinstance(tier["active"], bool):
        raise ValueError("active must be true or false")
    if tier["toPortions"] is not None and tier["toPortions"] < tier["fromPortions"]:
        raise ValueError("toPortions must not be less than fromPortions")
    return tier


def list_tiers(location_id):
    items = dynamo.table(tiers_table()).query(
        KeyConditionExpression=Key("PK").eq(location_pk(location_id)) & Key("SK").begins_with("TIER#"),
        ConsistentRead=True,
    ).get("Items") or []
    out = []
    for item in items:
        try:
            tier = validate_tier({k: item.get(k) for k in ("name", "fromPortions", "toPortions",
                                                            "discountPercent", "active")})
        except ValueError:
            continue
        out.append({"tierId": item["SK"].removeprefix("TIER#"), **tier})
    return sorted(out, key=lambda t: (t["fromPortions"], t["name"]))


def tier_for(tiers, portions):
    best = None
    for tier in tiers:
        if not tier["active"] or portions < tier["fromPortions"]:
            continue
        if tier["toPortions"] is not None and portions > tier["toPortions"]:
            continue
        if best is None or tier["discountPercent"] > best["discountPercent"]:
            best = tier
    return best


# --- pricing ---------------------------------------------------------------------------------

def totals(lines, discount_percent, delivery_fee, cleaning_fee=0):
    """{subtotal, discountPercent, discount, deliveryFee, cleaningFee, total, vat}
    in öre. The discount is on the dishes only, not on the fees."""
    subtotal = sum(int(line["unitPrice"]) * int(line["quantity"]) for line in lines)
    discount = int((Decimal(subtotal) * int(discount_percent) / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    total = subtotal - discount + int(delivery_fee) + int(cleaning_fee)
    return {"subtotal": subtotal, "discountPercent": int(discount_percent), "discount": discount,
            "deliveryFee": int(delivery_fee), "cleaningFee": int(cleaning_fee), "total": total,
            "vat": vat_of(total), "vatRate": VAT_RATE}


def delivery_fee(settings, fulfilment, zone):
    if fulfilment == PICKUP:
        return 0
    if zone == "beyond":
        if not settings["allowOutsideArea"]:
            raise Unavailable("catering_outside_area")
        return settings["priceBeyondLimit"] * 100
    return settings["priceWithinLimit"] * 100


def equipment_return(value):
    """The guest's choice; a request without one (an older site) is cleaned."""
    if value is None:
        return CLEANED
    if value not in EQUIPMENT_RETURNS:
        raise ValueError("equipmentReturn must be cleaned or not_cleaned")
    return value


def cleaning_fee(settings, choice):
    return settings["cleaningFee"] * 100 if choice == NOT_CLEANED else 0


def read_menu(location_id, item_ids):
    menu_table = os.environ["MENU_TABLE_NAME"]
    keys = [{"PK": {"S": location_pk(location_id)}, "SK": {"S": f"MENU#{i}"}} for i in item_ids]
    found = {}
    request = {menu_table: {"Keys": keys, "ConsistentRead": True}}
    for _ in range(5):
        response = dynamo.client().batch_get_item(RequestItems=request)
        for raw in (response.get("Responses") or {}).get(menu_table, []):
            item = deserialize(raw)
            found[item.get("menuItemId")] = item
        request = response.get("UnprocessedKeys") or {}
        if not request:
            return found
    raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "menu"}},
                      "BatchGetItem")


def validate_wanted(raw):
    if not isinstance(raw, list) or not raw:
        raise ValueError("items must be a non-empty list")
    if len(raw) > MAX_LINES:
        raise ValueError(f"items may have at most {MAX_LINES} lines")
    quantities, order = {}, []
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) - {"menuItemId", "quantity"}:
            raise ValueError("each item needs menuItemId and quantity")
        item_id = entry.get("menuItemId")
        if not isinstance(item_id, str) or not item_id.strip() or len(item_id) > 128 or "#" in item_id:
            raise ValueError("menuItemId is invalid")
        qty = whole(entry.get("quantity"), 1, MAX_LINE_QTY, "quantity")
        if item_id not in quantities:
            order.append(item_id)
            quantities[item_id] = 0
        quantities[item_id] += qty
    return [(i, quantities[i]) for i in order]


def price_request(location_id, settings, tiers, wanted, fulfilment, zone, equipment=CLEANED):
    """Lines from the menu (alcohol excluded), the tier discount on total
    portions, the delivery fee and the cleaning fee."""
    menu = read_menu(location_id, [i for i, _ in wanted])
    lines = []
    for item_id, qty in wanted:
        item = menu.get(item_id)
        if not item or item.get("active") is not True or item.get("alcohol") is True:
            raise ValueError("menu_item_unavailable")
        try:
            unit = kronor_to_ore(item.get("price"), "price")
        except ValueError:
            raise ValueError("menu_item_unavailable") from None
        lines.append({"menuItemId": item_id, "name": str(item.get("name") or "")[:120],
                      "unitPrice": unit, "quantity": qty})
    portions = sum(line["quantity"] for line in lines)
    if portions < settings["minPortions"]:
        raise ValueError(f"at least {settings['minPortions']} portions")
    if portions > settings["maxPortions"]:
        raise ValueError(f"at most {settings['maxPortions']} portions")
    tier = tier_for(tiers, portions)
    result = totals(lines, tier["discountPercent"] if tier else 0, delivery_fee(settings, fulfilment, zone),
                    cleaning_fee(settings, equipment))
    if result["total"] > MAX_TOTAL_ORE:
        raise ValueError("order total is too large")
    return {"lines": lines, "portions": portions, "tierName": tier["name"] if tier else None,
            "equipmentReturn": equipment, **result}


def check_date(settings, location, day, now):
    try:
        wanted = date.fromisoformat(day)
    except (TypeError, ValueError):
        raise ValueError("deliveryDate must be YYYY-MM-DD") from None
    tz = ZoneInfo((location or {}).get("timezone") or "Europe/Stockholm")
    today = now.astimezone(tz).date()
    if wanted < today + timedelta(days=settings["advanceDays"]):
        raise Unavailable("catering_too_soon")
    if wanted > today + timedelta(days=365):
        raise ValueError("deliveryDate must be within a year")
    specials = (location or {}).get("specialDays") or []
    if any(isinstance(s, dict) and s.get("date") == day and s.get("kind") == "closed" for s in specials):
        raise Unavailable("catering_closed_day")
    return wanted


def capacity_left(location_id, settings, day):
    limit = settings["maxPortionsPerDay"]
    if limit is None:
        return None
    item = dynamo.table(requests_table()).get_item(Key=capacity_key(location_id, day),
                                                   ConsistentRead=True).get("Item") or {}
    return limit - int(item.get("heldPortions") or 0)


def capacity_change(location_id, settings, day, delta):
    """Transaction item adding `delta` held portions on the day (None when
    nothing to do). Raises nothing itself; the condition fails the
    transaction when the day is full."""
    if not delta:
        return None
    update = {
        "TableName": requests_table(),
        "Key": serialize(capacity_key(location_id, day)),
        "UpdateExpression": "ADD heldPortions :d",
        "ExpressionAttributeValues": serialize({":d": int(delta)}),
    }
    limit = settings.get("maxPortionsPerDay")
    if delta > 0 and limit is not None and delta > int(limit):
        raise InvalidState("capacity_full")
    if delta > 0 and limit is not None:
        update["ConditionExpression"] = "attribute_not_exists(heldPortions) OR heldPortions <= :room"
        update["ExpressionAttributeValues"] = serialize({":d": int(delta), ":room": int(limit) - int(delta)})
    return {"Update": update}


# --- the record ------------------------------------------------------------------------------

def load(location_id, request_id):
    if not valid_id(request_id):
        raise NotFound
    item = dynamo.table(requests_table()).get_item(Key=request_key(location_id, request_id),
                                                   ConsistentRead=True).get("Item")
    if not item or item.get("locationId") != location_id:
        raise NotFound
    return item


def list_requests(location_id):
    tbl = dynamo.table(requests_table())
    request = {"KeyConditionExpression": Key("PK").eq(location_pk(location_id))
               & Key("SK").begins_with("REQUEST#"), "ConsistentRead": True}
    items = []
    while True:
        page = tbl.query(**request)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return items
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def audit_put(request_id, action, actor, at, **extra):
    """Append-only history entry (transaction item)."""
    # time_ns keeps entries written in the same second in order
    sk = f"AUDIT#{iso(at)}#{time.time_ns():020d}"
    entry = {"PK": f"REQUEST#{request_id}", "SK": sk, "action": action, "by": actor, "at": iso(at),
             **{k: v for k, v in extra.items() if v is not None}}
    return {"Put": {"TableName": history_table(), "Item": serialize(entry),
                    "ConditionExpression": "attribute_not_exists(SK)"}}


def offer_key(request_id, version):
    return {"PK": f"REQUEST#{request_id}", "SK": f"OFFER#v{int(version):04d}"}


def load_offer(request_id, version):
    item = dynamo.table(history_table()).get_item(Key=offer_key(request_id, version),
                                                  ConsistentRead=True).get("Item")
    if not item:
        raise NotFound
    return item


def history(request_id, limit=200):
    items = dynamo.table(history_table()).query(
        KeyConditionExpression=Key("PK").eq(f"REQUEST#{request_id}") & Key("SK").begins_with("AUDIT#"),
        ScanIndexForward=False, Limit=limit,
    ).get("Items") or []
    return [{k: v for k, v in i.items() if k not in ("PK", "SK")} for i in items]


def head_update(item, *, to_status=None, sets=None, removes=(), values=None, names=None, expected=None):
    """Transaction item: conditional on the status (expected, default the
    current one) and the version the caller read; bumps the version."""
    names = {"#s": "status", "#v": "version", **(names or {})}
    values = {":expected": expected or item["status"], ":version": int(item.get("version") or 0),
              ":one": 1, **(values or {})}
    parts = list(sets or [])
    if to_status:
        parts.append("#s = :to")
        values[":to"] = to_status
    parts.append("#v = :version + :one")
    expression = "SET " + ", ".join(parts)
    if removes:
        expression += " REMOVE " + ", ".join(removes)
    return {"Update": {
        "TableName": requests_table(),
        "Key": serialize(request_key(item["locationId"], item["requestId"])),
        "UpdateExpression": expression,
        "ConditionExpression": "attribute_exists(PK) AND #s = :expected AND #v = :version",
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": serialize(values),
    }}


def transact(items):
    items = [i for i in items if i]
    try:
        dynamo.client().transact_write_items(TransactItems=items)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        reasons = exc.response.get("CancellationReasons") or []
        for i, reason in enumerate(reasons):
            if isinstance(reason, dict) and reason.get("Code") in ("ConditionalCheckFailed", "TransactionConflict"):
                key = deserialize(next(iter(items[i].values())).get("Key") or {})
                if str(key.get("SK", "")).startswith("CAPACITY#"):
                    raise InvalidState("capacity_full") from None
                raise InvalidState("changed_retry") from None
        raise


# --- magic link ------------------------------------------------------------------------------

_link_cache = {"key": None, "at": 0.0}


class LinkKeyUnavailable(RuntimeError):
    pass


def _link_key():
    now = time.monotonic()
    if _link_cache["key"] is None or now - _link_cache["at"] > 300:
        # notification calls it CATERING_LINK_SIGNING_KEY_SECRET_ARN (it holds two link keys)
        arn = os.environ.get("LINK_SIGNING_KEY_SECRET_ARN") or os.environ.get("CATERING_LINK_SIGNING_KEY_SECRET_ARN")
        if not arn:
            raise LinkKeyUnavailable("LINK_SIGNING_KEY_SECRET_ARN is not set")
        secret = boto3.client("secretsmanager").get_secret_value(SecretId=arn).get("SecretString")
        if not isinstance(secret, str) or len(secret) < 32:
            raise LinkKeyUnavailable("catering link key missing or too short")
        _link_cache.update(key=secret.encode("utf-8"), at=now)
    return _link_cache["key"]


def reset_cache():
    _link_cache.update(key=None, at=0.0)


def token_for(item):
    message = f"{item['locationId']}#{item['requestId']}#{int(item.get('linkVersion') or 1)}".encode("utf-8")
    return base64.urlsafe_b64encode(hmac.new(_link_key(), message, hashlib.sha256).digest()).rstrip(b"=").decode()


def token_matches(item, token):
    if not isinstance(token, str) or not token or len(token) > 256:
        return False
    return hmac.compare_digest(token_for(item), token)


def guest_url(site_base, item, *, slug=None):
    if not site_base:
        return None
    query = f"?restaurang={slug}" if slug else ""
    return f"{site_base.rstrip('/')}/catering/{item['locationId']}/{item['requestId']}{query}#t={token_for(item)}"


def admin_url(admin_base, item):
    if not admin_base:
        return None
    return f"{admin_base.rstrip('/')}/catering/{item['locationId']}/{item['requestId']}"


# --- offer document --------------------------------------------------------------------------

def offer_document(item, offer, tenant_row, location, settings):
    """The exact content the guest signs: who sells, to whom, what, when,
    for how much, on which terms. Its SHA-256 is the document id shown in
    BankID and stored with the signature."""
    return {
        "type": "catering-offer",
        "requestId": item["requestId"],
        "version": int(offer["version"]),
        "seller": {"name": (tenant_row or {}).get("legalName") or (tenant_row or {}).get("name"),
                   "orgNumber": (tenant_row or {}).get("orgNumber"),
                   "location": (location or {}).get("name")},
        "customer": {"type": item.get("customerType"), "name": item.get("customerName"),
                     "companyName": item.get("companyName"), "orgNumber": item.get("orgNumber"),
                     "email": item.get("customerEmail")},
        "delivery": {"date": item.get("deliveryDate"), "time": item.get("deliveryTime"),
                     "fulfilment": item.get("fulfilment"), "address": item.get("deliveryAddress")},
        "lines": [{"name": line["name"], "unitPrice": int(line["unitPrice"]), "quantity": int(line["quantity"])}
                  for line in offer["lines"]],
        "totals": {k: int(offer["totals"][k]) for k in ("subtotal", "discount", "deliveryFee", "total", "vat")},
        **_equipment_terms(offer),
        "currency": CURRENCY,
        "validUntil": offer["validUntil"],
        "paymentTermsDays": int(settings.get("paymentTermsDays") or 0) if item.get("customerType") == COMPANY else 0,
        "terms": offer.get("terms") or "",
        "message": offer.get("message") or "",
    }


def _equipment_terms(offer):
    """Signed with the offer. Left out of offers sent before the choice
    existed, so their document (and its hash) reads as it did."""
    if not offer.get("equipmentReturn"):
        return {}
    return {"equipmentReturn": offer["equipmentReturn"],
            "cleaningFee": int(offer["totals"].get("cleaningFee") or 0)}


def plain(value):
    """DynamoDB's Decimals back to int/float, so a document read back from
    the table hashes exactly like the one that was signed."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


def document_hash(document):
    canonical = json.dumps(plain(document), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def money(ore):
    text_ = f"{int(ore) / 100:,.2f}".replace(",", " ").replace(".", ",")
    return (text_[:-3] if text_.endswith(",00") else text_) + " kr"


# --- views -----------------------------------------------------------------------------------

_GUEST_FIELDS = ("requestId", "locationId", "status", "customerType", "customerName", "companyName",
                 "orgNumber", "invoiceReference", "deliveryDate", "deliveryTime", "fulfilment",
                 "deliveryZone", "deliveryAddress", "guests", "notes", "request", "createdAt",
                 "currentOfferVersion", "validUntil", "signedAt", "signedOfferVersion", "paid",
                 "invoiceNumber", "invoiceUrl", "invoiceStatus", "declineReason", "cancelReason",
                 "changeMessage", "delivered", "equipmentReturn")


def guest_view(item, offer=None, location=None, settings=None):
    view = {k: item.get(k) for k in _GUEST_FIELDS if item.get(k) is not None}
    signer = (item.get("signature") or {}).get("name")
    if signer:
        view["signedBy"] = signer
    if offer:
        view["offer"] = {
            "version": int(offer["version"]),
            "lines": offer["lines"],
            "totals": offer["totals"],
            "equipmentReturn": offer.get("equipmentReturn"),
            "validUntil": offer["validUntil"],
            "message": offer.get("message"),
            "terms": offer.get("terms"),
            "documentHash": offer.get("documentHash"),
            "sentAt": offer.get("createdAt"),
        }
    if location:
        view["location"] = {k: location.get(k) for k in ("name", "address", "phoneNumber", "email")}
    if settings and item.get("customerType") == COMPANY:
        view["paymentTermsDays"] = settings.get("paymentTermsDays")
    view["canSign"] = item.get("status") == OFFER_SENT
    view["canPay"] = item.get("status") == SIGNED and item.get("customerType") == PRIVATE
    return view


def staff_view(item):
    view = {k: v for k, v in item.items() if k not in ("PK", "SK", "signing", "notice")}
    view.pop("signature", None)
    sig = item.get("signature") or {}
    if sig:
        view["signature"] = {k: sig.get(k) for k in ("name", "personalNumberMasked", "signedAt", "documentHash")}
    return view

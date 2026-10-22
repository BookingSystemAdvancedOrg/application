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
    # Issue 5 M1: stored and edited; pricing and date checks use them from M2/M3.
    "minGuests": 15,
    "leadTimeRules": [],  # [{fromGuests, toGuests|null, days}]; empty = advanceDays
    "deliveryBands": [],  # [{upToKm, price}]; empty = derived from the flat prices
    "maxDeliveryKm": None,  # always the last band's upToKm
    "defaultServiceIncluded": False,
    "ownerReminderHours": 24,  # 0 = off
    "signReminderDaysBefore": 2,  # 0 = off
    "dayBeforeReminder": True,
    "allowSimpleAcceptance": False,
}
MAX_LEAD_TIME_RULES = 10
MAX_DELIVERY_BANDS = 10


def validate_settings(raw, *, phone_normalizer=None):
    """Owner input (merged over the defaults) -> stored settings."""
    if not isinstance(raw, dict):
        raise ValueError("cateringSettings must be an object")
    unknown = sorted(set(raw) - set(DEFAULT_SETTINGS))
    if unknown:
        raise ValueError(f"unsupported catering settings: {', '.join(unknown)}")
    s = {**DEFAULT_SETTINGS, **raw}
    out = {}
    for flag in ("enabled", "pickup", "delivery", "allowOutsideArea", "defaultServiceIncluded",
                 "dayBeforeReminder", "allowSimpleAcceptance"):
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
    out["minGuests"] = whole(s["minGuests"], 1, 5000, "minGuests")
    out["leadTimeRules"] = _lead_time_rules(s["leadTimeRules"])
    _delivery_bands(s, out)
    # Checked against the expiry only once set: a stored location without
    # them must still load (settings_of) even with a short expiry.
    out["ownerReminderHours"] = whole(s["ownerReminderHours"], 0, 168, "ownerReminderHours")
    if "ownerReminderHours" in raw and out["ownerReminderHours"] >= out["requestExpiryHours"]:
        raise ValueError("ownerReminderHours must be less than requestExpiryHours")
    out["signReminderDaysBefore"] = whole(s["signReminderDaysBefore"], 0, 59, "signReminderDaysBefore")
    if "signReminderDaysBefore" in raw and out["signReminderDaysBefore"] >= out["offerValidDays"]:
        raise ValueError("signReminderDaysBefore must be less than offerValidDays")
    return out


def _lead_time_rules(raw):
    """Contiguous guest ranges, ascending; only the last may be open (toGuests null)."""
    if not isinstance(raw, list) or len(raw) > MAX_LEAD_TIME_RULES:
        raise ValueError(f"leadTimeRules must be a list of at most {MAX_LEAD_TIME_RULES} rules")
    rules = []
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) - {"fromGuests", "toGuests", "days"}:
            raise ValueError("each lead time rule needs fromGuests, toGuests and days")
        rule = {"fromGuests": whole(entry.get("fromGuests"), 1, 5000, "fromGuests"),
                "toGuests": None if entry.get("toGuests") is None else whole(entry["toGuests"], 1, 5000, "toGuests"),
                "days": whole(entry.get("days"), 0, 90, "days")}
        if rule["toGuests"] is not None and rule["toGuests"] < rule["fromGuests"]:
            raise ValueError("toGuests must not be less than fromGuests")
        rules.append(rule)
    rules.sort(key=lambda r: r["fromGuests"])
    for prev, nxt in zip(rules, rules[1:]):
        if prev["toGuests"] is None or nxt["fromGuests"] != prev["toGuests"] + 1:
            raise ValueError("leadTimeRules must not overlap or leave gaps")
    return rules


def _delivery_bands(s, out):
    """deliveryBands + maxDeliveryKm. Without bands (older settings) the flat
    prices become one band, or two when delivery beyond the limit was allowed
    (then without an upper limit, i.e. up to 1000 km). The flat fields are
    then derived back from the bands, so the current request pricing
    (within/beyond zone) stays in step until it moves to bands."""
    raw = s["deliveryBands"]
    if not isinstance(raw, list) or len(raw) > MAX_DELIVERY_BANDS:
        raise ValueError(f"deliveryBands must be a list of at most {MAX_DELIVERY_BANDS} bands")
    if not raw:
        raw = [{"upToKm": max(1, out["maxDistanceKm"]), "price": out["priceWithinLimit"]}]
        if out["allowOutsideArea"] and raw[0]["upToKm"] < 1000:
            raw.append({"upToKm": 1000, "price": out["priceBeyondLimit"]})
        maximum = raw[-1]["upToKm"]
    else:
        maximum = s["maxDeliveryKm"]
    bands = []
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) != {"upToKm", "price"}:
            raise ValueError("each delivery band needs upToKm and price")
        bands.append({"upToKm": whole(entry["upToKm"], 1, 1000, "upToKm"),
                      "price": whole(entry["price"], 0, 100000, "price")})
    if any(b["upToKm"] <= a["upToKm"] for a, b in zip(bands, bands[1:])):
        raise ValueError("deliveryBands must be in ascending order of upToKm")
    if maximum is not None and whole(maximum, 1, 1000, "maxDeliveryKm") != bands[-1]["upToKm"]:
        raise ValueError("maxDeliveryKm must be the last band's upToKm")
    out["deliveryBands"] = bands
    out["maxDeliveryKm"] = bands[-1]["upToKm"]
    out["maxDistanceKm"] = bands[0]["upToKm"]
    out["priceWithinLimit"] = bands[0]["price"]
    out["priceBeyondLimit"] = bands[-1]["price"]
    out["allowOutsideArea"] = len(bands) > 1


def settings_of(location):
    raw = (location or {}).get("cateringSettings")
    try:
        return validate_settings(raw if isinstance(raw, dict) else {})
    except ValueError:
        return dict(DEFAULT_SETTINGS)


def public_settings(settings):
    keys = ("enabled", "minPortions", "maxPortions", "advanceDays", "pickup", "delivery", "deliveryArea",
            "maxDistanceKm", "priceWithinLimit", "priceBeyondLimit", "allowOutsideArea", "cleaningFee", "terms",
            "offerValidDays", "paymentTermsDays", "minGuests", "leadTimeRules", "deliveryBands", "maxDeliveryKm",
            "defaultServiceIncluded", "allowSimpleAcceptance")
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


# --- catering menu (catering-menu table: DISH# and PACKAGE# per location) ------------------

# EU 1169/2011 annex II (the 14 allergens) and the dietary labels the admin offers.
ALLERGENS = ("gluten", "crustaceans", "eggs", "fish", "peanuts", "soy", "milk", "nuts", "celery", "mustard",
             "sesame", "sulphites", "lupin", "molluscs")
DIETARY = ("vegetarian", "vegan", "gluten_free", "lactose_free")
MAX_DISHES = 200
MAX_PACKAGES = 50
MAX_PACKAGE_DISHES = 40
MAX_MENU_PRICE = 10000  # kronor per portion / guest - typo guard
DISH = "DISH#"
PACKAGE = "PACKAGE#"
_DISH_FIELDS = ("name", "description", "pricePerPortion", "allergens", "dietary", "active", "sort",
                "sourceMenuItemId")
_PACKAGE_FIELDS = ("name", "description", "pricePerGuest", "dishIds", "minGuests", "active", "sort")


def menu_table():
    return os.environ["CATERING_MENU_TABLE_NAME"]


def menu_key(location_id, prefix, item_id):
    return {"PK": location_pk(location_id), "SK": f"{prefix}{item_id}"}


def _price(value, field):
    kronor_to_ore(value, field)  # at least 0, at most two decimals
    if Decimal(value) > MAX_MENU_PRICE:
        raise ValueError(f"{field} may be at most {MAX_MENU_PRICE}")
    return Decimal(value)


def _codes(value, allowed, field):
    if value is None:
        return []
    if not isinstance(value, list) or any(v not in allowed for v in value):
        raise ValueError(f"{field} must be a list of: {', '.join(allowed)}")
    return [v for v in allowed if v in value]


def _common(raw, fields):
    if not isinstance(raw, dict):
        raise ValueError("body must be an object")
    unknown = sorted(set(raw) - set(fields) - {"version"})
    if unknown:
        raise ValueError(f"unsupported fields: {', '.join(unknown)}")
    active = raw.get("active", True)
    if not isinstance(active, bool):
        raise ValueError("active must be true or false")
    return {"name": text(raw.get("name"), "name", required=True, max_length=120),
            "description": text(raw.get("description"), "description", max_length=500) or "",
            "active": active, "sort": whole(raw.get("sort", 0), 0, 10000, "sort")}


def validate_dish(raw):
    dish = _common(raw, _DISH_FIELDS)
    dish["pricePerPortion"] = _price(raw.get("pricePerPortion"), "pricePerPortion")
    dish["allergens"] = _codes(raw.get("allergens"), ALLERGENS, "allergens")
    dish["dietary"] = _codes(raw.get("dietary"), DIETARY, "dietary")
    source = text(raw.get("sourceMenuItemId"), "sourceMenuItemId", max_length=128)
    if source:
        dish["sourceMenuItemId"] = source
    return dish


def validate_package(raw, dish_ids):
    """`dish_ids`: the location's existing dish ids (a package only lists those)."""
    package = _common(raw, _PACKAGE_FIELDS)
    package["pricePerGuest"] = _price(raw.get("pricePerGuest"), "pricePerGuest")
    wanted = raw.get("dishIds")
    if not isinstance(wanted, list) or not wanted or len(wanted) > MAX_PACKAGE_DISHES:
        raise ValueError(f"dishIds must list 1 to {MAX_PACKAGE_DISHES} dishes")
    if len(set(wanted)) != len(wanted) or any(d not in dish_ids for d in wanted):
        raise ValueError("dishIds must be distinct dishes of this location")
    package["dishIds"] = list(wanted)
    if raw.get("minGuests") is not None:
        package["minGuests"] = whole(raw["minGuests"], 1, 5000, "minGuests")
    return package


def _menu_view(item, prefix, id_field):
    out = {k: v for k, v in item.items() if k not in ("PK", "SK")}
    return {id_field: item["SK"].removeprefix(prefix), **out}


def list_catering_menu(location_id, *, active_only=False):
    """{dishes, packages} sorted by sort then name. Active only = what a
    guest may order: active dishes, and active packages whose dishes are
    all active."""
    table = dynamo.table(menu_table())
    request = {"KeyConditionExpression": Key("PK").eq(location_pk(location_id)), "ConsistentRead": True}
    items = []
    while True:
        page = table.query(**request)
        items += page.get("Items") or []
        if "LastEvaluatedKey" not in page:
            break
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    dishes = [_menu_view(i, DISH, "dishId") for i in items if i["SK"].startswith(DISH)]
    packages = [_menu_view(i, PACKAGE, "packageId") for i in items if i["SK"].startswith(PACKAGE)]
    if active_only:
        dishes = [d for d in dishes if d.get("active") is True]
        live = {d["dishId"] for d in dishes}
        packages = [p for p in packages if p.get("active") is True and set(p.get("dishIds") or []) <= live]
    order = lambda x: (int(x.get("sort") or 0), str(x.get("name") or "").lower())  # noqa: E731
    return {"dishes": sorted(dishes, key=order), "packages": sorted(packages, key=order)}


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

"""The welcome offer: a small card on the restaurant's website ("10 % på din
nästa beställning") where a guest leaves their email, gives consent to
marketing and gets a single-use discount code by email.

Shared by marketing (the owner's settings), marketing-public (the card,
sign-up and code check), payment-intent (the code at checkout) and
marketing-sync (redeemed when the order is paid).

Data (marketing table):

    TENANT#<t>#OFFER   SIGNUP                 settings + counters (views, signups, redeemed, ...)
    TENANT#<t>#CODE    CODE#<code>            one code: percent, minimum, expiry, status, order
    TENANT#<t>#CODE    EMAIL#<subscriber id>  which code an address got (one per address, ever)

A code is "issued" until an order reserves it ("reserved" for the length of
the Stripe checkout, so an abandoned checkout frees it again) and "redeemed"
when that order is paid. The terms (percent, minimum, expiry) are copied
onto the code, so changing the offer later never changes a code already
sent.
"""

import re
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

OFFER_SK = "SIGNUP"
# No 0/O, 1/I/L: codes are read off a phone and typed in.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_RE = re.compile(r"[A-Z2-9]{4}-[A-Z2-9]{4}")
PAGES = ("all", "menu")
FREQUENCIES = ("visit", "page")
COUNTERS = ("views", "signups", "redeemed", "revenueOre", "discountOre")

DEFAULTS = {
    "enabled": False,
    "percent": 10,
    "minOrderOre": 0,
    "validDays": 30,
    "headline": "{procent} % på din nästa beställning",
    "text": "Lämna din e-post så skickar vi rabattkoden direkt. Du får våra erbjudanden och kan avsluta när du vill.",
    "buttonLabel": "Skicka min rabattkod",
    "imageUrl": None,
    "dishName": None,
    "imageMenuItemId": None,
    "imageLocationId": None,
    "delaySeconds": 20,
    "scrollPercent": 40,
    "pages": "all",
    "frequency": "visit",
    "showOnMobile": True,
    "activeFrom": None,
    "activeTo": None,
}

PUBLIC_FIELDS = ("percent", "minOrderOre", "validDays", "headline", "text", "buttonLabel", "imageUrl",
                 "dishName", "delaySeconds", "scrollPercent", "pages", "frequency", "showOnMobile")


def offer_key(tenant_id):
    return {"PK": f"TENANT#{tenant_id}#OFFER", "SK": OFFER_SK}


def code_pk(tenant_id):
    return f"TENANT#{tenant_id}#CODE"


def code_key(tenant_id, code):
    return {"PK": code_pk(tenant_id), "SK": f"CODE#{code}"}


def email_key(tenant_id, sid):
    return {"PK": code_pk(tenant_id), "SK": f"EMAIL#{sid}"}


def new_code():
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_code(value):
    """What a guest typed -> 'ABCD-EFGH', or None."""
    if not isinstance(value, str):
        return None
    raw = re.sub(r"[\s\-]", "", value).upper()
    if len(raw) != 8:
        return None
    code = f"{raw[:4]}-{raw[4:]}"
    return code if CODE_RE.fullmatch(code) else None


def _int(body, field, low, high):
    value = body.get(field, DEFAULTS[field])
    if isinstance(value, Decimal):
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValueError(f"{field} must be a whole number between {low} and {high}")
    return value


def _text(body, field, max_length):
    value = body.get(field, DEFAULTS[field])
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    value = " ".join(value.split())
    if len(value) > max_length:
        raise ValueError(f"{field} must be at most {max_length} characters")
    return value


def _day(body, field):
    value = body.get(field)
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a date (YYYY-MM-DD)")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} must be a date (YYYY-MM-DD)") from None
    return value


def validate_settings(body):
    """The owner's settings (the image is resolved from the menu by the caller)."""
    if not isinstance(body, dict):
        raise ValueError("settings must be an object")
    enabled = body.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be true or false")
    mobile = body.get("showOnMobile", True)
    if not isinstance(mobile, bool):
        raise ValueError("showOnMobile must be true or false")
    pages = body.get("pages", "all")
    if pages not in PAGES:
        raise ValueError(f"pages must be one of {', '.join(PAGES)}")
    frequency = body.get("frequency", "visit")
    if frequency not in FREQUENCIES:
        raise ValueError(f"frequency must be one of {', '.join(FREQUENCIES)}")
    out = {
        "enabled": enabled,
        "percent": _int(body, "percent", 5, 50),
        "minOrderOre": _int(body, "minOrderOre", 0, 500000),
        "validDays": _int(body, "validDays", 7, 90),
        "headline": _text(body, "headline", 60),
        "text": _text(body, "text", 180),
        "buttonLabel": _text(body, "buttonLabel", 30),
        "delaySeconds": _int(body, "delaySeconds", 0, 120),
        "scrollPercent": _int(body, "scrollPercent", 0, 90),
        "pages": pages,
        "frequency": frequency,
        "showOnMobile": mobile,
        "activeFrom": _day(body, "activeFrom"),
        "activeTo": _day(body, "activeTo"),
    }
    if out["activeFrom"] and out["activeTo"] and out["activeFrom"] > out["activeTo"]:
        raise ValueError("activeFrom must be on or before activeTo")
    check_percent_claims(out)
    item_id, location_id = body.get("imageMenuItemId"), body.get("imageLocationId")
    if item_id not in (None, ""):
        for value, field in ((item_id, "imageMenuItemId"), (location_id, "imageLocationId")):
            if not isinstance(value, str) or not value.strip() or len(value) > 128 or "#" in value:
                raise ValueError(f"{field} is invalid")
        out["imageMenuItemId"], out["imageLocationId"] = item_id.strip(), location_id.strip()
    else:
        out["imageMenuItemId"] = out["imageLocationId"] = None
    return out


def settings_view(item):
    """Settings with defaults for anything never saved, plus the counters."""
    item = item or {}
    view = {k: item.get(k, v) for k, v in DEFAULTS.items()}
    view["stats"] = {k: int(item.get(k) or 0) for k in COUNTERS}
    view["updatedAt"] = item.get("updatedAt")
    return view


def is_live(item, today):
    """Switched on and inside its optional date window (`today`: local ISO date)."""
    if not item or item.get("enabled") is not True:
        return False
    if item.get("activeFrom") and today < item["activeFrom"]:
        return False
    if item.get("activeTo") and today > item["activeTo"]:
        return False
    return True


def fill_percent(text, percent):
    return (text or "").replace("{procent}", str(int(percent)))


_PERCENT_IN_TEXT = re.compile(r"(\d{1,3})\s?%")


def check_percent_claims(settings):
    """A headline or text that names another percent than the code gives
    would be misleading advertising: use {procent} or the same number."""
    for field in ("headline", "text", "buttonLabel"):
        for number in _PERCENT_IN_TEXT.findall(settings[field]):
            if int(number) != settings["percent"]:
                raise ValueError(f"{field} says {number} % but the discount is {settings['percent']} % "
                                 "(write {procent} to always show the right number)")


def public_view(item):
    view = {k: item.get(k, DEFAULTS[k]) for k in PUBLIC_FIELDS}
    for field in ("headline", "text", "buttonLabel"):
        view[field] = fill_percent(view[field], view["percent"])
    return view


def expires_at(valid_days, now=None):
    now = now or datetime.now(timezone.utc)
    return (now + timedelta(days=int(valid_days))).strftime("%Y-%m-%dT%H:%M:%SZ")


def apply_discount(lines, percent):
    """Each line's unit price lowered by `percent` (rounded per unit, so VAT
    stays correct per rate). The original unit price is kept as listPrice.
    Returns (lines, total, saved)."""
    factor = Decimal(100 - int(percent)) / Decimal(100)
    out, total, saved = [], 0, 0
    for line in lines:
        unit = int(line["unitPrice"])
        cut = int((Decimal(unit) * factor).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        new = {**line, "listPrice": unit, "unitPrice": cut, "total": cut * int(line["quantity"])}
        out.append(new)
        total += new["total"]
        saved += (unit - cut) * int(line["quantity"])
    return out, total, saved


def kronor(ore):
    kr = Decimal(int(ore)) / 100
    return f"{kr:.0f} kr" if kr == kr.to_integral_value() else f"{kr:.2f} kr".replace(".", ",")


# --- at checkout (payment-intent) and when paid (marketing-sync) ------------------------

class CodeRefused(Exception):
    """The code can't be used: discount_invalid / _expired / _used / _min_order / _other_email."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class CodeHeld(Exception):
    """Another order holds the code. The caller decides from that order's
    state (shared/orders) whether the hold may be taken over."""

    def __init__(self, item):
        super().__init__("discount_held")
        self.item = item


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_email(email):
    """One person, one code: name+tag@x.se is name@x.se, and Gmail ignores dots."""
    local, _, domain = (email or "").strip().lower().partition("@")
    local = local.split("+", 1)[0]
    if domain in ("gmail.com", "googlemail.com"):
        local, domain = local.replace(".", ""), "gmail.com"
    return f"{local}@{domain}"


def owner_key(tenant_id, email):
    """Who a code belongs to: a hash of the canonical address (never the address)."""
    import hashlib
    return hashlib.sha256(f"{tenant_id}|{canonical_email(email)}".encode("utf-8")).hexdigest()[:24]


def reserve_code(table, tenant_id, raw_code, order_id, location_id, email, subtotal_ore, now, takeover_from=None):
    """Holds a code for one order while the guest pays. The code is personal:
    the order's email must be the one it was sent to. If another order holds
    it, CodeHeld is raised (unless `takeover_from` names that order, which the
    caller has made sure can never be paid). Returns the code item (terms)."""
    from botocore.exceptions import ClientError
    code = normalize_code(raw_code)
    item = table.get_item(Key=code_key(tenant_id, code), ConsistentRead=True).get("Item") if code else None
    if not item:
        raise CodeRefused("discount_invalid")
    stamp = _iso(now)
    if item.get("status") == "redeemed":
        raise CodeRefused("discount_used")
    if item.get("expiresAt", "") < stamp:
        raise CodeRefused("discount_expired")
    if item.get("ownerKey") and item["ownerKey"] != owner_key(tenant_id, email):
        raise CodeRefused("discount_other_email")
    if subtotal_ore < int(item.get("minOrderOre") or 0):
        raise CodeRefused("discount_min_order")
    holder = item.get("orderId") if item.get("status") == "reserved" else None
    if holder and holder != order_id and holder != takeover_from:
        raise CodeHeld(item)
    condition = "expiresAt >= :now AND (#st = :issued OR (#st = :reserved AND orderId = :prev))"
    try:
        table.update_item(
            Key=code_key(tenant_id, code),
            UpdateExpression="SET #st = :reserved, orderId = :o, holdLocationId = :loc, reservedAt = :now",
            ConditionExpression=condition,
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":reserved": "reserved", ":issued": "issued", ":o": order_id,
                                       ":prev": holder or "-", ":loc": location_id, ":now": stamp})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise CodeRefused("discount_used") from None
        raise
    return item


def release_code(table, tenant_id, code, order_id):
    """The order can never be paid: the code is free again."""
    from botocore.exceptions import ClientError
    try:
        table.update_item(Key=code_key(tenant_id, code),
                          UpdateExpression="SET #st = :issued REMOVE orderId, holdLocationId, reservedAt",
                          ConditionExpression="#st = :reserved AND orderId = :o",
                          ExpressionAttributeNames={"#st": "status"},
                          ExpressionAttributeValues={":issued": "issued", ":reserved": "reserved", ":o": order_id})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


def redeem_code(table, tenant_id, code, order_id, revenue_ore, discount_ore):
    """The order was paid: the code is used up and counted on the offer, in
    one transaction (a retried stream record can't count it twice or lose
    the counters). Returns "redeemed", "already" (this order, a retry) or
    "other" (another order already used it - should never happen; logged)."""
    from botocore.exceptions import ClientError

    def av(values):
        return values  # the table's own client serializes plain values

    name = table.name
    try:
        table.meta.client.transact_write_items(TransactItems=[
            {"Update": {"TableName": name, "Key": av(code_key(tenant_id, code)),
                        "UpdateExpression": "SET #st = :redeemed, orderId = :o, redeemedAt = :now "
                                            "REMOVE holdLocationId, reservedAt",
                        "ConditionExpression": "attribute_exists(PK) AND #st <> :redeemed",
                        "ExpressionAttributeNames": {"#st": "status"},
                        "ExpressionAttributeValues": av({":redeemed": "redeemed", ":o": order_id,
                                                         ":now": _iso(datetime.now(timezone.utc))})}},
            {"Update": {"TableName": name, "Key": av(offer_key(tenant_id)),
                        "UpdateExpression": "ADD redeemed :one, revenueOre :rev, discountOre :disc",
                        "ExpressionAttributeValues": av({":one": 1, ":rev": int(revenue_ore),
                                                         ":disc": int(discount_ore)})}},
        ])
        return "redeemed"
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
    item = table.get_item(Key=code_key(tenant_id, code), ConsistentRead=True).get("Item") or {}
    return "already" if item.get("orderId") == order_id else "other"

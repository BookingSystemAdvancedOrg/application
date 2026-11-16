"""Gift cards (feature E, ADR-E): shared logic for the gift-cards Lambda.

Table `<prefix>gift-cards` (GIFT_CARDS_TABLE_NAME), partition `TENANT#<t>`:

    SETTINGS                       enabled, amountsSek, customMin/MaxSek, validityMonths, version
    PURCHASE#<purchaseId>          checkout before Stripe confirms (ttl 7 days while pending)
    CARD#<cardId>                  balanceOre, status, expiresAt, contact fields (GSI2 = expiry or cleanup day)
    CODE#<codeHmac>                cardId (uniqueness and lookup)
    CARD#<cardId>#TX#<txId>        append-only balance changes (GSI1 = location + day, for Bokföring)
    TXREF#<txId>                   cardId (find a TX by its id)
    JOB / NIGHTLY                  (PK "JOB") lastDay: resume point of the nightly job

The code is 16 Crockford base32 characters (80 bits), stored only as
HMAC-SHA256 with the pepper secret plus `last4`; it is sent once by email
through notification and never stored, logged or evented. A bearer
instrument: whoever has the code may use it.

VAT: multi-purpose voucher (flerfunktionsvoucher, ML 2023:200 9 kap.):
no VAT at sale, the liability on 2421, breakage to 3990. NEEDS ACCOUNTANT
REVIEW (ADR-E 8).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from shared import dynamo

FEATURE_ID = "gift-cards"
FEATURE_KEY = "giftCards"
GSI1, GSI2 = "byLocationDay", "byExpiry"

STATUSES = ("active", "used", "expired", "void")
TX_KINDS = ("issue", "redeem", "reverse", "expire", "adjust", "refund", "void")
DEFAULT_AMOUNTS = [250, 500, 1000]
CUSTOM_MIN, CUSTOM_MAX = 100, 5000
DEFAULT_VALIDITY, MIN_VALIDITY, MAX_VALIDITY = 24, 12, 60
MESSAGE_MAX = 300
REFUND_DAYS = 14          # distansavtalslagen 2 kap.: withdrawal period of an online sale (lawyer to confirm)
CONTACT_MONTHS = 24       # contact fields removed this long after used/expired/void (BR-NFR-008)
PURCHASE_TTL_DAYS = 7
BOOKKEEPING_YEARS = 7
PAGE = 50

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32
_CROCKFORD = {**{c: c for c in ALPHABET}, "I": "1", "L": "1", "O": "0"}
_ID = re.compile(r"[0-9A-Za-z_-]{8,80}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class GiftCardError(Exception):
    def __init__(self, status: int, code: str, field: Optional[str] = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.field = field


def table():
    return dynamo.table(os.environ["GIFT_CARDS_TABLE_NAME"])


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def local_day(at: datetime, tz_name="Europe/Stockholm") -> str:
    return at.astimezone(ZoneInfo(tz_name or "Europe/Stockholm")).date().isoformat()


def conditional_failed(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [plain(v) for v in value]
    return value


def valid_id(value) -> bool:
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def valid_email(value) -> bool:
    return isinstance(value, str) and len(value) <= 254 and bool(_EMAIL.fullmatch(value.strip()))


def email_hash(tenant_id, email) -> str:
    return hashlib.sha256(f"{tenant_id}|{email.strip().lower()}".encode("utf-8")).hexdigest()


def add_months(at: datetime, months: int) -> datetime:
    month = at.month - 1 + months
    year, month = at.year + month // 12, month % 12 + 1
    for day in (at.day, 30, 29, 28):
        try:
            return at.replace(year=year, month=month, day=day)
        except ValueError:
            continue
    raise ValueError("bad date")


# --- keys --------------------------------------------------------------------------------

def pk(tenant_id) -> str:
    return f"TENANT#{tenant_id}"


def settings_key(tenant_id) -> dict:
    return {"PK": pk(tenant_id), "SK": "SETTINGS"}


def purchase_key(tenant_id, purchase_id) -> dict:
    return {"PK": pk(tenant_id), "SK": f"PURCHASE#{purchase_id}"}


def card_key(tenant_id, card_id) -> dict:
    return {"PK": pk(tenant_id), "SK": f"CARD#{card_id}"}


def code_key(tenant_id, code_hmac) -> dict:
    return {"PK": pk(tenant_id), "SK": f"CODE#{code_hmac}"}


def tx_key(tenant_id, card_id, tx_id) -> dict:
    return {"PK": pk(tenant_id), "SK": f"CARD#{card_id}#TX#{tx_id}"}


def txref_key(tenant_id, tx_id) -> dict:
    return {"PK": pk(tenant_id), "SK": f"TXREF#{tx_id}"}


def card_id_for(purchase_id) -> str:
    """Deterministic: a replayed webhook finds the card it already issued."""
    return f"gc{hashlib.sha256(purchase_id.encode()).hexdigest()[:24]}"


# --- codes -------------------------------------------------------------------------------

_PEPPER = {}


def _pepper() -> bytes:
    if "p" not in _PEPPER:
        raw = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["GIFT_CARD_PEPPER_SECRET_ARN"])
        value = raw.get("SecretString") or ""
        if len(value) < 32:
            raise RuntimeError("gift card pepper is missing or too short")
        _PEPPER["p"] = value.encode("utf-8")
    return _PEPPER["p"]


def reset_pepper() -> None:
    _PEPPER.clear()


def new_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(16))


def normalize_code(raw) -> Optional[str]:
    if not isinstance(raw, str) or len(raw) > 40:
        return None
    chars = [_CROCKFORD.get(c) for c in raw.upper() if c not in " -"]
    if len(chars) != 16 or any(c is None for c in chars):
        return None
    return "".join(chars)


def display_code(code: str) -> str:
    return "-".join(code[i:i + 4] for i in range(0, 16, 4))


def code_hmac(tenant_id, code: str) -> str:
    return hmac.new(_pepper(), f"giftcard|v1|{tenant_id}|{code}".encode("ascii"), hashlib.sha256).hexdigest()


def find_card_by_code(tenant_id, raw) -> Optional[dict]:
    code = normalize_code(raw)
    if not code:
        return None
    ref = table().get_item(Key=code_key(tenant_id, code_hmac(tenant_id, code)), ConsistentRead=True).get("Item")
    return get_card(tenant_id, ref["cardId"]) if ref else None


# --- settings ----------------------------------------------------------------------------

def get_settings(tenant_id) -> dict:
    item = table().get_item(Key=settings_key(tenant_id), ConsistentRead=True).get("Item")
    return item or {"enabled": False, "amountsSek": list(DEFAULT_AMOUNTS), "customMinSek": CUSTOM_MIN,
                    "customMaxSek": CUSTOM_MAX, "validityMonths": DEFAULT_VALIDITY, "version": 0}


def settings_view(item) -> dict:
    return {"enabled": bool(item.get("enabled")), "amountsSek": [int(a) for a in item.get("amountsSek") or []],
            "customMinSek": int(item["customMinSek"]), "customMaxSek": int(item["customMaxSek"]),
            "validityMonths": int(item["validityMonths"]), "version": int(item.get("version") or 0)}


def _whole(value, low, high) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        return None
    if isinstance(value, Decimal) and value != value.to_integral_value():
        return None
    number = int(value)
    return number if low <= number <= high else None


def validate_settings(body) -> dict:
    if not isinstance(body.get("enabled"), bool):
        raise GiftCardError(400, "enabled is required", "enabled")
    amounts = body.get("amountsSek")
    if not isinstance(amounts, list) or not 1 <= len(amounts) <= 6 \
            or any(_whole(a, CUSTOM_MIN, CUSTOM_MAX) is None for a in amounts) or len(set(amounts)) != len(amounts):
        raise GiftCardError(400, "invalid_amount", "amountsSek")
    low, high = _whole(body.get("customMinSek"), CUSTOM_MIN, CUSTOM_MAX), _whole(body.get("customMaxSek"),
                                                                                  CUSTOM_MIN, CUSTOM_MAX)
    if low is None or high is None or low > high:
        raise GiftCardError(400, "invalid_amount", "customMinSek" if low is None else "customMaxSek")
    validity = _whole(body.get("validityMonths"), MIN_VALIDITY, MAX_VALIDITY)
    if validity is None:
        raise GiftCardError(400, "invalid_validity", "validityMonths")
    version = body.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise GiftCardError(400, "version is required", "version")
    return {"enabled": body["enabled"], "amountsSek": sorted(int(a) for a in amounts), "customMinSek": low,
            "customMaxSek": high, "validityMonths": validity, "version": version}


# --- cards and transactions ------------------------------------------------------------------

def get_card(tenant_id, card_id) -> Optional[dict]:
    if not valid_id(card_id):
        return None
    item = table().get_item(Key=card_key(tenant_id, card_id), ConsistentRead=True).get("Item")
    return item if item and item.get("tenantId") == tenant_id else None


def get_tx(tenant_id, card_id, tx_id) -> Optional[dict]:
    return table().get_item(Key=tx_key(tenant_id, card_id, tx_id), ConsistentRead=True).get("Item")


def tx_card(tenant_id, tx_id) -> Optional[str]:
    if not isinstance(tx_id, str) or not tx_id or len(tx_id) > 128 or "#" in tx_id:
        return None
    ref = table().get_item(Key=txref_key(tenant_id, tx_id), ConsistentRead=True).get("Item")
    return ref["cardId"] if ref else None


def transactions(tenant_id, card_id) -> list:
    kwargs = {"KeyConditionExpression": Key("PK").eq(pk(tenant_id)) & Key("SK").begins_with(f"CARD#{card_id}#TX#"),
              "ConsistentRead": True}
    items = []
    while True:
        page = table().query(**kwargs)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return sorted(items, key=lambda t: t.get("at") or "")
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def all_cards(tenant_id) -> list:
    kwargs = {"KeyConditionExpression": Key("PK").eq(pk(tenant_id)) & Key("SK").begins_with("CARD#"),
              "FilterExpression": Attr("codeHmac").exists()}
    items = []
    while True:
        page = table().query(**kwargs)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def shown_status(card, at=None) -> str:
    expires = parse_iso(card.get("expiresAt"))
    if card.get("status") == "active" and expires and expires <= (at or now()):
        return "expired"
    return card.get("status")


def card_summary(card) -> dict:
    return {"cardId": card["cardId"], "last4": card["last4"], "initialSek": int(card["initialOre"]) // 100,
            "balanceSek": int(card["balanceOre"]) // 100, "balanceOre": int(card["balanceOre"]),
            "status": shown_status(card), "expiresAt": card["expiresAt"],
            "soldAtLocationId": card.get("soldAtLocationId"), "purchasedAt": card.get("purchasedAt")}


def card_detail(card) -> dict:
    out = card_summary(card)
    for field in ("recipientName", "message", "buyerEmail", "recipientEmail"):
        if card.get(field):
            out[field] = card[field]
    return out


def tx_view(tx) -> dict:
    return {k: plain(tx[k]) for k in ("txId", "kind", "amountOre", "balanceAfterOre", "channel", "ref", "locationId",
                                       "at", "by", "reason") if tx.get(k) is not None}


def expiry_gsi(tenant_id, card_id, expires_at: str) -> dict:
    return {"GSI2PK": f"EXPIRY#{expires_at[:10]}", "GSI2SK": f"{tenant_id}#{card_id}"}


def cleanup_gsi(tenant_id, card_id, at: datetime) -> dict:
    day = add_months(at, CONTACT_MONTHS).date().isoformat()
    return {"GSI2PK": f"CLEANUP#{day}", "GSI2SK": f"{tenant_id}#{card_id}"}


def tx_item(tenant_id, card, tx_id, kind, amount, balance_after, channel, location_id, at, *, ref=None, by=None,
            reason=None) -> dict:
    day = local_day(at)
    item = {**tx_key(tenant_id, card["cardId"], tx_id), "tenantId": tenant_id, "cardId": card["cardId"], "txId": tx_id,
            "kind": kind, "amountOre": amount, "balanceAfterOre": balance_after, "channel": channel,
            "locationId": location_id, "at": iso(at), "GSI1PK": f"TENANT#{tenant_id}#LOC#{location_id}#{day}",
            "GSI1SK": f"{iso(at)}#{tx_id}"}
    for field, value in (("ref", ref), ("by", by), ("reason", reason)):
        if value:
            item[field] = value
    return item


def change_balance(tenant_id, card_id, tx_id, delta, kind, channel, location_id, *, ref=None, by=None, reason=None,
                   allow_expired=False, attempts=4, checks=None, expect_version=None) -> tuple:
    """One balance change: CARD update (version, status, expiry and balance
    conditions) + the TX row (attribute_not_exists) + TXREF, in one
    transaction. Returns (card, tx, created). A retry with the same txId
    returns the stored TX; another amount is 409 changed_retry. `checks`:
    extra ConditionCheck items in the same transaction (a failed check ends
    in 409 changed_retry after the retries). `expect_version`: only from
    that card version (no re-read loop), so the caller knows the balance the
    change was based on."""
    tbl_name = os.environ["GIFT_CARDS_TABLE_NAME"]
    for _ in range(attempts):
        card = get_card(tenant_id, card_id)
        if not card:
            raise GiftCardError(404, "not found")
        existing = get_tx(tenant_id, card_id, tx_id)
        if existing:
            if int(existing["amountOre"]) != delta:
                raise GiftCardError(409, "changed_retry")
            return card, existing, False
        if expect_version is not None and int(card["version"]) != int(expect_version):
            raise GiftCardError(409, "changed_retry")
        at = now()
        status = shown_status(card, at)
        if status == "void":
            raise GiftCardError(409, "card_void")
        if status == "expired" and not allow_expired:
            raise GiftCardError(409, "card_expired")
        balance = int(card["balanceOre"]) + delta
        if balance < 0:
            raise GiftCardError(409, "insufficient_balance")
        if kind == "expire":
            next_status = "expired"
        elif kind in ("void", "refund"):
            next_status = "void"
        else:
            next_status = "used" if balance == 0 else "active"
        sets = ["balanceOre = :b", "version = version + :one", "#s = :st", "lastTxAt = :at"]
        values = {":b": balance, ":one": 1, ":st": next_status, ":v": int(card["version"]), ":at": iso(at)}
        if next_status == "active":
            gsi = expiry_gsi(tenant_id, card_id, card["expiresAt"])
        else:
            gsi = cleanup_gsi(tenant_id, card_id, at)
        sets += ["GSI2PK = :g2pk", "GSI2SK = :g2sk"]
        values.update({":g2pk": gsi["GSI2PK"], ":g2sk": gsi["GSI2SK"]})
        condition = "version = :v"
        if kind == "redeem":
            condition += " AND expiresAt > :now AND #s = :active"
            values.update({":now": iso(at), ":active": "active"})
        update = {"TableName": tbl_name, "Key": _ser(card_key(tenant_id, card_id)),
                  "UpdateExpression": "SET " + ", ".join(sets),
                  "ConditionExpression": condition, "ExpressionAttributeNames": {"#s": "status"},
                  "ExpressionAttributeValues": _ser(values)}
        tx = tx_item(tenant_id, card, tx_id, kind, delta, balance, channel, location_id, at, ref=ref, by=by,
                     reason=reason)
        txref = {**txref_key(tenant_id, tx_id), "tenantId": tenant_id, "cardId": card_id}
        try:
            dynamo.client().transact_write_items(TransactItems=[
                {"Update": update},
                {"Put": {"TableName": tbl_name, "Item": _ser(tx), "ConditionExpression": "attribute_not_exists(PK)"}},
                {"Put": {"TableName": tbl_name, "Item": _ser(txref)}},
                *(checks or []),
            ])
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in ("TransactionCanceledException",):
                raise
            continue  # a concurrent change: read again (the version moved or the TX now exists)
        card = get_card(tenant_id, card_id)
        return card, tx, True
    raise GiftCardError(409, "changed_retry")


def _ser(item):
    from boto3.dynamodb.types import TypeSerializer
    s = TypeSerializer()
    return {k: s.serialize(v) for k, v in item.items() if v is not None}


# --- cursors -----------------------------------------------------------------------------

def encode_cursor(key) -> str:
    return base64.urlsafe_b64encode(json.dumps(key, sort_keys=True).encode()).decode().rstrip("=")


def decode_cursor(raw, tenant_id) -> dict:
    try:
        key = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode())
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise GiftCardError(400, "cursor is invalid", "cursor")
    if not isinstance(key, dict) or set(key) != {"PK", "SK"} or key.get("PK") != pk(tenant_id):
        raise GiftCardError(400, "cursor is invalid", "cursor")
    return key


def days_between(first: str, last: str):
    day = date.fromisoformat(first)
    end = date.fromisoformat(last)
    while day <= end:
        yield day.isoformat()
        day += timedelta(days=1)

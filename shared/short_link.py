"""Short links for SMS: https://restaurang.<domain>/api/k/<code>.

A long URL in an SMS breaks: phones stop the link at characters like # or %
(a Stripe Checkout URL has a long #fragment full of %2F), so the guest
lands on a broken page. Every link the phone flow texts is therefore a
short one on Booqy's own domain: only letters, digits, "/", "." and "-",
about 45 characters.

    code   10 random base62 characters (secrets, about 59 bits): unguessable
    item   reservation table, PK "LINK#<code>", SK "LINK":
           target (https only), doneUrl (optional), tenantId, locationId,
           reservationId, expiresAt, ttl
    read   GET /k/{code} (get-reservation): redirects only to the stored
           target, never to anything in the request (no open redirect)

The item is written in the same transaction as the booking it points to and
lives no longer than the booking's hold: ttl = expiresAt (DynamoDB removes
it later; the reader checks expiresAt itself).

ENV_VARS:
    SHORT_LINK_BASE   writers: e.g. https://restaurang.booqy.se/api/k (unset:
                      no short link, the long URL is used as before)
    RESERVATION_TABLE_NAME
"""

import os
import re
import secrets
import string
from datetime import datetime, timezone

from shared import dynamo

CODE_LENGTH = 10
_ALPHABET = string.ascii_letters + string.digits
_CODE = re.compile(rf"[A-Za-z0-9]{{{CODE_LENGTH}}}")
# What an SMS app links in full: scheme, host, path of letters, digits, "/", "." and "-".
PLAIN_URL = re.compile(r"https://[A-Za-z0-9.-]+(?:/[A-Za-z0-9/.-]*)?")
SK = "LINK"


def base():
    """SHORT_LINK_BASE without a trailing slash, or None when unset or not plain."""
    value = (os.environ.get("SHORT_LINK_BASE") or "").strip().rstrip("/")
    return value if value and PLAIN_URL.fullmatch(value) else None


def new_code():
    return "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH))


def valid_code(code):
    return isinstance(code, str) and bool(_CODE.fullmatch(code))


def key(code):
    return {"PK": f"LINK#{code}", "SK": SK}


def url(code, link_base=None):
    return f"{link_base or base()}/{code}"


def _https(value):
    return isinstance(value, str) and value.startswith("https://") and len(value) <= 4096


def put_op(code, *, table, target, item, expires, serialize, done_url=None):
    """TransactWriteItems Put of the link to `target` for booking `item`
    (tenantId, locationId, reservationId), valid until `expires`."""
    if not valid_code(code):
        raise ValueError("invalid short link code")
    if not _https(target):
        raise ValueError("short link target must be https")
    expires_at = expires.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = {
        **key(code),
        "target": target,
        "tenantId": item["tenantId"],
        "locationId": item["locationId"],
        "reservationId": item["reservationId"],
        "expiresAt": expires_at,
        "ttl": int(expires.timestamp()),
    }
    if _https(done_url):
        row["doneUrl"] = done_url
    return {"Put": {"TableName": table, "Item": serialize(row),
                    "ConditionExpression": "attribute_not_exists(PK)"}}


def load(code, table):
    """The stored link or None (unknown code, or not a link item)."""
    if not valid_code(code):
        return None
    row = dynamo.table(table).get_item(Key=key(code), ConsistentRead=True).get("Item")
    if not row or not _https(row.get("target")):
        return None
    for name in ("tenantId", "locationId", "reservationId", "expiresAt"):
        if not isinstance(row.get(name), str) or not row[name]:
            return None
    return row


def open_at(row, now):
    """True while the link may still redirect to its target."""
    try:
        expires = datetime.strptime(row["expiresAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return False
    return now < expires

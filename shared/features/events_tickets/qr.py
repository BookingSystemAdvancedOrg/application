"""Ticket QR codes, scan keys and link tokens (ADR-K 5).

    scanKey = HMAC-SHA256(pepper, "scan|v1|<tenant>|<occurrenceId>")      (hex, per occurrence)
    sig     = HMAC-SHA256(scanKey, ticketId)[:16 hex]
    QR      = "<ticketId>.<sig>"

The door scanner downloads the occurrence's scanKey with the guest list, so
a scan verifies offline; the server verifies again on sync. A scanKey is
bound to the tenant and the occurrence, so another restaurant's (or another
evening's) ticket never verifies. Nothing of the code is stored: it is
recomputed from the pepper on the ticket page and verified by HMAC at the
door, so a table dump holds no usable QR (stricter than ADR-K 5's
"HMAC only").

Link tokens (the ticket page and the transfer accept link) are in the URL
fragment on the guest site and in the path of the public routes:

    base64url("<kind>:<tenant>:<id>") + "." + base64url(HMAC(pepper, "link|v1|<kind>|<tenant>|<id>")[:16])

kind `o` = an order (all its tickets the buyer still holds), `t` = one
ticket, `x` = a transfer to accept. Never stored.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from typing import Optional

from shared.features import events_tickets as et

KINDS = ("o", "t", "x")


def scan_key(tenant_id, occ_id) -> str:
    return et.mac(f"scan|v1|{tenant_id}|{occ_id}").hex()


def sig(key_hex: str, ticket_id: str) -> str:
    return hmac.new(bytes.fromhex(key_hex), ticket_id.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def payload(tenant_id, occ_id, ticket_id) -> str:
    return f"{ticket_id}.{sig(scan_key(tenant_id, occ_id), ticket_id)}"


def verify(tenant_id, occ_id, ticket_id, given_sig) -> bool:
    if not isinstance(given_sig, str) or len(given_sig) != 16 or not et.valid_id(ticket_id):
        return False
    return hmac.compare_digest(sig(scan_key(tenant_id, occ_id), ticket_id), given_sig.lower())


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def token(kind: str, tenant_id: str, item_id: str) -> str:
    assert kind in KINDS
    body = _b64(f"{kind}:{tenant_id}:{item_id}".encode("utf-8"))
    return f"{body}.{_b64(et.mac(f'link|v1|{kind}|{tenant_id}|{item_id}')[:16])}"


def read_token(raw) -> Optional[tuple]:
    """(kind, tenantId, id) for a genuine token, else None (any failure looks the same)."""
    if not isinstance(raw, str) or len(raw) > 128 or raw.count(".") != 1:
        return None
    body, mac_part = raw.split(".")
    try:
        decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    parts = decoded.split(":")
    if len(parts) != 3 or parts[0] not in KINDS or not parts[1] or not et.valid_id(parts[2]):
        return None
    if not hmac.compare_digest(token(*parts), raw):
        return None
    return parts[0], parts[1], parts[2]

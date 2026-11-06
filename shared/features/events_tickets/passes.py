"""Apple and Google Wallet passes for tickets (ADR-K 6) behind one provider port.

`EVENTS_WALLET_MODE` picks the adapter (unset = off, so nothing changes until
someone turns it on):

    off        no Wallet: K18 says `wallet {apple: false, google: false}`, K19 is 404
    simulator  dev only (refused, i.e. off, when ENVIRONMENT=prod): both kinds
               available, K19 answers a deterministic JSON stand-in for the
               pass (`pass.json`-like for Apple, a stub save link for Google).
               Nothing is signed and nothing leaves the Lambda.
    live       real passes. Each kind is available only when its secret is set
               and complete, read by the ARN in the named env var (a missing,
               empty or broken secret hides that kind, as ADR-K 6 says):
               EVENTS_TICKETS_APPLE_PASS_SECRET_ARN    JSON {passTypeIdentifier,
                   teamIdentifier, certificatePem, privateKeyPem,
                   privateKeyPassword?, wwdrCertificatePem}
               EVENTS_TICKETS_GOOGLE_WALLET_SECRET_ARN JSON {issuerId,
                   client_email, private_key, classId?} (the service account key
                   file plus the issuer id). The EventTicketClass
                   `<issuerId>.<classId or booqy-event-ticket>` must exist in
                   Google Wallet (created once by hand); the save JWT carries only
                   the ticket object with its long strings capped (title and venue 40, holder 30,
                   ticket type 20 characters, ellipsis when cut), which keeps it under the 1800
                   character limit.

Passes are built on request from the ticket page and never stored. They carry
the event, time, place, ticket type, holder name and the ticket's QR payload
(`<ticketId>.<sig>`, the same the door scanner verifies) and nothing else.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import struct
import time
import zipfile
import zlib
from dataclasses import dataclass
from typing import Optional, Protocol

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

KINDS = ("apple", "google")
MODE_ENV = "EVENTS_WALLET_MODE"
APPLE_SECRET_ENV = "EVENTS_TICKETS_APPLE_PASS_SECRET_ARN"
GOOGLE_SECRET_ENV = "EVENTS_TICKETS_GOOGLE_WALLET_SECRET_ARN"
APPLE_FIELDS = ("passTypeIdentifier", "teamIdentifier", "certificatePem", "privateKeyPem", "wwdrCertificatePem")
GOOGLE_FIELDS = ("issuerId", "client_email", "private_key")
SECRET_CACHE_SECONDS = 300
GOOGLE_SAVE_URL = "https://pay.google.com/gp/v/save/"
PKPASS = "application/vnd.apple.pkpass"


@dataclass(frozen=True)
class PassData:
    """What one ticket's pass shows. `qr` is the scanner payload."""
    tenant_id: str
    event_id: str
    ticket_id: str
    title: str
    start_at: str
    end_at: str
    location_name: str
    type_name: str
    holder_name: str
    qr: str


@dataclass(frozen=True)
class PassResult:
    """A file to download (`body`, `content_type`, `filename`) or a `redirect`."""
    content_type: str = ""
    body: bytes = b""
    filename: Optional[str] = None
    redirect: Optional[str] = None


class WalletProvider(Protocol):
    def available(self) -> dict:
        """{apple: bool, google: bool}"""

    def issue(self, kind: str, data: PassData) -> PassResult:
        """Only called for a kind `available()` said yes to."""


class OffWallet:
    def available(self) -> dict:
        return {"apple": False, "google": False}

    def issue(self, kind, data):
        raise LookupError(kind)


def _pass_json(data: PassData, *, pass_type: str, team: str) -> dict:
    return {
        "formatVersion": 1, "passTypeIdentifier": pass_type, "teamIdentifier": team,
        "serialNumber": data.ticket_id, "organizationName": data.location_name or "Booqy",
        "description": f"Biljett: {data.title}", "relevantDate": data.start_at, "expirationDate": data.end_at,
        "eventTicket": {
            "primaryFields": [{"key": "event", "label": "Evenemang", "value": data.title}],
            "secondaryFields": [{"key": "start", "label": "Tid", "value": data.start_at,
                                 "dateStyle": "PKDateStyleMedium", "timeStyle": "PKDateStyleShort"},
                                {"key": "place", "label": "Plats", "value": data.location_name}],
            "auxiliaryFields": [{"key": "holder", "label": "Namn", "value": data.holder_name},
                                {"key": "type", "label": "Biljett", "value": data.type_name}],
        },
        "barcodes": [{"format": "PKBarcodeFormatQR", "message": data.qr, "messageEncoding": "iso-8859-1",
                      "altText": data.ticket_id[-8:]}],
    }


def _object_id(issuer_id: str, *parts: str) -> str:
    """Google ids are `<issuerId>.<[A-Za-z0-9._-]+>`."""
    return f"{issuer_id}." + "-".join(re.sub(r"[^A-Za-z0-9._-]", "_", p) for p in parts)


GOOGLE_CLASS_NAME = "booqy-event-ticket"


def google_class_id(issuer_id: str, class_name: str = "") -> str:
    """The one EventTicketClass every ticket points at. It is created once by hand in the Google
    Wallet console / API (issuer setup) and never sent inside the save JWT: an inline class pushed
    the live JWT over the 1800 character limit of the save link."""
    return _object_id(issuer_id, class_name or GOOGLE_CLASS_NAME)


TITLE_CAP, LOCATION_CAP, HOLDER_CAP, TYPE_CAP = 40, 40, 30, 20


def _cap(value: str, limit: int) -> str:
    """At most `limit` characters, ending in an ellipsis when cut. Google's save link must stay under
    1800 characters and the app allows far longer names, so what goes into the JWT is shortened."""
    value = (value or "").strip()
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "\u2026"


class _CompactJSONEncoder(json.JSONEncoder):
    """UTF-8 as is: PyJWT would escape å, ä and ö to six characters each (\\u00e5)."""

    def __init__(self, **kwargs):
        kwargs["ensure_ascii"] = False
        super().__init__(**kwargs)


def _google_claims(data: PassData, *, issuer_id: str, client_email: str, origin: str, class_name: str = "") -> dict:
    """Object only, by reference to the class (see google_class_id). Event facts ride on the object's
    text modules because event name, venue and dates are class fields."""
    return {
        "iss": client_email, "aud": "google", "typ": "savetowallet", "origins": [origin] if origin else [],
        "payload": {
            "eventTicketObjects": [{
                "id": _object_id(issuer_id, data.ticket_id), "classId": google_class_id(issuer_id, class_name),
                "state": "ACTIVE", "ticketHolderName": _cap(data.holder_name, HOLDER_CAP),
                "ticketType": {"defaultValue": {"language": "sv", "value": _cap(data.type_name, TYPE_CAP)}},
                "textModulesData": [{"id": "event", "header": _cap(data.title, TITLE_CAP), "body": data.start_at},
                                    {"id": "place", "header": "Plats",
                                     "body": _cap(data.location_name, LOCATION_CAP) or "Booqy"}],
                "barcode": {"type": "QR_CODE", "value": data.qr, "alternateText": data.ticket_id[-8:]},
                "validTimeInterval": {"start": {"date": data.start_at}, "end": {"date": data.end_at}},
            }],
        },
    }


def _origin() -> str:
    url = (os.environ.get("CUSTOMER_SITE_URL") or "").rstrip("/")
    return url if url.startswith("https://") else ""


class SimulatorWallet:
    """Deterministic stand-ins: the same ticket always gives the same bytes."""

    def available(self) -> dict:
        return {"apple": True, "google": True}

    def issue(self, kind, data):
        if kind == "apple":
            payload = {"simulated": True, "kind": "apple",
                       "pass": _pass_json(data, pass_type="pass.se.booqy.simulator", team="SIMULATOR")}
        else:
            claims = _google_claims(data, issuer_id="0000000000000000000", client_email="simulator@booqy.invalid",
                                    origin=_origin())
            digest = hashlib.sha256(json.dumps(claims, sort_keys=True).encode()).hexdigest()[:32]
            payload = {"simulated": True, "kind": "google", "saveUrl": f"{GOOGLE_SAVE_URL}simulated-{digest}",
                       "claims": claims}
        return PassResult(content_type="application/json",
                          body=json.dumps(payload, ensure_ascii=False, sort_keys=True).encode())


_secret_cache: dict = {}


def reset_cache() -> None:
    _secret_cache.clear()


def _secret(env_name: str, fields) -> Optional[dict]:
    """The secret named by `env_name` when it is set and has every field, else None (cached 5 min)."""
    arn = os.environ.get(env_name) or ""
    if not arn:
        return None
    hit = _secret_cache.get(arn)
    if hit and time.monotonic() - hit[1] < SECRET_CACHE_SECONDS:
        return hit[0]
    value = None
    try:
        raw = boto3.client("secretsmanager").get_secret_value(SecretId=arn).get("SecretString") or ""
        parsed = json.loads(raw) if raw.strip() else None
        if isinstance(parsed, dict) and all(isinstance(parsed.get(f), str) and parsed[f] for f in fields):
            value = parsed
    except (BotoCoreError, ClientError, ValueError) as exc:
        logger.warning(json.dumps({"wallet": env_name, "unavailable": type(exc).__name__}))
    _secret_cache[arn] = (value, time.monotonic())
    return value


def _png(size: int = 29) -> bytes:
    """A plain square icon.png (Apple requires one); dark grey, generated so no binary is committed."""
    raw = b"".join(b"\x00" + b"\x2a\x25\x23" * size for _ in range(size))

    def chunk(tag, payload):
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def build_pkpass(data: PassData, secret: dict) -> bytes:
    """A signed .pkpass: pass.json + icon.png + manifest.json (SHA-1 per file) + a detached
    PKCS #7 signature of the manifest by the Pass Type ID certificate, with Apple's WWDR certificate."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.serialization import pkcs7

    files = {
        "pass.json": json.dumps(_pass_json(data, pass_type=secret["passTypeIdentifier"],
                                           team=secret["teamIdentifier"]), ensure_ascii=False).encode(),
        "icon.png": _png(29), "icon@2x.png": _png(58),
    }
    # SHA-1 is what Apple's pass format prescribes for the manifest (the signature itself is SHA-256).
    manifest = json.dumps({name: hashlib.sha1(body, usedforsecurity=False).hexdigest()
                           for name, body in files.items()}, sort_keys=True).encode()
    password = secret.get("privateKeyPassword")
    key = serialization.load_pem_private_key(secret["privateKeyPem"].encode(),
                                             password=password.encode() if password else None)
    signature = (pkcs7.PKCS7SignatureBuilder().set_data(manifest)
                 .add_signer(x509.load_pem_x509_certificate(secret["certificatePem"].encode()), key, hashes.SHA256())
                 .add_certificate(x509.load_pem_x509_certificate(secret["wwdrCertificatePem"].encode()))
                 .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.DetachedSignature,
                                                    pkcs7.PKCS7Options.Binary]))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in {**files, "manifest.json": manifest, "signature": signature}.items():
            archive.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), body)
    return out.getvalue()


def google_save_url(data: PassData, secret: dict) -> str:
    import jwt

    claims = _google_claims(data, issuer_id=secret["issuerId"], client_email=secret["client_email"],
                            origin=_origin(), class_name=secret.get("classId") or "")
    claims["iat"] = int(time.time())
    return GOOGLE_SAVE_URL + jwt.encode(claims, secret["private_key"], algorithm="RS256",
                                         json_encoder=_CompactJSONEncoder)


class LiveWallet:
    def available(self) -> dict:
        return {"apple": _secret(APPLE_SECRET_ENV, APPLE_FIELDS) is not None,
                "google": _secret(GOOGLE_SECRET_ENV, GOOGLE_FIELDS) is not None}

    def issue(self, kind, data):
        if kind == "apple":
            secret = _secret(APPLE_SECRET_ENV, APPLE_FIELDS)
            if secret is None:  # gone from the cache and unreadable now
                raise LookupError(kind)
            return PassResult(content_type=PKPASS, body=build_pkpass(data, secret),
                              filename=f"biljett-{data.ticket_id}.pkpass")
        secret = _secret(GOOGLE_SECRET_ENV, GOOGLE_FIELDS)
        if secret is None:
            raise LookupError(kind)
        return PassResult(redirect=google_save_url(data, secret))


def mode() -> str:
    value = (os.environ.get(MODE_ENV) or "off").strip().lower()
    if value == "simulator" and os.environ.get("ENVIRONMENT") == "prod":
        logger.warning(json.dumps({"wallet": "simulator refused in prod"}))
        return "off"
    return value if value in ("off", "simulator", "live") else "off"


def provider() -> WalletProvider:
    return {"simulator": SimulatorWallet, "live": LiveWallet}.get(mode(), OffWallet)()


def response(result: PassResult) -> dict:
    """The API Gateway answer for a pass: never cached (it is bound to a ticket link)."""
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if result.redirect:
        return {"statusCode": 302, "headers": {**headers, "Location": result.redirect}, "body": ""}
    if result.filename:
        headers["Content-Disposition"] = f'attachment; filename="{result.filename}"'
    return {"statusCode": 200, "headers": {**headers, "Content-Type": result.content_type},
            "body": base64.b64encode(result.body).decode(), "isBase64Encoded": True}

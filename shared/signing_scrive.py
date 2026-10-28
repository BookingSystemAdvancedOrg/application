"""Scrive Document API v2 behind shared/signing.py (provider "scrive";
docs/orchestrator/CATERING-SIGNING-HANDOFF.md, "If the choice is Scrive").

One Scrive document per offer version. The offer PDF is uploaded as is
(its SHA-256 is what both parties sign), with three parties: the API user
(the author, a viewer that never signs), the restaurant (sign_order 1) and
the customer (sign_order 2). Both sign with Swedish BankID (se_bankid) on
Scrive's own signing page: delivery_method "api" gives each signatory a
private link (api_delivery_url), which start() reads again from Scrive
every time and never stores. Scrive sends no e-mails of its own; Booqy
tells each party (shared/catering_messages.py).

Callbacks (api_callback_url) are not signed by Scrive: the URL carries the
order's random token (`t`, compared in constant time by signing.py), the
body is never trusted and the webhook always re-reads the document.

The secret (SIGNING_PROVIDER_SECRET_ARN):

    {"provider": "scrive", "baseUrl": "https://api-testbed.scrive.com",
     "apiToken", "apiSecret", "accessToken", "accessSecret"}

    The four values are Scrive's personal access credentials (OAuth 1.0
    PLAINTEXT: client credentials identifier/secret, token credentials
    identifier/secret). In prod the testbed is refused.

Errors: Scrive unreachable, 5xx, 401/403 (credentials) -> SigningUnavailable
(503, alarm); any other 4xx -> SigningRejected(<error_type>) (409).
"""

import hashlib
import json
import logging
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from shared.bankid import SigningRejected, SigningUnavailable

logger = logging.getLogger()

_TIMEOUT = 12
_MAX_DAYS_TO_SIGN = 90
METHOD = "scrive_se_bankid"
CLOSED = "closed"
_DEAD = ("canceled", "timedout", "rejected", "document_error")
STOCKHOLM = ZoneInfo("Europe/Stockholm")


class EvidenceNotReady(SigningUnavailable):
    """The document is signed but Scrive has not sealed the PDF yet."""


def check_config(config, environment):
    base = str(config.get("baseUrl") or "")
    host = urlparse(base).hostname or ""
    if urlparse(base).scheme != "https" or not (host == "scrive.com" or host.endswith(".scrive.com")):
        raise SigningUnavailable("scrive baseUrl must be https://*.scrive.com")
    if environment == "prod" and "testbed" in host:
        raise SigningUnavailable("the Scrive testbed is not for prod")
    if not all(isinstance(config.get(k), str) and config[k] for k in
               ("apiToken", "apiSecret", "accessToken", "accessSecret")):
        raise SigningUnavailable("scrive credentials are missing")


def _auth(config):
    return ("OAuth oauth_signature_method=\"PLAINTEXT\", "
            f"oauth_consumer_key=\"{config['apiToken']}\", oauth_token=\"{config['accessToken']}\", "
            f"oauth_signature=\"{config['apiSecret']}&{config['accessSecret']}\"")


def _request(config, method, path, data=None, content_type=None, raw=False):
    url = f"{config['baseUrl'].rstrip('/')}/api/v2/{path}"
    headers = {"Authorization": _auth(config), "Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read() or b"{}")
        except ValueError:
            error = {}
        code = str(error.get("error_type") or f"http_{exc.code}")[:64]
        logger.warning(json.dumps({"scrive": "error", "path": path.split("/")[-1], "status": exc.code,
                                   "errorType": code}))
        if exc.code >= 500 or exc.code in (401, 403, 429):
            raise SigningUnavailable(code) from None
        raise SigningRejected(code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SigningUnavailable(f"scrive unreachable: {type(exc).__name__}") from None
    if raw:
        return body
    try:
        return json.loads(body or b"{}")
    except ValueError:
        raise SigningUnavailable("scrive answered something that is not JSON") from None


def _form(fields):
    return urllib.parse.urlencode(fields).encode("utf-8"), "application/x-www-form-urlencoded"


def _multipart(filename, pdf_bytes):
    boundary = f"booqy{secrets.token_hex(12)}"
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"saved\"\r\n\r\nfalse\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            "Content-Type: application/pdf\r\n\r\n").encode("utf-8")
    return head + pdf_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8"), f"multipart/form-data; boundary={boundary}"


def _get(config, document_id):
    return _request(config, "GET", f"documents/{document_id}/get")


def _days_to_sign(valid_until, today=None):
    """Scrive times the document out at the end of the last day (in the
    document's timezone), so the offer's last valid day is the last day."""
    try:
        days = (date.fromisoformat(str(valid_until)) - (today or datetime.now(STOCKHOLM).date())).days
    except ValueError:
        days = 14
    return max(1, min(_MAX_DAYS_TO_SIGN, days))


def _field(kind, value, order=None):
    field = {"type": kind, "value": value or "", "is_obligatory": False, "should_be_filled_by_sender": False}
    return {**field, "order": order} if order else field


def _signatory(party, sign_order):
    fields = [_field("name", party.get("name") if party["role"] == "customer" else "", 1),
              _field("email", party.get("email"))]
    company = party.get("company") or (party.get("name") if party["role"] == "restaurant" else None)
    if company:
        fields.append(_field("company", company))
    signatory = {"signatory_role": "signing_party", "sign_order": sign_order, "fields": fields,
                 "delivery_method": "api", "confirmation_delivery_method": "none",
                 "notification_delivery_method": "none",
                 "authentication_method_to_view": "standard", "authentication_method_to_sign": "se_bankid"}
    if party.get("returnUrl"):
        signatory["sign_success_redirect_url"] = party["returnUrl"]
        signatory["reject_redirect_url"] = party["returnUrl"]
    return signatory


def _author(document):
    author = next((p for p in document.get("parties") or [] if p.get("is_author")), None)
    if not author:
        raise SigningUnavailable("scrive document has no author party")
    return {"id": author["id"], "signatory_role": "viewer", "fields": author.get("fields") or [],
            "delivery_method": "api", "confirmation_delivery_method": "none",
            "notification_delivery_method": "none"}


def create_order(config, order, offer, pdf_bytes, parties):
    """new (upload) -> update (parties, callback, days) -> start; on a failure
    after the upload the draft is trashed. -> the order with documentId and
    the Scrive party id of each role."""
    title = f"Offert catering v{int(offer['version'])}"
    body, content_type = _multipart(f"offert-v{int(offer['version']):04d}.pdf", pdf_bytes)
    document = _request(config, "POST", "documents/new", body, content_type)
    document_id = str(document["id"])
    try:
        signatories = [_signatory(p, n) for n, p in enumerate(parties, start=1)]
        update = {"title": title, "lang": "sv", "timezone": "Europe/Stockholm",
                  "days_to_sign": _days_to_sign(offer.get("validUntil")),
                  "days_to_remind": None, "api_callback_url": order.get("callbackUrl"),
                  "display_options": {"show_reject_option": False, "allow_reject_reason": False,
                                      "show_pdf_download": True},
                  "parties": [_author(document), *signatories]}
        _request(config, "POST", f"documents/{document_id}/update", *_form({"document": json.dumps(update)}))
        started = _request(config, "POST", f"documents/{document_id}/start", *_form({}))
    except (SigningRejected, SigningUnavailable, KeyError):
        _trash(config, document_id)
        raise
    party_ids = _party_ids(started, parties)
    return {**order, "documentId": document_id, "partyIds": party_ids}


def _party_ids(document, parties):
    signers = sorted((p for p in document.get("parties") or [] if not p.get("is_author")
                      and p.get("signatory_role", "signing_party") == "signing_party"),
                     key=lambda p: int(p.get("sign_order") or 0))
    if len(signers) != len(parties):
        raise SigningUnavailable("scrive did not keep the parties")
    return {party["role"]: str(signer["id"]) for party, signer in zip(parties, signers)}


def _trash(config, document_id):
    try:
        _request(config, "POST", f"documents/{document_id}/trash", *_form({}))
    except (SigningRejected, SigningUnavailable):
        logger.warning(json.dumps({"scrive": "trash_failed", "documentId": document_id}))


def _scrive_party(document, order, role):
    party_id = (order.get("partyIds") or {}).get(role)
    party = next((p for p in document.get("parties") or [] if str(p.get("id")) == party_id), None)
    if not party:
        raise SigningUnavailable("scrive party is missing")
    return party


def start(config, order, role):
    document = _get(config, order["documentId"])
    if document.get("status") != "pending":
        raise SigningRejected(f"document_{document.get('status')}")
    url = _scrive_party(document, order, role).get("api_delivery_url")
    if not url:
        raise SigningUnavailable("scrive gave no signing link")
    if not url.startswith("https://"):
        url = f"{config['baseUrl'].rstrip('/')}/{url.lstrip('/')}"
    sessions = {**(order.get("sessions") or {}), role: {"kind": "redirect", "startedAt": int(time.time())}}
    return {"kind": "redirect", "url": url}, {**order, "sessions": sessions}


def _value(party, kind):
    return next((f.get("value") for f in party.get("fields") or [] if f.get("type") == kind and f.get("value")), None)


def _mask(personal_number):
    digits = "".join(ch for ch in str(personal_number or "") if ch.isdigit())
    return f"{digits[:8]}-****" if len(digits) >= 10 else None


def collect(config, order, role):
    document = _get(config, order["documentId"])
    party = _scrive_party(document, order, role)
    sessions = order.get("sessions") or {}
    if party.get("sign_time"):
        if (sessions.get(role) or {}).get("signedAt"):
            return {"state": "signed", "signer": sessions[role].get("signer")}, order
        signer = {k: v for k, v in {"name": _value(party, "name") or _value(party, "company"),
                                    "personalNumberMasked": _mask(_value(party, "personal_number"))}.items() if v}
        evidence = {f"{role}-scrive.json": json.dumps({
            "provider": "scrive", "documentId": order["documentId"], "partyId": party.get("id"), "role": role,
            "pdfSha256": order["pdfSha256"], "documentStatus": document.get("status"),
            "signTime": party.get("sign_time"), "authenticationToSign": party.get("authentication_method_to_sign"),
            "fields": [{"type": f.get("type"), "value": f.get("value")} for f in party.get("fields") or []],
        }, ensure_ascii=False).encode("utf-8")}
        session = {**(sessions.get(role) or {"kind": "redirect"}), "signedAt": int(time.time()), "method": METHOD,
                   "signer": signer}
        return {"state": "signed", "signer": signer, "evidence": evidence}, \
            {**order, "sessions": {**sessions, role: session}}
    if document.get("status") in _DEAD or party.get("rejected_time"):
        return {"state": "failed", "hint": "rejected" if party.get("rejected_time") else document.get("status")}, order
    return {"state": "pending", "hint": "outstandingTransaction"}, order


def status(config, order):
    document = _get(config, order["documentId"])
    parties = []
    for p in order.get("parties") or []:
        party = _scrive_party(document, order, p["role"])
        parties.append({"role": p["role"], "signedAt": party.get("sign_time"),
                        "method": METHOD if party.get("sign_time") else None})
    return {"complete": document.get("status") == CLOSED, "parties": parties, "documentStatus": document.get("status")}


def cancel(config, order):
    try:
        _request(config, "POST", f"documents/{order['documentId']}/cancel", *_form({}))
    except (SigningRejected, SigningUnavailable) as exc:
        logger.warning(json.dumps({"scrive": "cancel_failed", "documentId": order["documentId"],
                                   "error": str(exc)[:64]}))


def fetch_evidence(config, order):
    """The sealed PDF (Scrive's evidence attachments are inside it). Raises
    EvidenceNotReady while the document is closed but not yet sealed."""
    document = _get(config, order["documentId"])
    sealed = document.get("sealed_file")
    if document.get("status") != CLOSED or not sealed:
        raise EvidenceNotReady("scrive has not sealed the document yet")
    name = urllib.parse.quote(str(sealed.get("name") or "sealed.pdf"))
    pdf = _request(config, "GET", f"documents/{order['documentId']}/files/main/{name}", raw=True)
    if not pdf.startswith(b"%PDF"):
        raise SigningUnavailable("scrive sealed file is not a PDF")
    return {"scrive-sealed.pdf": pdf}


def callback_event_id(order, raw_body):
    """The callback body is a form (document_id, document_json). Never
    trusted: only used to refuse a callback about another document and to
    name the event for dedupe. -> event id | None (reject)."""
    fields = urllib.parse.parse_qs(raw_body or "", keep_blank_values=True)
    claimed = (fields.get("document_id") or fields.get("documentid") or [None])[0]
    if claimed and str(claimed) != str(order.get("documentId")):
        return None
    if not raw_body:  # nothing to tell callbacks apart by: handle each one (re-fetch is idempotent)
        return f"scrive-{order.get('documentId')}-{time.time_ns()}"
    return "scrive-" + hashlib.sha256(f"{order.get('documentId')}|{raw_body}".encode("utf-8")).hexdigest()

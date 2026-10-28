"""Catering signing: one interface, one implementation per provider
(docs/orchestrator/CATERING-SIGNING-HANDOFF.md).

The provider is the `provider` field of the signing-provider secret
(SIGNING_PROVIDER_SECRET_ARN, shared/bankid.py loads it):

    simulator      dev only: BankID-like, completes on the second status poll,
                   signed by "Test Testsson". {"startKind": "redirect"} in the
                   secret makes it behave like a provider with its own signing
                   page instead (start answers a URL, the return completes it).
    bankid_direct  BankID's RP API v6 (shared/bankid.py), the same certificate
                   for every restaurant. "bankid" is the older name.
    scrive         Scrive Document API v2 (shared/signing_scrive.py): one
                   document per offer version, both parties sign with BankID
                   on Scrive's page (start answers a redirect), callbacks
                   carry the order's token, the sealed PDF is the evidence.

A signing order is a plain dict the caller stores on the request
(`signing.order`) and passes back in; the functions return what changed
in it. Parties sign in order: the restaurant, then the customer.

    create_order(request, offer, pdf_bytes, parties, callback_url) -> order
        parties: [{"role": "restaurant" | "customer", "name", "email", "company"?, "returnUrl"?}]
        returnUrl: where a provider's own signing page sends the party back
        (Scrive fixes it per party when the document is created)
    start(order, role, end_user_ip, return_url) -> (result, order)
        return_url: where a provider's own signing page sends the party back
        result {"kind": "bankid", "autoStartToken", "qrData"} | {"kind": "redirect", "url"}
    collect(order, role) -> (result, order)
        result {"state": "pending" | "signed" | "failed", "hint"?, "signer"?, "evidence"?}
        signer {"name", "personalNumberMasked"}; evidence {filename: bytes}
    qr(order, role) -> the current BankID QR string (kind "bankid" only)
    cancel(order) -> None (best effort)
    status(order) -> {"complete", "parties": [{"role", "signedAt"?, "method"}]}
    fetch_evidence(order) -> {filename: bytes} for the whole order (Scrive: the
        sealed PDF; raises EvidenceNotReady until it is sealed). None for the
        simulator and direct BankID: they hand evidence over per signature in collect
    verify_webhook(order, headers, raw_body, query) -> {"eventId"} | None (reject)

A provider with webhooks puts a random per-order token in the callback URL
(`callbackToken`, compared in constant time); the webhook then always
re-reads the state with status() and never trusts the callback body.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time

from shared import bankid
from shared import signing_scrive as scrive
from shared.bankid import SigningRejected, SigningUnavailable  # noqa: F401 - re-exported
from shared.signing_scrive import EvidenceNotReady  # noqa: F401 - re-exported

ROLES = ("restaurant", "customer")
_ROLE_TEXT = {"restaurant": "Restaurangen godkänner offerten.", "customer": "Jag godkänner offerten och villkoren."}


def provider():
    name = bankid._config()["provider"]
    return "bankid_direct" if name == "bankid" else name


def _config():
    return bankid._config()


def _now():
    return int(time.time())


def create_order(request, offer, pdf_bytes, parties, callback_url=None):
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    token = secrets.token_urlsafe(24)
    order = {
        "provider": provider(),
        "ref": f"{request['requestId']}-v{int(offer['version']):04d}-{token[:8]}",
        "pdfSha256": sha,
        "summary": offer.get("summary") or "",
        "parties": [{k: p.get(k) for k in ("role", "name", "email")} for p in parties],
        "callbackToken": token,
        "callbackUrl": f"{callback_url}&t={token}" if callback_url and "?" in callback_url
        else (f"{callback_url}?t={token}" if callback_url else None),
        "sessions": {},
        "createdAt": _now(),
    }
    if order["provider"] == "scrive":
        return scrive.create_order(_config(), order, offer, pdf_bytes, parties)
    return order


def _party(order, role):
    if role not in ROLES:
        raise ValueError("unknown signing role")
    return next((p for p in order["parties"] if p["role"] == role), {})


def _visible_text(order, role):
    return f"{order['summary']}\n\nDokument-ID (SHA-256): {order['pdfSha256'][:32]}\n\n{_ROLE_TEXT[role]}"


def start(order, role, end_user_ip, return_url=None):
    _party(order, role)
    sessions = dict(order.get("sessions") or {})
    if (sessions.get(role) or {}).get("signedAt"):
        raise SigningRejected("already_signed")
    if order["provider"] == "scrive":
        return scrive.start(_config(), order, role)
    if order["provider"] == "simulator":
        if _config().get("startKind") == "redirect":
            sessions[role] = {"kind": "redirect", "startedAt": _now()}
            return {"kind": "redirect", "url": return_url or ""}, {**order, "sessions": sessions}
        ref = hashlib.sha256(f"{order['ref']}{role}{time.time_ns()}".encode()).hexdigest()[:32]
        session = {"kind": "bankid", "orderRef": f"sim-{ref}", "autoStartToken": f"sim-{ref}",
                   "qrStartToken": f"sim-{ref}", "qrStartSecret": "simulator", "startedAt": _now(), "polls": 0}
    else:
        old = sessions.get(role)
        if old and old.get("orderRef"):
            bankid.cancel(old)
        session = {"kind": "bankid", **bankid.start(end_user_ip, _visible_text(order, role), order["pdfSha256"])}
    sessions[role] = session
    result = {"kind": "bankid", "autoStartToken": session["autoStartToken"], "qrData": bankid.qr_data(session)}
    return result, {**order, "sessions": sessions}


def qr(order, role):
    session = (order.get("sessions") or {}).get(role) or {}
    return bankid.qr_data(session) if session.get("kind") == "bankid" else None


def _signed(order, role, session, signer, evidence, method):
    sessions = {**order["sessions"], role: {**session, "signedAt": _now(), "method": method,
                                           "signer": signer}}
    return {"state": "signed", "signer": signer, "evidence": evidence}, {**order, "sessions": sessions}


def _evidence_file(order, role, completion):
    body = {"orderRef": order["ref"], "role": role, "pdfSha256": order["pdfSha256"], "provider": order["provider"],
            "visibleText": _visible_text(order, role), **completion}
    return {f"{role}-bankid.json": json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")}


def collect(order, role):
    if order.get("provider") == "scrive":  # the state is Scrive's, with or without a started session
        _party(order, role)
        return scrive.collect(_config(), order, role)
    sessions = order.get("sessions") or {}
    session = sessions.get(role)
    if not session:
        return {"state": "failed", "hint": "no_session"}, order
    if session.get("signedAt"):
        return {"state": "signed", "signer": session.get("signer")}, order
    if order["provider"] == "simulator":
        signer = {"name": "Test Testsson", "personalNumberMasked": "19000000-****"}
        completion = {"user": {"name": "Test Testsson", "personalNumber": "190000000000"},
                      "signature": base64.b64encode(b"<simulated/>").decode(), "ocspResponse": "", "simulated": True}
        if session["kind"] == "redirect":
            return _signed(order, role, session, signer, _evidence_file(order, role, completion), "simulator")
        if int(session.get("polls") or 0) < 1:
            pending = {**session, "polls": int(session.get("polls") or 0) + 1}
            return {"state": "pending", "hint": "outstandingTransaction"}, {**order, "sessions": {**sessions, role: pending}}
        return _signed(order, role, session, signer, _evidence_file(order, role, completion), "simulator")
    result = bankid.collect(session)
    if result["status"] == "pending":
        return {"state": "pending", "hint": result.get("hintCode")}, order
    if result["status"] != "complete":
        rest = {k: v for k, v in sessions.items() if k != role}
        return {"state": "failed", "hint": result.get("hintCode")}, {**order, "sessions": rest}
    done = result["completion"]
    signer = {"name": done.get("name"), "personalNumberMasked": bankid.mask_personal_number(done.get("personalNumber"))}
    completion = {"user": {"name": done.get("name"), "personalNumber": done.get("personalNumber")},
                  **{k: done.get(k) for k in ("signature", "ocspResponse", "bankIdIssueDate", "stepUp", "device")}}
    return _signed(order, role, session, signer, _evidence_file(order, role, completion), "bankid")


def cancel(order):
    if not order or order.get("provider") == "simulator":
        return
    if order.get("provider") == "scrive":
        if order.get("documentId"):
            scrive.cancel(_config(), order)
        return
    for session in (order.get("sessions") or {}).values():
        if session.get("orderRef") and not session.get("signedAt"):
            bankid.cancel(session)


def status(order):
    if order.get("provider") == "scrive":
        return scrive.status(_config(), order)
    parties = []
    for p in order.get("parties") or []:
        session = (order.get("sessions") or {}).get(p["role"]) or {}
        parties.append({"role": p["role"], "signedAt": session.get("signedAt"), "method": session.get("method")})
    return {"complete": bool(parties) and all(p["signedAt"] for p in parties), "parties": parties}


def fetch_evidence(order):
    if (order or {}).get("provider") == "scrive":
        return scrive.fetch_evidence(_config(), order)
    return {}


def verify_webhook(order, headers, raw_body, query):
    """The order's own random token (`t`) in constant time, for every
    provider (Scrive does not sign its callbacks). Neither the simulator nor
    direct BankID calls back; accepting their token lets dev exercise the
    webhook path. The body is never trusted, only used to name the event."""
    token = (query or {}).get("t")
    expected = (order or {}).get("callbackToken")
    if not isinstance(token, str) or not expected or not hmac.compare_digest(token, expected):
        return None
    if order.get("provider") == "scrive":
        event_id = scrive.callback_event_id(order, raw_body)
        return {"eventId": event_id} if event_id else None
    try:
        event_id = json.loads(raw_body or "{}").get("eventId")
    except ValueError:
        event_id = None
    if not isinstance(event_id, str) or not event_id or len(event_id) > 128:
        event_id = hashlib.sha256((raw_body or "").encode("utf-8")).hexdigest()
    return {"eventId": event_id}

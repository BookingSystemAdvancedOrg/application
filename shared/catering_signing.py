"""Both-party signing of a catering offer version (issue 5, C2/C8), shared
by catering-offer (the restaurant), catering-customer (the customer) and
catering-signing-webhook. The provider is behind shared/signing.py.

The head item carries `signing` for the offer version being signed:

    {"offerVersion", "pdfKey", "pdfSha256", "order": <signing order>,
     "signatures": {"restaurant"|"customer": {"name", "personalNumberMasked"?,
                    "role"?, "method", "signedAt", "evidenceKey"}}}

The restaurant signs first; only then is the customer told (notice
catering_offer_sent) and may sign. When the customer has signed (BankID or,
where the restaurant allows it, simple acceptance), the request is `signed`
and the evidence package is written under <tenant>/signed/<requestId>/vNNNN/:
the offer PDF, each signature's evidence and a manifest of SHA-256 hashes,
every object with its own Object Lock retention (DOCUMENT_LOCK_MODE /
DOCUMENT_LOCK_DAYS; the bucket has no default lock).
"""

import hashlib
import json
import os
from datetime import timedelta

import boto3

from shared import catering as c
from shared import signing

RESTAURANT = "restaurant"
CUSTOMER = "customer"


class NotReady(Exception):
    """The order can't be signed by this party now (409 with the code)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _s3():
    return boto3.client("s3")


def bucket():
    return os.environ["CATERING_DOCUMENTS_BUCKET_NAME"]


def put_locked(key, body, content_type):
    """Writes an evidence object with its own retention (C7)."""
    extra = {}
    mode, days = os.environ.get("DOCUMENT_LOCK_MODE"), os.environ.get("DOCUMENT_LOCK_DAYS")
    if mode and days:
        extra = {"ObjectLockMode": mode, "ObjectLockRetainUntilDate": c.now_utc() + timedelta(days=int(days))}
    _s3().put_object(Bucket=bucket(), Key=key, Body=body, ContentType=content_type, ChecksumAlgorithm="SHA256",
                     **extra)
    return key


def signed_prefix(item, version):
    return f"{item['tenantId']}/signed/{item['requestId']}/v{int(version):04d}/"


def _current(item):
    """The signing record of the current offer version, or NotReady."""
    s = item.get("signing") or {}
    if item.get("status") != c.OFFER_SENT or not s.get("order") \
            or int(s.get("offerVersion") or 0) != int(item.get("currentOfferVersion") or 0):
        raise NotReady("not_signable")
    return s


def restaurant_signed(item):
    return bool(((item.get("signing") or {}).get("signatures") or {}).get(RESTAURANT))


def _order_update(item, s, order):
    """Saves the provider's order state (sessions) without bumping the head
    version, so polling never races the owner or the guest."""
    c.dynamo.table(c.requests_table()).update_item(
        Key=c.request_key(item["locationId"], item["requestId"]),
        UpdateExpression="SET signing.#o = :o",
        ConditionExpression="signing.offerVersion = :v",
        ExpressionAttributeNames={"#o": "order"},
        ExpressionAttributeValues={":o": order, ":v": int(s["offerVersion"])})


def begin(item, role, ip, return_url=None):
    s = _current(item)
    if role == CUSTOMER and not restaurant_signed(item):
        raise NotReady("awaiting_restaurant")
    if (s.get("signatures") or {}).get(role):
        raise NotReady("already_signed")
    result, order = signing.start(s["order"], role, ip, return_url)
    _order_update(item, s, order)
    now = c.now_utc()
    c.transact([c.audit_put(item["requestId"], f"signing_started_{role}", role, now,
                            version=int(s["offerVersion"]), ip=ip, provider=order["provider"])])
    return result


def poll(item, role, ip):
    """-> {"state": "pending", "hint", "qrData"} | {"state": "failed", "hint"} | {"state": "signed"}"""
    s = _current(item)
    if (s.get("signatures") or {}).get(role):
        return {"state": "signed"}
    if not ((s["order"].get("sessions") or {}).get(role)):
        raise NotReady("no_signing_started")  # e.g. a new version replaced the order being signed
    result, order = signing.collect(s["order"], role)
    if result["state"] == "pending":
        if order is not s["order"]:
            _order_update(item, s, order)
        return {"state": "pending", "hint": result.get("hint"), "qrData": signing.qr(order, role)}
    if result["state"] == "failed":
        _order_update(item, s, order)
        c.transact([c.audit_put(item["requestId"], f"signing_failed_{role}", role, c.now_utc(),
                                hint=result.get("hint"))])
        return {"state": "failed", "hint": result.get("hint")}
    record(item, role, {**result["signer"], "method": order["sessions"][role].get("method") or "bankid"},
           result.get("evidence") or {}, ip, order)
    return {"state": "signed"}


def accept(item, signer, ip, user_agent):
    """Simple acceptance by the customer (C8): name (+ role and authority for
    a company), the exact PDF hash, time, IP and user agent."""
    s = _current(item)
    if not restaurant_signed(item):
        raise NotReady("awaiting_restaurant")
    now = c.now_utc()
    evidence = {"customer-acceptance.json": json.dumps({
        "requestId": item["requestId"], "offerVersion": int(s["offerVersion"]), "pdfSha256": s["pdfSha256"],
        "acceptedAt": c.iso(now), "method": "simple_acceptance", "signer": signer, "ip": ip,
        "userAgent": (user_agent or "")[:300],
        "text": "Jag godkänner offerten och villkoren och att detta är ett bindande avtal.",
    }, ensure_ascii=False).encode("utf-8")}
    signing.cancel(s["order"])  # the provider's customer step is no longer needed
    record(item, CUSTOMER, {**signer, "method": "simple_acceptance"}, evidence, ip, s["order"])


def record(item, role, signer, evidence, ip, order):
    """Stores one party's signature: evidence first (locked), then the head
    item conditionally on status + version. The customer's completes it."""
    s = item["signing"]
    version = int(s["offerVersion"])
    prefix = signed_prefix(item, version)
    keys = [put_locked(prefix + name, body, "application/json") for name, body in evidence.items()]
    now = c.now_utc()
    entry = {k: v for k, v in {**signer, "signedAt": c.iso(now), "evidenceKey": keys[0] if keys else None}.items()
             if v is not None}
    signatures = {**(s.get("signatures") or {}), role: entry}
    sets = ["signing.signatures = :sigs", "signing.#o = :o", "updatedAt = :now"]
    values = {":sigs": signatures, ":o": order, ":now": c.iso(now)}
    names = {"#o": "order"}
    audit = c.audit_put(item["requestId"], "signed_restaurant" if role == RESTAURANT else "signed", role, now,
                        version=version, pdfSha256=s["pdfSha256"], ip=ip, method=signer.get("method"),
                        evidence=keys[0] if keys else None)
    if role == RESTAURANT:
        sets += ["restaurantSignedAt = :now", "notice = :n"]
        values[":n"] = c.notice(c.N_OFFER_SENT, now, version=version)
        c.transact([c.head_update(item, sets=sets, values=values, names=names), audit])
        return
    signed_key = _finalize(item, s, signatures, prefix)
    signature = {**{k: entry.get(k) for k in ("name", "personalNumberMasked", "role", "method", "evidenceKey")},
                 "signedAt": entry["signedAt"], "documentHash": (item.get("offer") or {}).get("documentHash"),
                 "pdfSha256": s["pdfSha256"], "provider": order.get("provider"), "signedPdfKey": signed_key}
    sets += ["signature = :sig", "signedAt = :now", "signedOfferVersion = :v", "notice = :n", "signedPdfKey = :pdf"]
    values.update({":sig": {k: v for k, v in signature.items() if v is not None}, ":v": version,
                   ":n": c.notice(c.N_SIGNED, now), ":pdf": signed_key})
    c.transact([c.head_update(item, to_status=c.SIGNED, sets=sets, values=values, names=names), audit])


def _finalize(item, s, signatures, prefix):
    """The evidence package: the signed PDF (a locked copy of the offer the
    parties signed), the provider's own evidence, and a manifest of hashes."""
    pdf = _s3().get_object(Bucket=bucket(), Key=s["pdfKey"])["Body"].read()
    if hashlib.sha256(pdf).hexdigest() != s["pdfSha256"]:
        raise RuntimeError("offer PDF does not match the signed hash")
    files = {"offer.pdf": pdf, **signing.fetch_evidence(s["order"])}
    for name, body in files.items():
        put_locked(prefix + name, body, "application/pdf" if name.endswith(".pdf") else "application/octet-stream")
    manifest = {"requestId": item["requestId"], "offerVersion": int(s["offerVersion"]), "pdfSha256": s["pdfSha256"],
                "files": {name: hashlib.sha256(body).hexdigest() for name, body in files.items()},
                "signatures": signatures, "createdAt": c.iso(c.now_utc())}
    put_locked(prefix + "manifest.json", json.dumps(manifest, ensure_ascii=False, default=str).encode("utf-8"),
               "application/json")
    return prefix + "offer.pdf"


def apply_status(item):
    """Webhook: re-reads the order from the provider and records each party
    that has signed there but not here, in signing order."""
    s = _current(item)
    state = signing.status(s["order"])
    done = []
    for party in state["parties"]:
        if party.get("signedAt") and not (s.get("signatures") or {}).get(party["role"]):
            result, order = signing.collect(s["order"], party["role"])
            if result["state"] != "signed":
                break
            record(item, party["role"], {**(result.get("signer") or {}), "method": party.get("method") or "provider"},
                   result.get("evidence") or {}, None, order)
            done.append(party["role"])
            item = c.load(item["locationId"], item["requestId"])
            if item.get("status") != c.OFFER_SENT:
                break
            s = item["signing"]
    return done


def presign(key, seconds=300):
    return _s3().generate_presigned_url("get_object", ExpiresIn=seconds, Params={"Bucket": bucket(), "Key": key})


def document_key(item, doc_id):
    """offer: the current offer PDF; signed: the signed copy; signature: the
    customer's signature evidence."""
    if doc_id == "offer":
        return (item.get("signing") or {}).get("pdfKey")
    if doc_id == "signed":
        return item.get("signedPdfKey")
    if doc_id == "signature":
        return (item.get("signature") or {}).get("evidenceKey")
    return None

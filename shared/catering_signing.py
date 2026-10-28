"""Both-party signing of a catering offer version (issue 5, C2/C8), shared
by catering-offer (the restaurant), catering-customer (the customer) and
catering-signing-webhook. The provider is behind shared/signing.py.

The head item carries `signing` for the offer version being signed:

    {"offerVersion", "pdfKey", "pdfSha256", "order": <signing order>,
     "signatures": {"restaurant"|"customer": {"name", "personalNumberMasked"?,
                    "role"?, "method", "signedAt", "evidenceKey"}}}

The restaurant signs first; only then is the customer told (notice
catering_offer_sent) and may sign. The restaurant's evidence is kept next to
the offer PDF under offers/ (unlocked, tagged retention=offer, so it expires
with an offer that is never signed). When the customer has signed (BankID
or, where the restaurant allows it, simple acceptance), the contract is
binding: the request is `signed` and the evidence package is written under
<tenant>/signed/<requestId>/vNNNN/: the offer PDF (the exact S3 version that
was signed, pdfVersionId), both parties' evidence and a manifest of SHA-256
hashes, every object with its own Object Lock retention (DOCUMENT_LOCK_MODE /
DOCUMENT_LOCK_DAYS; the bucket has no default lock).

A provider that seals the document itself (Scrive) adds its sealed PDF to
the package. Sealing runs after the last signature, so when it is not ready
yet the request is signed anyway (the contract is binding) with
`signing.sealPending`; the provider's next callback stores the sealed PDF
(store_sealed) and the webhook answers 503 until then, so the provider
retries.
"""

import hashlib
import json
import os
from datetime import timedelta

import boto3
from botocore.exceptions import ClientError

from shared import catering as c
from shared import signing, site

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


def retain_until(now=None):
    """DOCUMENT_LOCK_DAYS from now; with DOCUMENT_LOCK_YEAR_END (prod) rounded
    up to the end of that calendar year, since Bokföringslagen counts the
    7 years from the end of the year the record belongs to."""
    until = (now or c.now_utc()) + timedelta(days=int(os.environ["DOCUMENT_LOCK_DAYS"]))
    if os.environ.get("DOCUMENT_LOCK_YEAR_END") == "true":
        until = until.replace(month=12, day=31, hour=23, minute=59, second=59, microsecond=0)
    return until


def put_locked(key, body, content_type):
    """Writes an evidence object with its own retention (C7); -> its version id."""
    extra = {}
    mode, days = os.environ.get("DOCUMENT_LOCK_MODE"), os.environ.get("DOCUMENT_LOCK_DAYS")
    if mode and days:
        extra = {"ObjectLockMode": mode, "ObjectLockRetainUntilDate": retain_until()}
    return _s3().put_object(Bucket=bucket(), Key=key, Body=body, ContentType=content_type,
                            ChecksumAlgorithm="SHA256", **extra).get("VersionId")


def _put_pending(key, body):
    """Evidence of a contract not yet binding: unlocked, expires with the offer."""
    _s3().put_object(Bucket=bucket(), Key=key, Body=body, ContentType="application/json",
                     ChecksumAlgorithm="SHA256", Tagging="retention=offer")
    return key


def _read(key, version=None):
    params = {"Bucket": bucket(), "Key": key, **({"VersionId": version} if version else {})}
    return _s3().get_object(**params)["Body"].read()


def _signed_pdf(s):
    """The offer PDF exactly as signed (its S3 version), checked against the hash."""
    pdf = _read(s["pdfKey"], s.get("pdfVersionId"))
    if hashlib.sha256(pdf).hexdigest() != s["pdfSha256"]:
        raise c.Unavailable("document_mismatch")
    return pdf


def signed_prefix(item, version):
    return f"{item['tenantId']}/signed/{item['requestId']}/v{int(version):04d}/"


def _current(item):
    """The signing record of the current offer version, or NotReady."""
    s = item.get("signing") or {}
    if item.get("status") != c.OFFER_SENT or not s.get("order") \
            or int(s.get("offerVersion") or 0) != int(item.get("currentOfferVersion") or 0):
        raise NotReady("not_signable")
    return s


def restaurant_return_url(item):
    """Where the provider's signing page sends the restaurant back (admin)."""
    admin = c.admin_url(os.environ.get("ADMIN_DASHBOARD_URL"), item)
    return f"{admin}?signering=klar" if admin else None


def customer_return_url(tenant_id, tenant_row, item):
    """Where the provider's signing page sends the guest back (the site)."""
    base, slug = site.base(tenant_id, tenant_row)
    if not base:
        return None
    page = site.page(base, slug, f"/catering/{item['locationId']}/{item['requestId']}")
    return f"{page}{'&' if slug else '?'}signering=klar"


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
    try:
        record(item, role, {**result["signer"], "method": order["sessions"][role].get("method") or "bankid"},
               result.get("evidence") or {}, ip, order)
    except c.InvalidState:
        # The provider's callback recorded it a moment earlier (same signature).
        fresh = c.load(item["locationId"], item["requestId"])
        if ((fresh.get("signing") or {}).get("signatures") or {}).get(role):
            return {"state": "signed"}
        raise
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
    if role == RESTAURANT:
        pending = f"{item['tenantId']}/offers/{item['requestId']}/v{version:04d}-"
        keys = [_put_pending(pending + name, body) for name, body in evidence.items()]
    else:
        pdf = _signed_pdf(s)  # before anything is locked
        for name, body in evidence.items():
            put_locked(prefix + name, body, "application/json")
        keys = [prefix + name for name in evidence]
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
    signed_key, signed_version, signatures, sealed = _finalize(item, s, signatures, prefix, pdf)
    signature = {**{k: entry.get(k) for k in ("name", "personalNumberMasked", "role", "method", "evidenceKey")},
                 "signedAt": entry["signedAt"], "documentHash": (item.get("offer") or {}).get("documentHash"),
                 "pdfSha256": s["pdfSha256"], "provider": order.get("provider"), "signedPdfKey": signed_key}
    sets += ["signature = :sig", "signedAt = :now", "signedOfferVersion = :v", "notice = :n", "signedPdfKey = :pdf",
             "signedPdfVersionId = :pv"]
    values.update({":sig": {k: v for k, v in signature.items() if v is not None}, ":v": version,
                   ":n": c.notice(c.N_SIGNED, now), ":pdf": signed_key, ":pv": signed_version or "-",
                   ":sigs": signatures})
    if sealed == "pending":
        sets.append("signing.sealPending = :true")
        values[":true"] = True
    elif sealed:
        sets += ["sealedPdfKey = :sk", "sealedPdfVersionId = :sv"]
        values.update({":sk": sealed[0], ":sv": sealed[1] or "-"})
    c.transact([c.head_update(item, to_status=c.SIGNED, sets=sets, values=values, names=names), audit])


def _finalize(item, s, signatures, prefix, pdf):
    """The evidence package once the contract is binding: the signed PDF (a
    locked copy of the version the parties signed), the restaurant's
    evidence moved in from offers/, the provider's own evidence, and a
    manifest of hashes. -> (pdf key, its version id, signatures, sealed):
    sealed is (key, version id) of the provider's sealed PDF, "pending" or None."""
    files = {"offer.pdf": pdf}
    sealed = None
    try:
        provider_files = signing.fetch_evidence(s["order"])
    except signing.EvidenceNotReady:
        provider_files, sealed = {}, "pending"
    files.update(provider_files)
    restaurant = dict(signatures.get(RESTAURANT) or {})
    if restaurant.get("evidenceKey") and "/offers/" in restaurant["evidenceKey"]:
        name = restaurant["evidenceKey"].rsplit("/", 1)[1].split("-", 1)[1]
        files[name] = _read(restaurant["evidenceKey"])
        signatures = {**signatures, RESTAURANT: {**restaurant, "evidenceKey": prefix + name}}
    pdf_version = None
    for name, body in files.items():
        version = put_locked(prefix + name, body,
                             "application/pdf" if name.endswith(".pdf") else "application/json")
        pdf_version = version if name == "offer.pdf" else pdf_version
        if name in provider_files and name.endswith(".pdf"):
            sealed = (prefix + name, version)
    manifest = {"requestId": item["requestId"], "offerVersion": int(s["offerVersion"]), "pdfSha256": s["pdfSha256"],
                "files": {name: hashlib.sha256(body).hexdigest() for name, body in files.items()},
                "signatures": signatures, "createdAt": c.iso(c.now_utc())}
    put_locked(prefix + "manifest.json", json.dumps(manifest, ensure_ascii=False, default=str).encode("utf-8"),
               "application/json")
    return prefix + "offer.pdf", pdf_version, signatures, sealed


def store_sealed(item):
    """Adds the provider's sealed PDF to a signed request's locked package
    (raises EvidenceNotReady while the provider is still sealing)."""
    s = item["signing"]
    files = signing.fetch_evidence(s["order"])
    prefix = signed_prefix(item, s["offerVersion"])
    sealed = None
    for name, body in files.items():
        version = put_locked(prefix + name, body, "application/pdf" if name.endswith(".pdf") else "application/json")
        sealed = (prefix + name, version) if name.endswith(".pdf") else sealed
    now = c.now_utc()
    put_locked(prefix + "manifest-sealed.json", json.dumps({
        "requestId": item["requestId"], "offerVersion": int(s["offerVersion"]),
        "files": {name: hashlib.sha256(body).hexdigest() for name, body in files.items()},
        "createdAt": c.iso(now)}).encode("utf-8"), "application/json")
    sets, values = ["updatedAt = :now"], {":now": c.iso(now)}
    if sealed:
        sets += ["sealedPdfKey = :sk", "sealedPdfVersionId = :sv"]
        values.update({":sk": sealed[0], ":sv": sealed[1] or "-"})
    try:
        c.dynamo.table(c.requests_table()).update_item(
            Key=c.request_key(item["locationId"], item["requestId"]),
            UpdateExpression=f"SET {', '.join(sets)} REMOVE signing.sealPending",
            ConditionExpression="signing.sealPending = :true AND signing.offerVersion = :v",
            ExpressionAttributeValues={**values, ":true": True, ":v": int(s["offerVersion"])})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        return  # stored by a concurrent callback
    c.transact([c.audit_put(item["requestId"], "sealed", "provider", now, version=int(s["offerVersion"]),
                            sealedPdfKey=sealed[0] if sealed else None)])


def apply_status(item):
    """Webhook: re-reads the order from the provider and records each party
    that has signed there but not here, in signing order; then stores the
    provider's sealed PDF if it was not ready when the contract became
    binding (raises EvidenceNotReady while it still is not)."""
    if (item.get("signing") or {}).get("sealPending"):
        store_sealed(item)
        return ["sealed"]
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
            if (item.get("signing") or {}).get("sealPending"):
                raise signing.EvidenceNotReady("sealing")  # recorded; the provider's retry stores the seal
            if item.get("status") != c.OFFER_SENT:
                break
            s = item["signing"]
    return done


def presign(key, version=None, seconds=300):
    params = {"Bucket": bucket(), "Key": key, **({"VersionId": version} if version else {})}
    return _s3().generate_presigned_url("get_object", ExpiresIn=seconds, Params=params)


def document_ref(item, doc_id):
    """(key, version id or None). offer: the current offer PDF as rendered;
    signed: the signed copy; signature: the customer's signature evidence."""
    if doc_id == "offer":
        s = item.get("signing") or {}
        return s.get("pdfKey"), s.get("pdfVersionId")
    if doc_id == "signed":  # the provider's sealed PDF when there is one, else our locked copy
        key, version = ((item.get("sealedPdfKey"), item.get("sealedPdfVersionId")) if item.get("sealedPdfKey")
                        else (item.get("signedPdfKey"), item.get("signedPdfVersionId")))
        return key, version if version and version != "-" else None
    if doc_id == "signature":
        return (item.get("signature") or {}).get("evidenceKey"), None
    return None, None

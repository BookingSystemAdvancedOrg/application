"""Scrive Document API v2 adapter (shared/signing_scrive.py) through the real
catering handlers. Scrive is a fake HTTP server serving the recorded
responses in tests/fixtures/scrive/ (no network): create, start, the
callback token, out-of-order and duplicate callbacks, the sealed PDF in
the locked package, cancel on a new version, errors."""

import copy
import hashlib
import io
import json
import urllib.error
import urllib.parse
from datetime import date
from pathlib import Path

import boto3
import pytest

from shared import bankid, signing, signing_scrive
from shared import catering as c
from test_catering import (BUCKET, LOC_A, OFFER, REGION, TENANT_A, body_of, env, guest, sent_offer,  # noqa: F401
                           staff)

FIXTURES = Path(__file__).parent / "fixtures" / "scrive"
BASE = "https://api-testbed.scrive.com"
SECRET = {"provider": "scrive", "baseUrl": BASE, "apiToken": "ct", "apiSecret": "cs", "accessToken": "tt",
          "accessSecret": "ts"}
SEALED = b"%PDF-1.7 sealed by Scrive with evidence attachments"
RESTAURANT_PARTY, CUSTOMER_PARTY = "9222115557378174613", "9222115557378174614"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeScrive:
    """Answers like Scrive's v2 API from the recorded responses; the test
    moves the document along (sign, close, seal)."""

    def __init__(self):
        self.calls, self.updates, self.uploads, self.fail = [], [], [], {}
        self.document = None
        self.next_id = 0

    def __call__(self, request, timeout=None):
        url = urllib.parse.urlparse(request.full_url)
        assert url.scheme == "https" and url.hostname == "api-testbed.scrive.com"
        path = url.path.removeprefix("/api/v2/")
        action = path.split("/")[2] if path.count("/") >= 2 else path
        self.calls.append((request.get_method(), path, dict(request.header_items()), request.data))
        if action in self.fail:
            status, name = self.fail.pop(action)
            raise urllib.error.HTTPError(request.full_url, status, "error", {},
                                         io.BytesIO((FIXTURES / name).read_bytes()))
        if path == "documents/new":
            assert b"%PDF" in request.data and b'name="saved"' in request.data
            self.uploads.append(request.data)
            self.next_id += 1
            new = fixture("document_new.json")
            new["id"] = f"{new['id'][:-1]}{self.next_id}"
            return self._json(new)
        document_id = path.split("/")[1]
        if action == "update":
            self.updates.append(json.loads(urllib.parse.parse_qs(request.data.decode())["document"][0]))
            return self._json(fixture("document_new.json"))
        if action == "start":
            self.document = fixture("document_started.json")
            self.document["id"] = document_id
            return self._json(self.document)
        if action == "get":
            assert self.document and self.document["id"] == document_id
            return self._json(self.document)
        if action == "cancel":
            if self.document and self.document["id"] == document_id:
                self.document["status"] = "canceled"
            return self._json(self.document or {})
        if action == "trash":
            return self._json({})
        if action == "files":
            return _Response(SEALED)
        raise AssertionError(f"unexpected Scrive call {path}")

    @staticmethod
    def _json(body):
        return _Response(json.dumps(body).encode())

    def party(self, party_id):
        return next(p for p in self.document["parties"] if p["id"] == party_id)

    def sign(self, party_id, name, personal_number="198001011234"):
        party = self.party(party_id)
        party["sign_time"] = "2026-10-01T10:00:00Z"
        for field in party["fields"]:
            if field["type"] == "name":
                field["value"] = name
            if field["type"] == "personal_number":
                field["value"] = personal_number

    def close(self, sealed=True):
        self.document["status"] = "closed"
        self.document["sealed_file"] = {"id": "9222115557375404999", "name": "offert-v0001-sealed.pdf"} \
            if sealed else None

    def paths(self, action):
        return [path for _, path, _, _ in self.calls if path.endswith(action)]


@pytest.fixture
def scrive(env, monkeypatch):  # noqa: F811
    boto3.client("secretsmanager", region_name=REGION).put_secret_value(SecretId="signing",
                                                                         SecretString=json.dumps(SECRET))
    bankid.reset_cache()
    fake = FakeScrive()
    monkeypatch.setattr(signing_scrive.urllib.request, "urlopen", fake)
    env.scrive = fake
    return env


def _s3():
    return boto3.client("s3", region_name=REGION)


def webhook(env, rid, token, body=""):  # noqa: F811
    event = {"queryStringParameters": {"l": LOC_A, "r": rid, "t": token}, "body": body, "headers": {}}
    return env.m["signing-webhook"].handler(event, None)


def callback_body(document):
    return urllib.parse.urlencode({"document_id": document["id"], "document_json": json.dumps(document)})


def restaurant_signs_at_scrive(env, rid):  # noqa: F811
    start = body_of(staff(env, "POST", "/restaurant-sign", request_id=rid))
    assert start["kind"] == "redirect"
    env.scrive.sign(RESTAURANT_PARTY, "Maria Rossi")
    return body_of(staff(env, "POST", "/restaurant-sign/status", request_id=rid))


# --- create -----------------------------------------------------------------------------------

def test_create_uploads_the_signed_pdf_and_sets_both_parties(scrive):
    rid, _ = sent_offer(scrive, sign=False, companyName=None)
    item = c.load(LOC_A, rid)
    order = item["signing"]["order"]
    assert order["provider"] == "scrive" and order["documentId"] == "9222115557379108031"
    assert order["partyIds"] == {"restaurant": RESTAURANT_PARTY, "customer": CUSTOMER_PARTY}
    upload = scrive.scrive.uploads[0]
    pdf = upload[upload.index(b"%PDF"):upload.rindex(b"\r\n--")]
    assert hashlib.sha256(pdf).hexdigest() == item["signing"]["pdfSha256"]

    update = scrive.scrive.updates[0]
    author, restaurant, customer = update["parties"]
    assert author == {**author, "id": "9222115557378174612", "signatory_role": "viewer", "delivery_method": "api",
                      "confirmation_delivery_method": "none"}
    for party, order_no in ((restaurant, 1), (customer, 2)):
        assert party["sign_order"] == order_no and party["signatory_role"] == "signing_party"
        assert party["authentication_method_to_sign"] == "se_bankid" and party["delivery_method"] == "api"
        assert party["confirmation_delivery_method"] == "none" and party["notification_delivery_method"] == "none"
    fields = {f["type"]: f["value"] for f in restaurant["fields"]}
    assert fields["company"] == "Roma AB" and fields["name"] == ""
    name = next(f for f in restaurant["fields"] if f["type"] == "name")  # the signer types it
    assert name["is_obligatory"] is True and name["should_be_filled_by_sender"] is False
    for party in (restaurant, customer):  # se_bankid: the signer fills in the personal number
        pn = next(f for f in party["fields"] if f["type"] == "personal_number")
        assert pn["value"] == "" and pn["is_obligatory"] is True and pn["should_be_filled_by_sender"] is False
    assert {f["type"]: f["value"] for f in customer["fields"]}["name"] == "Anna Svensson"
    assert restaurant["sign_success_redirect_url"] == f"https://admin.example.se/catering/{LOC_A}/{rid}?signering=klar"
    assert customer["sign_success_redirect_url"] == \
        f"https://dev123.cloudfront.net/catering/{LOC_A}/{rid}?restaurang=roma&signering=klar"
    assert update["api_callback_url"].endswith(f"?l={LOC_A}&r={rid}&t={order['callbackToken']}")
    assert update["lang"] == "sv" and update["timezone"] == "Europe/Stockholm"
    assert update["display_options"]["show_reject_option"] is False
    assert 1 <= update["days_to_sign"] <= 90

    _, _, headers, _ = scrive.scrive.calls[0]
    assert headers["Authorization"] == ('OAuth oauth_signature_method="PLAINTEXT", oauth_consumer_key="ct", '
                                        'oauth_token="tt", oauth_signature="cs&ts"')
    assert "api_delivery_url" not in json.dumps(c.staff_view(item), default=str) and "/s/" not in json.dumps(order, default=str)


def test_days_to_sign_follows_the_offer_validity():
    today = date(2026, 10, 28)
    assert signing_scrive._days_to_sign("2026-11-04", today) == 7
    assert signing_scrive._days_to_sign("2026-10-01", today) == 1
    assert signing_scrive._days_to_sign("2027-10-01", today) == 90
    assert signing_scrive._days_to_sign("not a date", today) == 14


def test_a_failed_update_trashes_the_draft_and_answers_503(scrive):
    scrive.scrive.fail["update"] = (401, "error_invalid_authorisation.json")
    from test_catering import create
    rid = body_of(create(scrive))["requestId"]
    response = staff(scrive, "POST", "/offer/send", body=OFFER, request_id=rid)
    assert response["statusCode"] == 503 and body_of(response) == {"error": "signing_unavailable"}
    assert scrive.scrive.paths("trash") and c.load(LOC_A, rid)["status"] == "requested"


def test_a_sign_method_scrive_has_not_enabled_is_named_in_the_error(scrive):
    scrive.scrive.fail["start"] = (409, "error_sign_method_disabled.json")
    from test_catering import create
    rid = body_of(create(scrive))["requestId"]
    response = staff(scrive, "POST", "/offer/send", body=OFFER, request_id=rid)
    assert response["statusCode"] == 409
    assert body_of(response) == {"error": "signing_authentication_to_sign_method_disabled"}
    assert scrive.scrive.paths("trash") and c.load(LOC_A, rid)["status"] == "requested"


def test_dev_can_sign_without_bankid_while_scrive_enables_it(scrive):
    boto3.client("secretsmanager", region_name=REGION).put_secret_value(
        SecretId="signing", SecretString=json.dumps({**SECRET, "signMethod": "standard"}))
    bankid.reset_cache()
    sent_offer(scrive, sign=False)
    for party in scrive.scrive.updates[0]["parties"][1:]:
        assert party["authentication_method_to_sign"] == "standard"
        assert next(f for f in party["fields"] if f["type"] == "personal_number")["is_obligatory"] is False


# --- start and collect ------------------------------------------------------------------------

def test_start_redirects_to_scrive_and_the_restaurant_signs_first(scrive):
    rid, token = sent_offer(scrive, sign=False)
    assert guest(scrive, "POST", "/sign", rid, token)["statusCode"] == 409  # awaiting_restaurant
    start = body_of(staff(scrive, "POST", "/restaurant-sign", request_id=rid))
    assert start == {"kind": "redirect",
                     "url": f"{BASE}/s/9222115557379108034/{RESTAURANT_PARTY}/5a0c1e2b3d4f6a7b"}
    assert body_of(staff(scrive, "POST", "/restaurant-sign/status", request_id=rid)) == \
        {"state": "pending", "hint": "outstandingTransaction", "qrData": None}
    scrive.scrive.sign(RESTAURANT_PARTY, "Maria Rossi")
    assert body_of(staff(scrive, "POST", "/restaurant-sign/status", request_id=rid))["state"] == "signed"
    item = c.load(LOC_A, rid)
    restaurant = item["signing"]["signatures"]["restaurant"]
    assert restaurant["method"] == "scrive_se_bankid" and restaurant["name"] == "Maria Rossi"
    assert restaurant["personalNumberMasked"] == "19800101-****"
    assert restaurant["evidenceKey"] == f"{TENANT_A}/offers/{rid}/v0001-restaurant-scrive.json"
    assert "ObjectLockMode" not in _s3().head_object(Bucket=BUCKET, Key=restaurant["evidenceKey"])
    assert item["status"] == "offer_sent" and item["notice"]["type"] == "catering_offer_sent"
    start = body_of(guest(scrive, "POST", "/sign", rid, token))
    assert start["kind"] == "redirect" and start["url"].endswith(f"/{CUSTOMER_PARTY}/9e8d7c6b5a4f3e2d")


def test_both_sign_and_the_sealed_pdf_is_locked_with_the_package(scrive):
    rid, token = sent_offer(scrive, sign=False)
    restaurant_signs_at_scrive(scrive, rid)
    guest(scrive, "POST", "/sign", rid, token)
    scrive.scrive.sign(CUSTOMER_PARTY, "Anna Svensson")
    scrive.scrive.close(sealed=True)
    done = body_of(guest(scrive, "POST", "/sign/status", rid, token))
    assert done["status"] == "complete" and done["request"]["status"] == "signed"
    item = c.load(LOC_A, rid)
    prefix = f"{TENANT_A}/signed/{rid}/v0001/"
    assert item["sealedPdfKey"] == prefix + "scrive-sealed.pdf" and "sealPending" not in item["signing"]
    listed = {o["Key"] for o in _s3().list_objects_v2(Bucket=BUCKET, Prefix=prefix)["Contents"]}
    assert listed == {prefix + n for n in ("offer.pdf", "scrive-sealed.pdf", "restaurant-scrive.json",
                                           "customer-scrive.json", "manifest.json")}
    for key in listed:
        assert _s3().head_object(Bucket=BUCKET, Key=key)["ObjectLockMode"] == "GOVERNANCE"
    sealed = _s3().get_object(Bucket=BUCKET, Key=item["sealedPdfKey"], VersionId=item["sealedPdfVersionId"])
    assert sealed["Body"].read() == SEALED
    manifest = json.loads(_s3().get_object(Bucket=BUCKET, Key=prefix + "manifest.json")["Body"].read())
    assert manifest["files"]["scrive-sealed.pdf"] == hashlib.sha256(SEALED).hexdigest()
    assert manifest["files"]["offer.pdf"] == item["signing"]["pdfSha256"]
    from test_catering_signing import guest_document
    assert "scrive-sealed.pdf" in body_of(guest_document(scrive, rid, token, "signed"))["url"]


def test_a_party_that_rejects_or_a_document_that_timed_out_fails(scrive):
    rid, _ = sent_offer(scrive, sign=False)
    staff(scrive, "POST", "/restaurant-sign", request_id=rid)
    scrive.scrive.party(RESTAURANT_PARTY)["rejected_time"] = "2026-10-01T10:00:00Z"
    assert body_of(staff(scrive, "POST", "/restaurant-sign/status", request_id=rid)) == \
        {"state": "failed", "hint": "rejected"}
    scrive.scrive.party(RESTAURANT_PARTY)["rejected_time"] = None
    scrive.scrive.document["status"] = "timedout"
    response = staff(scrive, "POST", "/restaurant-sign", request_id=rid)
    assert response["statusCode"] == 409 and body_of(response) == {"error": "signing_document_timedout"}


# --- callbacks --------------------------------------------------------------------------------

def test_callback_token_mismatch_and_another_document_are_refused(scrive):
    rid, _ = sent_offer(scrive, sign=False)
    order = c.load(LOC_A, rid)["signing"]["order"]
    body = callback_body(scrive.scrive.document)
    assert webhook(scrive, rid, "x" * len(order["callbackToken"]), body)["statusCode"] == 401
    assert webhook(scrive, rid, order["callbackToken"][:-1], body)["statusCode"] == 401
    other = urllib.parse.urlencode({"document_id": "123", "document_json": "{}"})
    assert webhook(scrive, rid, order["callbackToken"], other)["statusCode"] == 401


def test_the_callback_body_is_never_trusted(scrive):
    rid, _ = sent_offer(scrive, sign=False)
    order = c.load(LOC_A, rid)["signing"]["order"]
    claimed = copy.deepcopy(scrive.scrive.document)
    claimed["status"] = "closed"
    for party in claimed["parties"]:
        party["sign_time"] = "2026-10-01T10:00:00Z"
    response = webhook(scrive, rid, order["callbackToken"], callback_body(claimed))
    assert body_of(response) == {"result": "handled", "recorded": []}  # Scrive itself says pending
    assert c.load(LOC_A, rid)["status"] == "offer_sent"


def test_out_of_order_and_duplicate_callbacks(scrive):
    rid, token = sent_offer(scrive, sign=False)
    order = c.load(LOC_A, rid)["signing"]["order"]
    fake = scrive.scrive
    fake.sign(RESTAURANT_PARTY, "Maria Rossi")
    after_restaurant = callback_body(fake.document)
    fake.sign(CUSTOMER_PARTY, "Anna Svensson")
    fake.close(sealed=True)
    closed = callback_body(fake.document)
    # the "closed" callback arrives first: both parties recorded in order, from a re-fetch
    assert body_of(webhook(scrive, rid, order["callbackToken"], closed)) == \
        {"result": "handled", "recorded": ["restaurant", "customer"]}
    item = c.load(LOC_A, rid)
    assert item["status"] == "signed" and item["signature"]["name"] == "Anna Svensson"
    assert item["sealedPdfKey"].endswith("/scrive-sealed.pdf")
    # the older one arrives late, then the closed one again
    assert body_of(webhook(scrive, rid, order["callbackToken"], after_restaurant)) == \
        {"result": "handled", "recorded": []}
    assert body_of(webhook(scrive, rid, order["callbackToken"], closed)) == {"result": "duplicate"}
    # the guest comes back from Scrive afterwards: still complete
    assert body_of(guest(scrive, "POST", "/sign/status", rid, token))["status"] == "none"
    assert len([h for h in c.history(rid) if h["action"] == "signed"]) == 1


def test_a_seal_that_is_not_ready_is_stored_on_a_later_callback(scrive):
    rid, token = sent_offer(scrive, sign=False)
    restaurant_signs_at_scrive(scrive, rid)
    guest(scrive, "POST", "/sign", rid, token)
    scrive.scrive.sign(CUSTOMER_PARTY, "Anna Svensson")
    scrive.scrive.close(sealed=False)
    assert body_of(guest(scrive, "POST", "/sign/status", rid, token))["status"] == "complete"
    item = c.load(LOC_A, rid)
    assert item["status"] == "signed" and item["signing"]["sealPending"] is True and "sealedPdfKey" not in item
    token_t = item["signing"]["order"]["callbackToken"]
    body = callback_body(scrive.scrive.document)
    response = webhook(scrive, rid, token_t, body)
    assert response["statusCode"] == 503 and body_of(response) == {"error": "seal_pending"}
    assert not [h for h in c.history(rid) if h["action"] == "signing_webhook"]  # not deduped: Scrive retries
    scrive.scrive.close(sealed=True)
    assert body_of(webhook(scrive, rid, token_t, body)) == {"result": "handled", "recorded": ["sealed"]}
    item = c.load(LOC_A, rid)
    assert "sealPending" not in item["signing"] and item["sealedPdfKey"].endswith("/scrive-sealed.pdf")
    assert _s3().head_object(Bucket=BUCKET, Key=item["sealedPdfKey"])["ObjectLockMode"] == "GOVERNANCE"
    assert any(h["action"] == "sealed" for h in c.history(rid))
    assert body_of(webhook(scrive, rid, token_t, body)) == {"result": "duplicate"}


def test_the_guest_coming_back_while_the_callback_records_still_sees_complete(scrive):
    rid, token = sent_offer(scrive, sign=False)
    restaurant_signs_at_scrive(scrive, rid)
    guest(scrive, "POST", "/sign", rid, token)
    scrive.scrive.sign(CUSTOMER_PARTY, "Anna Svensson")
    scrive.scrive.close(sealed=True)
    from shared import catering_signing as sign
    stale = c.load(LOC_A, rid)
    order = stale["signing"]["order"]
    assert webhook(scrive, rid, order["callbackToken"], callback_body(scrive.scrive.document))["statusCode"] == 200
    assert sign.poll(stale, sign.CUSTOMER, "203.0.113.9") == {"state": "signed"}  # its record lost the race


# --- cancel -----------------------------------------------------------------------------------

def test_a_new_version_cancels_the_old_document(scrive):
    rid, _ = sent_offer(scrive, sign=False)
    first = c.load(LOC_A, rid)["signing"]["order"]["documentId"]
    staff(scrive, "POST", "/offer/send", body=OFFER, request_id=rid)
    assert scrive.scrive.paths("cancel") == [f"documents/{first}/cancel"]
    assert c.load(LOC_A, rid)["signing"]["order"]["documentId"] != first


def test_cancel_failures_are_best_effort(scrive):
    rid, _ = sent_offer(scrive, sign=False)
    scrive.scrive.fail["cancel"] = (409, "error_document_state.json")
    assert body_of(staff(scrive, "POST", "/decline", body={"reason": "Fullt"}, request_id=rid))["status"] == "declined"


# --- configuration ----------------------------------------------------------------------------

@pytest.mark.parametrize("config, environment", [
    ({**SECRET, "baseUrl": "https://evil.example.com"}, "dev"),
    ({**SECRET, "baseUrl": "http://api-testbed.scrive.com"}, "dev"),
    ({**SECRET, "apiSecret": ""}, "dev"),
    (SECRET, "prod"),
])
def test_a_bad_secret_is_refused(config, environment):
    with pytest.raises(signing.SigningUnavailable):
        signing_scrive.check_config(config, environment)


@pytest.mark.parametrize("method, environment", [("standard", "prod"), ("sms_pin", "dev")])
def test_prod_signs_only_with_bankid(method, environment):
    with pytest.raises(signing.SigningUnavailable):
        signing_scrive.check_config({**SECRET, "baseUrl": "https://scrive.com", "signMethod": method}, environment)


def test_prod_accepts_the_production_host():
    signing_scrive.check_config({**SECRET, "baseUrl": "https://scrive.com"}, "prod")


def test_prod_lock_runs_to_the_end_of_the_seventh_year(monkeypatch):
    from datetime import datetime, timezone
    from shared import catering_signing as sign
    monkeypatch.setenv("DOCUMENT_LOCK_DAYS", "2557")
    signed_at = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("DOCUMENT_LOCK_YEAR_END", "true")
    assert sign.retain_until(signed_at) == datetime(2033, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    monkeypatch.setenv("DOCUMENT_LOCK_YEAR_END", "false")
    assert sign.retain_until(signed_at) == datetime(2033, 1, 5, 9, 0, tzinfo=timezone.utc)

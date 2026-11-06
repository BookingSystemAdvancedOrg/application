"""Wallet passes (feature K, ADR-K 6, CONTRACTS.md K18/K19): the provider port
in shared/features/events_tickets/passes.py, off by default, the dev
simulator and the live adapters signed with a throwaway test certificate
generated here (no key material is committed). Every pass is bound to one
valid ticket of the link's own order or ticket, in the link's own tenant."""

import base64
import hashlib
import io
import json
import subprocess
import zipfile
from datetime import datetime, timedelta, timezone

import boto3
import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from shared import tenant as shared_tenant
from shared.features.events_tickets import passes, qr
from tenant_support import LOC_A, REGION, TENANT_A, TENANT_B, features_on, tenant_row
import test_events_tickets as base
from test_events_tickets import ACCT_A, OCC1, L, OWNER_A, call, sold, tickets, token_of

PASS = "/public/tickets/{token}/pass"
PAGE = "/public/tickets/{token}"


env = base.env  # the shared moto fixture (two tenants, events on)


@pytest.fixture(autouse=True)
def wallet_env(monkeypatch):
    for name in (passes.MODE_ENV, passes.APPLE_SECRET_ENV, passes.GOOGLE_SECRET_ENV):
        monkeypatch.delenv(name, raising=False)
    passes.reset_cache()
    yield
    passes.reset_cache()


def wallet_on(monkeypatch, mode="simulator"):
    monkeypatch.setenv(passes.MODE_ENV, mode)


def get_pass(e, ticket_id, kind, token=None, **extra):
    event = {"routeKey": f"GET {PASS}", "pathParameters": {"token": token or token_of(e)},
             "queryStringParameters": {"ticketId": ticket_id, "kind": kind, **extra}, "headers": {},
             "requestContext": {"http": {"method": "GET", "sourceIp": "203.0.113.5"}}, "body": None}
    return e.api.handler(event, None)


def page(e, token=None):
    return call(e, "GET", PAGE, params={"token": token or token_of(e)})[1]


def first_ticket(e):
    return sorted(t["ticketId"] for t in tickets(e))[0]


# --- off by default -------------------------------------------------------------------------------

def test_off_by_default_hides_wallet_and_refuses_passes(env):
    sold(env)
    assert page(env)["wallet"] == {"apple": False, "google": False}
    for kind in passes.KINDS:
        assert get_pass(env, first_ticket(env), kind)["statusCode"] == 404


@pytest.mark.parametrize("value", ["", "maybe", "LIVEish"])
def test_unknown_mode_is_off(env, monkeypatch, value):
    wallet_on(monkeypatch, value)
    assert passes.mode() == "off" and isinstance(passes.provider(), passes.OffWallet)


def test_simulator_is_never_used_in_prod(env, monkeypatch):
    sold(env)
    wallet_on(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "prod")
    assert passes.mode() == "off"
    assert page(env)["wallet"] == {"apple": False, "google": False}
    assert get_pass(env, first_ticket(env), "apple")["statusCode"] == 404


# --- simulator ------------------------------------------------------------------------------------

def _json(response):
    assert response["statusCode"] == 200 and response["isBase64Encoded"]
    assert response["headers"]["Cache-Control"] == "no-store"
    return json.loads(base64.b64decode(response["body"]))


def test_simulator_apple_pass_is_deterministic_and_bound_to_the_ticket(env, monkeypatch):
    sold(env)
    wallet_on(monkeypatch)
    assert page(env)["wallet"] == {"apple": True, "google": True}
    ticket_id = first_ticket(env)
    first = get_pass(env, ticket_id, "apple")
    assert first["body"] == get_pass(env, ticket_id, "apple")["body"]  # deterministic
    body = _json(first)
    assert body["simulated"] is True and body["kind"] == "apple"
    pass_json = body["pass"]
    assert pass_json["serialNumber"] == ticket_id
    assert pass_json["barcodes"][0]["message"] == qr.payload(TENANT_A, OCC1, ticket_id)
    assert pass_json["eventTicket"]["primaryFields"][0]["value"] == "Vinmiddag"
    assert pass_json["eventTicket"]["auxiliaryFields"][0]["value"] == "Sara Svensson"
    assert pass_json["relevantDate"] == base.START
    assert "buyerEmail" not in json.dumps(body) and "sara@example.se" not in json.dumps(body).lower()


def test_simulator_google_link_stub(env, monkeypatch):
    sold(env)
    wallet_on(monkeypatch)
    ticket_id = first_ticket(env)
    body = _json(get_pass(env, ticket_id, "google"))
    assert body["simulated"] is True and body["saveUrl"].startswith("https://pay.google.com/gp/v/save/simulated-")
    obj = body["claims"]["payload"]["eventTicketObjects"][0]
    assert obj["barcode"]["value"] == qr.payload(TENANT_A, OCC1, ticket_id)
    assert obj["ticketHolderName"] == "Sara Svensson"
    assert body["claims"]["origins"] == ["https://dev-site.example"]
    assert body == _json(get_pass(env, ticket_id, "google"))


def test_pass_refused_for_tickets_outside_the_link(env, monkeypatch):
    sold(env)
    wallet_on(monkeypatch)
    ids = sorted(t["ticketId"] for t in tickets(env))
    # a single-ticket link only reaches its own ticket
    single = qr.token("t", TENANT_A, ids[0])
    assert get_pass(env, ids[0], "apple", token=single)["statusCode"] == 200
    assert get_pass(env, ids[1], "apple", token=single)["statusCode"] == 404
    # unknown ticket, forged and foreign-tenant tokens, unknown kind
    assert get_pass(env, "01TICKETNOPEAAAAAAAAAAAAAA", "apple")["statusCode"] == 404
    assert get_pass(env, ids[0], "apple", token=token_of(env) + "x")["statusCode"] == 404
    forged = qr.token("o", TENANT_B, "01ORDERAAAAAAAAAAAAAAAAAAA")
    assert get_pass(env, ids[0], "apple", token=forged)["statusCode"] == 404
    assert get_pass(env, ids[0], "samsung")["statusCode"] == 404
    assert get_pass(env, ids[0], "apple", extra="1")["statusCode"] == 400


def test_pass_requires_ticket_id_and_kind(env, monkeypatch):
    sold(env)
    wallet_on(monkeypatch)
    token = token_of(env)
    for query in ({}, {"kind": "apple"}, {"ticketId": first_ticket(env)}, {"ticketId": "", "kind": "apple"}):
        event = {"routeKey": f"GET {PASS}", "pathParameters": {"token": token}, "queryStringParameters": query or None,
                 "headers": {}, "requestContext": {"http": {"method": "GET", "sourceIp": "203.0.113.5"}}, "body": None}
        assert env.api.handler(event, None)["statusCode"] == 400, query


def test_pass_refused_for_a_refunded_ticket(env, monkeypatch):
    sold(env)
    wallet_on(monkeypatch)
    ticket_id = first_ticket(env)
    status, _ = call(env, "POST", L + "/tickets/{ticketId}/refund", params={"locationId": LOC_A, "ticketId": ticket_id},
                     body={"txId": "01TXREFUNDAAAAAAAAAAAAAAAA", "reason": "Sjuk"}, claims=OWNER_A)
    assert status == 200
    assert get_pass(env, ticket_id, "apple")["statusCode"] == 404
    other = sorted(t["ticketId"] for t in tickets(env))[1]
    assert get_pass(env, other, "apple")["statusCode"] == 200


def test_pass_refused_with_the_feature_off(env, monkeypatch):
    sold(env)
    wallet_on(monkeypatch)
    boto3.resource("dynamodb", region_name=REGION).Table("test-tenant").put_item(
        Item=tenant_row(TENANT_A, features=features_on(), stripe={"accountId": ACCT_A}))
    shared_tenant.reset_caches()
    assert get_pass(env, first_ticket(env), "apple")["statusCode"] == 404


# --- live adapters with a throwaway certificate ------------------------------------------------------

def _cert(name, key, issuer_name=None, issuer_key=None):
    now = datetime.now(timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    return (x509.CertificateBuilder().subject_name(subject)
            .add_extension(x509.BasicConstraints(ca=issuer_key is None, path_length=None), critical=True)
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_name or name)]))
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
            .sign(issuer_key or key, hashes.SHA256()))


def _pem(obj):
    if isinstance(obj, x509.Certificate):
        return obj.public_bytes(serialization.Encoding.PEM).decode()
    return obj.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


@pytest.fixture(scope="module")
def keys():
    wwdr_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pass_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    google_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    wwdr = _cert("Test WWDR", wwdr_key)
    return {"wwdr": wwdr, "pass_cert": _cert("Pass Type ID: pass.se.booqy.test", pass_key, "Test WWDR", wwdr_key),
            "pass_key": pass_key, "google_key": google_key}


def _secret(name, value=None):
    """A secret like Terraform makes them: no value at all until someone sets one by hand."""
    extra = {} if value is None else {"SecretString": value if isinstance(value, str) else json.dumps(value)}
    return boto3.client("secretsmanager", region_name=REGION).create_secret(Name=name, **extra)["ARN"]


def apple_secret(keys):
    return {"passTypeIdentifier": "pass.se.booqy.test", "teamIdentifier": "TEAM123456",
            "certificatePem": _pem(keys["pass_cert"]), "privateKeyPem": _pem(keys["pass_key"]),
            "wwdrCertificatePem": _pem(keys["wwdr"])}


def google_secret(keys):
    return {"issuerId": "3388000000012345678", "client_email": "wallet@booqy-test.iam.gserviceaccount.com",
            "private_key": _pem(keys["google_key"])}


def test_live_without_secrets_is_hidden(env, monkeypatch):
    sold(env)
    wallet_on(monkeypatch, "live")
    assert page(env)["wallet"] == {"apple": False, "google": False}
    monkeypatch.setenv(passes.APPLE_SECRET_ENV, _secret("apple-empty"))
    monkeypatch.setenv(passes.GOOGLE_SECRET_ENV, _secret("google-partial", {"issuerId": "1"}))
    passes.reset_cache()
    assert page(env)["wallet"] == {"apple": False, "google": False}
    monkeypatch.setenv(passes.APPLE_SECRET_ENV, _secret("apple-blank", "  "))
    passes.reset_cache()
    assert page(env)["wallet"] == {"apple": False, "google": False}
    monkeypatch.setenv(passes.APPLE_SECRET_ENV, "arn:aws:secretsmanager:eu-north-1:000000000000:secret:missing")
    passes.reset_cache()
    assert page(env)["wallet"] == {"apple": False, "google": False}
    assert get_pass(env, first_ticket(env), "apple")["statusCode"] == 404


def test_live_apple_pkpass_is_signed(env, monkeypatch, keys, tmp_path):
    sold(env)
    wallet_on(monkeypatch, "live")
    monkeypatch.setenv(passes.APPLE_SECRET_ENV, _secret("apple", apple_secret(keys)))
    assert page(env)["wallet"] == {"apple": True, "google": False}
    ticket_id = first_ticket(env)
    response = get_pass(env, ticket_id, "apple")
    assert response["statusCode"] == 200 and response["headers"]["Content-Type"] == passes.PKPASS
    assert response["headers"]["Content-Disposition"] == f'attachment; filename="biljett-{ticket_id}.pkpass"'
    archive = zipfile.ZipFile(io.BytesIO(base64.b64decode(response["body"])))
    names = set(archive.namelist())
    assert names == {"pass.json", "icon.png", "icon@2x.png", "manifest.json", "signature"}
    manifest = json.loads(archive.read("manifest.json"))
    for name in names - {"manifest.json", "signature"}:
        assert manifest[name] == hashlib.sha1(archive.read(name), usedforsecurity=False).hexdigest()
    pass_json = json.loads(archive.read("pass.json"))
    assert pass_json["passTypeIdentifier"] == "pass.se.booqy.test" and pass_json["teamIdentifier"] == "TEAM123456"
    assert pass_json["barcodes"][0]["message"] == qr.payload(TENANT_A, OCC1, ticket_id)
    # the detached PKCS #7 signature verifies over manifest.json with the pass certificate
    (tmp_path / "manifest.json").write_bytes(archive.read("manifest.json"))
    (tmp_path / "signature").write_bytes(archive.read("signature"))
    (tmp_path / "ca.pem").write_text(_pem(keys["wwdr"]))
    verified = subprocess.run(["openssl", "cms", "-verify", "-binary", "-inform", "DER", "-in",
                               str(tmp_path / "signature"), "-content", str(tmp_path / "manifest.json"),
                               "-CAfile", str(tmp_path / "ca.pem"), "-purpose", "any", "-out", "/dev/null"],
                              capture_output=True, text=True)
    assert verified.returncode == 0, verified.stderr
    assert get_pass(env, ticket_id, "google")["statusCode"] == 404  # no Google secret


def test_live_google_redirects_to_a_signed_save_link(env, monkeypatch, keys):
    sold(env)
    wallet_on(monkeypatch, "live")
    monkeypatch.setenv(passes.GOOGLE_SECRET_ENV, _secret("google", google_secret(keys)))
    assert page(env)["wallet"] == {"apple": False, "google": True}
    ticket_id = first_ticket(env)
    response = get_pass(env, ticket_id, "google")
    assert response["statusCode"] == 302 and response["headers"]["Cache-Control"] == "no-store"
    token = response["headers"]["Location"].removeprefix(passes.GOOGLE_SAVE_URL)
    claims = jwt.decode(token, keys["google_key"].public_key(), algorithms=["RS256"], audience="google")
    assert claims["iss"] == "wallet@booqy-test.iam.gserviceaccount.com" and claims["typ"] == "savetowallet"
    obj = claims["payload"]["eventTicketObjects"][0]
    assert obj["id"] == f"3388000000012345678.{ticket_id}"
    assert obj["classId"] == "3388000000012345678.booqy-event-ticket"
    assert "eventTicketClasses" not in claims["payload"]
    assert len(token) < 1800  # the class lives in Google Wallet, not in the JWT
    assert obj["barcode"]["value"] == qr.payload(TENANT_A, OCC1, ticket_id)
    assert get_pass(env, ticket_id, "apple")["statusCode"] == 404  # no Apple secret


def test_worst_case_google_link_stays_under_1800_characters(env, monkeypatch, keys):
    """The app's longest names (title 120, other names 80) with å/ä/ö, an issuer id and service account
    address as long as Google issues them, signed with a 2048-bit key."""
    long = "Åäö" * 40
    data = passes.PassData(
        tenant_id=TENANT_A, event_id="01" + "E" * 24, ticket_id="01" + "T" * 24, title=("Å" * 119 + "ö"),
        start_at="2026-12-31T23:59:59+01:00", end_at="2027-01-01T01:30:00+01:00", location_name=long[:80],
        type_name=long[:80], holder_name=long[:80], qr=qr.payload(TENANT_A, OCC1, "01" + "T" * 24))
    secret = {"issuerId": "3388000000012345678", "client_email": "booqy-wallet-issuer@some-long-project-name-123456.iam.gserviceaccount.com",
              "private_key": _pem(keys["google_key"])}
    monkeypatch.setenv("CUSTOMER_SITE_URL", "https://kund.example-restaurang.se")
    url = passes.google_save_url(data, secret)
    token = url.removeprefix(passes.GOOGLE_SAVE_URL)
    assert len(url) < 1800, len(url)
    obj = jwt.decode(token, keys["google_key"].public_key(), algorithms=["RS256"], audience="google")["payload"]["eventTicketObjects"][0]
    assert obj["ticketHolderName"].endswith("\u2026") and len(obj["ticketHolderName"]) == passes.HOLDER_CAP
    assert len(obj["ticketType"]["defaultValue"]["value"]) == passes.TYPE_CAP
    assert obj["textModulesData"][0]["header"].startswith("Å") and len(obj["textModulesData"][0]["header"]) == passes.TITLE_CAP
    assert "\\u00" not in token and "end" not in {m["id"] for m in obj["textModulesData"]}
    assert data.end_at not in json.dumps(obj["textModulesData"])  # no end time under a location header


EMAIL_85 = "booqy-wallet-issuer@" + "p" * 34 + "-123456.iam.gserviceaccount.com"
assert len(EMAIL_85) == 85


@pytest.mark.parametrize("name", ["\U0001F600" * 80, "\u20ac" * 80, "\u00e5\u00e4\u00f6" * 27, "Z" * 80,
                                  "\U0001F600\u20ac\u00e5a" * 20], ids=["emoji", "euro", "aao", "ascii", "mixed"])
@pytest.mark.parametrize("origin,class_id", [("https://kund.example-restaurang.se", ""),
                                             ("https://" + "o" * 60 + ".example-restaurang.se", "c" * 60)],
                         ids=["short", "long-origin-and-classid"])
def test_google_link_is_short_for_any_script_email_origin_and_class(env, monkeypatch, keys, name, origin, class_id):
    """Emoji cost 4 UTF-8 bytes, the euro sign 3, a-ring/a-umlaut/o-umlaut 2: the finished link is bounded
    whatever the characters, the service account address, the origin or the classId."""
    data = passes.PassData(
        tenant_id=TENANT_A, event_id="01" + "E" * 24, ticket_id="01" + "T" * 24, title=name + name[:40],
        start_at="2026-12-31T23:59:59+01:00", end_at="2027-01-01T01:30:00+01:00", location_name=name,
        type_name=name, holder_name=name, qr=qr.payload(TENANT_A, OCC1, "01" + "T" * 24))
    secret = {"issuerId": "3388000000012345678", "client_email": EMAIL_85, "private_key": _pem(keys["google_key"])}
    if class_id:
        secret["classId"] = class_id
    monkeypatch.setenv("CUSTOMER_SITE_URL", origin)
    url = passes.google_save_url(data, secret)
    assert len(url) < 1800 and len(url) <= passes.MAX_SAVE_URL, len(url)
    claims = jwt.decode(url.removeprefix(passes.GOOGLE_SAVE_URL), keys["google_key"].public_key(),
                        algorithms=["RS256"], audience="google")
    obj = claims["payload"]["eventTicketObjects"][0]
    assert obj["classId"].endswith(class_id or "booqy-event-ticket")
    assert data.start_at not in json.dumps(obj.get("textModulesData", []))  # start only in validTimeInterval
    assert obj["validTimeInterval"]["start"]["date"] == data.start_at


def test_google_link_drops_text_modules_then_refuses_when_nothing_helps(env, monkeypatch, keys):
    data = passes.PassData(
        tenant_id=TENANT_A, event_id="01" + "E" * 24, ticket_id="01" + "T" * 24, title="\U0001F600" * 80,
        start_at="2026-12-31T23:59:59+01:00", end_at="2027-01-01T01:30:00+01:00", location_name="\U0001F600" * 80,
        type_name="\U0001F600" * 80, holder_name="\U0001F600" * 80, qr=qr.payload(TENANT_A, OCC1, "01" + "T" * 24))
    secret = {"issuerId": "3388000000012345678", "client_email": EMAIL_85, "private_key": _pem(keys["google_key"]),
              "classId": "c" * 1200}
    monkeypatch.setenv("CUSTOMER_SITE_URL", "https://kund.example-restaurang.se")
    with pytest.raises(ValueError):
        passes.google_save_url(data, secret)  # a 1200 character classId alone is over the limit
    secret["classId"] = "c" * 280
    url = passes.google_save_url(data, secret)
    obj = jwt.decode(url.removeprefix(passes.GOOGLE_SAVE_URL), keys["google_key"].public_key(),
                     algorithms=["RS256"], audience="google")["payload"]["eventTicketObjects"][0]
    assert len(url) <= passes.MAX_SAVE_URL
    assert "textModulesData" not in obj  # optional text modules were dropped to fit


def test_cap_counts_bytes_not_only_characters():
    assert len(passes._cap("\U0001F600" * 40, 40).encode()) <= 120
    assert passes._cap("\u20ac" * 40, 40, 20).endswith("\u2026") and len(passes._cap("\u20ac" * 40, 40, 20).encode()) <= 20
    assert passes._cap("kort", 40) == "kort"


def test_broken_google_key_is_a_404_without_detail(env, monkeypatch, keys):
    sold(env)
    wallet_on(monkeypatch, "live")
    bad = {**google_secret(keys), "private_key": "-----BEGIN PRIVATE KEY-----\nnot a key\n-----END PRIVATE KEY-----\n"}
    monkeypatch.setenv(passes.GOOGLE_SECRET_ENV, _secret("google-bad", bad))
    response = get_pass(env, first_ticket(env), "google")
    assert response["statusCode"] == 404
    assert "PEM" not in response["body"] and "key" not in response["body"].lower()


def test_broken_apple_pem_is_a_404(env, monkeypatch, keys):
    sold(env)
    wallet_on(monkeypatch, "live")
    monkeypatch.setenv(passes.APPLE_SECRET_ENV, _secret("apple-bad", {**apple_secret(keys), "certificatePem": "garbage"}))
    assert get_pass(env, first_ticket(env), "apple")["statusCode"] == 404


def test_secret_gone_after_cache_expiry_is_a_404(env, monkeypatch, keys):
    sold(env)
    wallet_on(monkeypatch, "live")
    arn = _secret("google-gone", google_secret(keys))
    monkeypatch.setenv(passes.GOOGLE_SECRET_ENV, arn)
    assert get_pass(env, first_ticket(env), "google")["statusCode"] == 302
    boto3.client("secretsmanager", region_name=REGION).delete_secret(SecretId=arn, ForceDeleteWithoutRecovery=True)
    passes._secret_cache.clear()  # the 5 minute cache ran out
    assert get_pass(env, first_ticket(env), "google")["statusCode"] == 404


def test_secret_reads_are_cached(env, monkeypatch, keys):
    wallet_on(monkeypatch, "live")
    monkeypatch.setenv(passes.GOOGLE_SECRET_ENV, _secret("google", google_secret(keys)))
    reads = []
    real = boto3.client

    def counting(name, *a, **kw):
        client = real(name, *a, **kw)
        if name == "secretsmanager":
            reads.append(name)
        return client
    monkeypatch.setattr(passes.boto3, "client", counting)
    for _ in range(3):
        assert passes.provider().available()["google"] is True
    assert len(reads) == 1

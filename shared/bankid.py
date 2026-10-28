"""BankID's own RP API v6 (mutual TLS) and the signing-provider secret.

The secret (SIGNING_PROVIDER_SECRET_ARN, JSON) picks the catering signing
provider in shared/signing.py, which is what the handlers use:

    {"provider": "bankid_direct" (or the older "bankid"), "environment": "test" | "prod",
     "certPem": "...", "keyPem": "...", "caPem": "..."}
        BankID's RP API with the relying party's client certificate. "test"
        talks to BankID's public test environment (appapi2.test.bankid.com).

    {"provider": "simulator"}
        Dev only (refused when ENVIRONMENT=prod), shared/signing.py.

    {"provider": "scrive", "baseUrl", "apiToken", "apiSecret", "accessToken", "accessSecret"}
        Scrive Document API v2, shared/signing_scrive.py (testbed refused in prod).

    start(end_user_ip, visible_text, document_hash)
        -> {"orderRef", "autoStartToken", "qrStartToken", "qrStartSecret", "startedAt"}
    collect(order)
        -> {"status": "pending" | "failed" | "complete", "hintCode",
            "completion": {"name", "personalNumber", "signature", "ocspResponse", ...}}
    qr_data(order)  -> the animated QR string for "now" (changes every second)
"""

import base64
import hashlib
import hmac
import json
import os
import ssl
import tempfile
import time
import urllib.error
import urllib.request

import boto3

PROVIDERS = ("bankid", "bankid_direct", "simulator", "scrive")
_HOSTS = {"test": "https://appapi2.test.bankid.com", "prod": "https://appapi2.bankid.com"}
_TIMEOUT = 15
_cache = {"config": None, "at": 0.0, "context": None}


class SigningUnavailable(Exception):
    """The provider can't be reached or isn't configured (503)."""


class SigningRejected(Exception):
    """The provider refused the request (e.g. an order is already running)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def reset_cache():
    _cache.update(config=None, at=0.0, context=None)


def _config():
    now = time.monotonic()
    if _cache["config"] is None or now - _cache["at"] > 300:
        arn = os.environ.get("SIGNING_PROVIDER_SECRET_ARN")
        if not arn:
            raise SigningUnavailable("SIGNING_PROVIDER_SECRET_ARN is not set")
        try:
            raw = boto3.client("secretsmanager").get_secret_value(SecretId=arn).get("SecretString")
            config = json.loads(raw or "")
        except Exception:  # noqa: BLE001 - an unset or malformed secret
            raise SigningUnavailable("signing provider is not configured") from None
        if not isinstance(config, dict) or config.get("provider") not in PROVIDERS:
            raise SigningUnavailable("signing provider is not configured")
        if config["provider"] == "simulator" and os.environ.get("ENVIRONMENT") == "prod":
            raise SigningUnavailable("the signing simulator only runs in dev")
        if config["provider"] == "scrive":
            from shared.signing_scrive import check_config
            check_config(config, os.environ.get("ENVIRONMENT"))
        _cache.update(config=config, at=now, context=None)
    return _cache["config"]


def provider():
    return _config()["provider"]


# --- BankID RP API v6 --------------------------------------------------------------------

def _context(config):
    if _cache["context"] is None:
        try:
            ctx = ssl.create_default_context(cadata=config["caPem"])
            with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
                f.write(config["certPem"].strip() + "\n" + config["keyPem"].strip() + "\n")
                path = f.name
            ctx.load_cert_chain(path)
            os.unlink(path)
        except (KeyError, ssl.SSLError, OSError, ValueError):
            raise SigningUnavailable("BankID certificate is not usable") from None
        _cache["context"] = ctx
    return _cache["context"]


def _call(path, body):
    config = _config()
    base = _HOSTS.get(config.get("environment"), _HOSTS["test"])
    request = urllib.request.Request(f"{base}/rp/v6.0/{path}", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT, context=_context(config)) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read() or b"{}")
        except ValueError:
            error = {}
        code = error.get("errorCode") or f"http_{exc.code}"
        if exc.code >= 500 or code in ("maintenance", "internalError", "requestTimeout"):
            raise SigningUnavailable(code) from None
        raise SigningRejected(code) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise SigningUnavailable(str(exc)) from None


def _b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def start(end_user_ip, visible_text, document_hash):
    started = int(time.time())
    result = _call("sign", {
        "endUserIp": end_user_ip or "127.0.0.1",
        "userVisibleData": _b64(visible_text[:40000]),
        "userVisibleDataFormat": "simpleMarkdownV1",
        "userNonVisibleData": _b64(f"sha256:{document_hash}"),
    })
    return {"orderRef": result["orderRef"], "autoStartToken": result["autoStartToken"],
            "qrStartToken": result["qrStartToken"], "qrStartSecret": result["qrStartSecret"],
            "startedAt": started}


def collect(order):
    result = _call("collect", {"orderRef": order["orderRef"]})
    out = {"status": result.get("status"), "hintCode": result.get("hintCode")}
    if result.get("status") == "complete":
        data = result.get("completionData") or {}
        user = data.get("user") or {}
        out["completion"] = {"name": user.get("name"), "personalNumber": user.get("personalNumber"),
                             "signature": data.get("signature"), "ocspResponse": data.get("ocspResponse"),
                             "bankIdIssueDate": data.get("bankIdIssueDate"),
                             "stepUp": data.get("stepUp"), "device": data.get("device")}
    return out


def cancel(order):
    try:
        _call("cancel", {"orderRef": order["orderRef"]})
    except (SigningRejected, SigningUnavailable):
        pass


def qr_data(order, now=None):
    """bankid.<qrStartToken>.<seconds since start>.<HMAC(qrStartSecret, seconds)>"""
    seconds = max(0, int((now or time.time()) - int(order["startedAt"])))
    code = hmac.new(str(order["qrStartSecret"]).encode(), str(seconds).encode(), hashlib.sha256).hexdigest()
    return f"bankid.{order['qrStartToken']}.{seconds}.{code}"


def mask_personal_number(value):
    if not isinstance(value, str) or len(value) < 8:
        return None
    return f"{value[:8]}-****"

"""Minimal Stripe REST client (no SDK): the few v1 calls table reservations
need, made ON the restaurant's connected account (Stripe-Account header,
direct charges), with idempotency keys and a pinned API version.

ENV_VARS:
    STRIPE_SECRET_ARN   -- Secrets Manager secret {"apiKey": "sk_..."} (platform key)
    STRIPE_API_VERSION  -- Stripe-Version header, e.g. 2026-07-29.dahlia

Errors:
    StripeError        -- Stripe answered 4xx: a real decision (card declined,
                          authentication required, invalid request). Carries
                          type / code / decline_code.
    StripeUnavailable  -- network error, timeout, 429 or 5xx: nothing is known
                          about the outcome; retry with the SAME idempotency key.
"""

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3

_API = "https://api.stripe.com"
_TIMEOUT = 20
_key_cache = {"key": None}


class StripeError(Exception):
    def __init__(self, status, error):
        error = error if isinstance(error, dict) else {}
        super().__init__(error.get("message") or f"stripe error {status}")
        self.status = status
        self.type = error.get("type")
        self.code = error.get("code")
        self.decline_code = error.get("decline_code")
        self.payment_intent = error.get("payment_intent")


class StripeUnavailable(Exception):
    """The outcome is unknown - retry with the same idempotency key."""


def reset_cache():
    _key_cache["key"] = None


def _api_key():
    if _key_cache["key"] is None:
        arn = os.environ.get("STRIPE_SECRET_ARN")
        if not arn:
            raise StripeUnavailable("STRIPE_SECRET_ARN is not set")
        raw = boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"]
        try:
            key = json.loads(raw)["apiKey"]
        except (ValueError, KeyError, TypeError):
            raise StripeUnavailable("platform Stripe secret is malformed") from None
        if not isinstance(key, str) or not key.startswith(("sk_", "rk_")):
            raise StripeUnavailable("platform Stripe secret is malformed")
        _key_cache["key"] = key
    return _key_cache["key"]


def _flatten(params, prefix=""):
    """Stripe's form encoding: a[b]=1, list[]=x, metadata[k]=v."""
    pairs = []
    for key, value in params.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if value is None:
            continue
        if isinstance(value, dict):
            pairs.extend(_flatten(value, name))
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, dict):
                    pairs.extend(_flatten(item, f"{name}[]"))
                else:
                    pairs.append((f"{name}[]", _scalar(item)))
        else:
            pairs.append((name, _scalar(value)))
    return pairs


def _scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def request(method, path, params=None, *, account=None, idempotency_key=None):
    """One Stripe call. Returns the decoded JSON object."""
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Stripe-Version": os.environ.get("STRIPE_API_VERSION", ""),
    }
    if account:
        headers["Stripe-Account"] = account
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    url = f"{_API}{path}"
    data = None
    encoded = urllib.parse.urlencode(_flatten(params or {}))
    if method == "GET":
        if encoded:
            url = f"{url}?{encoded}"
    else:
        data = encoded.encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except ValueError:
            body = {}
        if exc.code == 429 or exc.code >= 500:
            raise StripeUnavailable(f"stripe {exc.code}") from None
        raise StripeError(exc.code, body.get("error")) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise StripeUnavailable(str(exc)) from None


# --- webhooks ---------------------------------------------------------------------

_TOLERANCE_SECONDS = 300


class InvalidSignature(Exception):
    pass


def verify_webhook(payload: bytes, header: str, secret: str, *, now=None, tolerance=_TOLERANCE_SECONDS):
    """Stripe-Signature check (t=..., v1=...). Returns the parsed event."""
    if not header or not secret:
        raise InvalidSignature("missing signature")
    parts = {}
    signatures = []
    for item in header.split(","):
        key, _, value = item.strip().partition("=")
        if key == "v1":
            signatures.append(value)
        elif key == "t":
            parts["t"] = value
    try:
        timestamp = int(parts["t"])
    except (KeyError, ValueError):
        raise InvalidSignature("missing timestamp") from None
    now = int(now if now is not None else time.time())
    if abs(now - timestamp) > tolerance:
        raise InvalidSignature("timestamp outside tolerance")
    expected = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode("utf-8") + payload,
                        hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in signatures):
        raise InvalidSignature("signature mismatch")
    try:
        return json.loads(payload)
    except ValueError:
        raise InvalidSignature("body is not JSON") from None

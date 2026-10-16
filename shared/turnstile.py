"""Cloudflare Turnstile: proof that an online booking came from a person at
the booking form, not from a script (developers.cloudflare.com/turnstile).

The guest site renders the platform's widget (its site key comes from
GET /site-config) and sends the widget's token with the booking as
``turnstileToken``. The token is checked here with Cloudflare's siteverify
API. Tokens are single-use and live 5 minutes, so the site asks the widget
for a new one after every attempt.

ENV_VARS:
    TURNSTILE_REQUIRED   -- "true" when the sites show the widget (the
                            platform's site key is configured). Otherwise no
                            token is asked for.
    TURNSTILE_SECRET_ARN -- Secrets Manager secret {"secretKey": "..."}: the
                            widget's secret key (the platform has one widget).

Errors:
    ValueError  -- no token, or Cloudflare says it's not valid (400).
    Unavailable -- the secret or Cloudflare can't be reached, or the secret
                   is wrong: nothing is known about the visitor (503).
"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid

import boto3
from botocore.exceptions import BotoCoreError, ClientError

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
# The widget on the booking form passes this action; a token solved for
# another form (with another action) isn't accepted for a booking. The
# order form passes ORDER_ACTION.
ACTION = "booking"
ORDER_ACTION = "order"
CATERING_ACTION = "catering"
_TIMEOUT = 5
_TOKEN_MAX = 2048
_cache = {"secret": None}
_CONFIG_ERRORS = frozenset({"missing-input-secret", "invalid-input-secret"})

logger = logging.getLogger(__name__)


class Unavailable(Exception):
    """The check couldn't be made - nothing is known about the visitor."""


def required():
    return os.environ.get("TURNSTILE_REQUIRED") == "true"


def reset_cache():
    _cache["secret"] = None


def _secret():
    if _cache["secret"] is None:
        arn = os.environ.get("TURNSTILE_SECRET_ARN")
        if not arn:
            raise Unavailable("TURNSTILE_SECRET_ARN is not set")
        try:
            raw = boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"]
            key = json.loads(raw)["secretKey"]
        except (BotoCoreError, ClientError, KeyError, TypeError, ValueError):
            raise Unavailable("turnstile secret unavailable") from None
        if not isinstance(key, str) or not key.strip():
            raise Unavailable("turnstile secret is empty")
        _cache["secret"] = key.strip()
    return _cache["secret"]


def _post(payload):
    request = urllib.request.Request(
        VERIFY_URL,
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def verify(token, remote_ip=None, action=ACTION):
    """Raises ValueError when the token is missing or not valid and
    Unavailable when the check can't be made; returns when it passed.
    ``action``: the form the widget was solved on ("booking", "order")."""
    if not isinstance(token, str) or not token.strip():
        raise ValueError("turnstile token is required")
    if len(token) > _TOKEN_MAX:
        raise ValueError("turnstile verification failed")
    payload = {
        "secret": _secret(),
        "response": token.strip(),
        # The same key on the retry: Cloudflare answers it like the first
        # call instead of reporting the token as already used.
        "idempotency_key": str(uuid.uuid4()),
    }
    if remote_ip:
        payload["remoteip"] = remote_ip
    result = None
    for attempt in range(2):
        try:
            result = _post(payload)
        except (urllib.error.URLError, OSError, ValueError):
            if attempt == 1:
                raise Unavailable("turnstile unreachable") from None
            continue
        codes = result.get("error-codes") if isinstance(result, dict) else None
        if attempt == 0 and isinstance(codes, list) and "internal-error" in codes:
            continue
        break
    if not isinstance(result, dict):
        raise Unavailable("turnstile answered something unexpected")
    codes = result.get("error-codes") if isinstance(result.get("error-codes"), list) else []
    if result.get("success") is True:
        if result.get("action") not in (None, "", action):
            logger.warning(json.dumps({"turnstile": "wrong_action", "action": result.get("action")}))
            raise ValueError("turnstile verification failed")
        return
    logger.warning(json.dumps({"turnstile": "rejected", "codes": codes}))
    if _CONFIG_ERRORS & set(codes) or "internal-error" in codes:
        raise Unavailable("turnstile check failed: " + ", ".join(codes))
    if "timeout-or-duplicate" in codes:
        raise ValueError("turnstile token expired")
    raise ValueError("turnstile verification failed")

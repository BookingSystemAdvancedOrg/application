"""platform-stripe-webhook

TRIGGER:
    Lambda Function URL (POST), registered by infrastructure payments/stripe
    as two Stripe endpoints:
      <url>connect  connected-account events (connect = true)
      <url>billing  the platform account's own subscription events
    Auth: NONE - trust comes ONLY from the Stripe-Signature header, verified
    against that endpoint's own signing secret before the body is used.

PURPOSE:
    Keeps the tenant's PROFILE.stripe.* truthful without anyone polling
    Stripe - this is what turns "Stripe är inte klart" into "klart" in the
    admin app once the restaurant finishes Stripe's onboarding form, and
    what the card guarantee checks (chargesEnabled) before asking a guest
    for a card.

      account.updated                   -> chargesEnabled, payoutsEnabled,
                                           detailsSubmitted, requirementsDue
      account.application.deauthorized  -> disconnected, charges/payouts off

    The connected account resolves to its tenant through the
    STRIPE_ACCOUNT#<acct> / TENANT row written by the onboarding workflow,
    and the write is conditional on PROFILE.stripe.accountId still being
    that account - an event can only ever touch the tenant that owns it.
    Events older than the last one applied are ignored (Stripe does not
    guarantee order). Test-mode events reaching prod, and live ones
    reaching dev, are ignored.

    /billing (SaaS plans) is verified and acknowledged; plans are not sold
    through Stripe Billing yet (M6).

    Answers 200 for anything handled or deliberately ignored, 400 for a bad
    signature, 404 for an unknown path, 500 for a transient failure (Stripe
    retries with backoff for up to three days).

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    TENANT_TABLE_NAME -- tenant table (PROFILE + STRIPE_ACCOUNT# rows)
    CONNECT_WEBHOOK_SECRET_ARN -- whsec_ of the /connect endpoint (plain string)
    BILLING_WEBHOOK_SECRET_ARN -- whsec_ of the /billing endpoint (plain string)
    STRIPE_SECRET_ARN / STRIPE_API_VERSION -- unused for now (no API calls)

AWS RESOURCE ACCESS:
    Tenant table GetItem (STRIPE_ACCOUNT# rows) and UpdateItem (PROFILE);
    GetSecretValue on the two signing secrets.
"""

import base64
import json
import logging
import os
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from shared import stripe_accounts
from shared import stripe_client as stripe

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_SECRET_CACHE_SECONDS = 300
_SECRET_ENV = {
    "/connect": "CONNECT_WEBHOOK_SECRET_ARN",
    "/billing": "BILLING_WEBHOOK_SECRET_ARN",
}
_secrets = {}


def _signing_secret(path):
    now = time.monotonic()
    cached = _secrets.get(path)
    if cached and now - cached[1] <= _SECRET_CACHE_SECONDS:
        return cached[0]
    value = boto3.client("secretsmanager").get_secret_value(
        SecretId=os.environ[_SECRET_ENV[path]])["SecretString"].strip()
    _secrets[path] = (value, now)
    return value


def reset_cache():
    _secrets.clear()


def _respond(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body)}


def _payload(event):
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body)
    return body.encode("utf-8")


def _header(event, name):
    for key, value in (event.get("headers") or {}).items():
        if key.lower() == name:
            return value
    return None


def _path(event):
    path = event.get("rawPath") or (event.get("requestContext") or {}).get("http", {}).get("path") or ""
    return path.rstrip("/") or "/"


def _connect(evt):
    kind = evt.get("type")
    obj = (evt.get("data") or {}).get("object") or {}
    account_id = evt.get("account") or (obj.get("id") if kind == "account.updated" else None)
    created = evt.get("created")
    if not isinstance(account_id, str) or not account_id.startswith("acct_") or not isinstance(created, int):
        return "ignored"

    if kind == "account.updated":
        state = stripe_accounts.state_of(obj)
    elif kind == "account.application.deauthorized":
        state = stripe_accounts.DISCONNECTED
    else:
        return "ignored"

    tenant_id = stripe_accounts.tenant_of_account(account_id)
    if tenant_id is None:
        logger.warning(json.dumps({"ignored": "unknown_account", "account": account_id}))
        return "ignored:unknown_account"
    applied = stripe_accounts.store(tenant_id, account_id, state, created)
    return "applied" if applied else "ignored:stale"


def handler(event, context):
    path = _path(event)
    if path not in _SECRET_ENV:
        return _respond(404, {"error": "not found"})
    try:
        evt = stripe.verify_webhook(_payload(event), _header(event, "stripe-signature"),
                                    _signing_secret(path))
    except stripe.InvalidSignature as exc:
        logger.warning(json.dumps({"rejected": str(exc), "path": path}))
        return _respond(400, {"error": "invalid signature"})
    except (BotoCoreError, ClientError):
        logger.exception("signing secret unavailable")
        return _respond(500, {"error": "temporarily unavailable"})

    live_expected = os.environ.get("ENVIRONMENT") == "prod"
    if bool(evt.get("livemode")) != live_expected:
        logger.warning(json.dumps({"ignored": "livemode_mismatch", "id": evt.get("id")}))
        return _respond(200, {"received": True, "result": "ignored:livemode"})

    if path == "/billing":
        logger.info(json.dumps({"event": evt.get("id"), "type": evt.get("type"), "result": "ignored:billing"}))
        return _respond(200, {"received": True, "result": "ignored"})

    try:
        result = _connect(evt)
    except (BotoCoreError, ClientError):
        logger.exception(json.dumps({"event": evt.get("id"), "type": evt.get("type")}))
        return _respond(500, {"error": "temporarily unavailable"})
    logger.info(json.dumps({"event": evt.get("id"), "type": evt.get("type"),
                            "account": evt.get("account"), "result": result}))
    return _respond(200, {"received": True, "result": result})

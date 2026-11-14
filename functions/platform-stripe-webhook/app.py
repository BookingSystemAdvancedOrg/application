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

    /billing: the platform account's invoices to the restaurants, built in
    advance by platform-tenants for each calendar month (the monthly price;
    SMS are included): charged to the saved card, or emailed. No Stripe
    subscriptions are used; a customer.subscription.* event is acknowledged
    and ignored.

      invoice.finalized/paid/payment_failed/voided/marked_uncollectible/sent
                                        -> finance table: the invoice row and
                                           the day's invoicedSek / paidSek
                                           (ex VAT), past-due bookkeeping on
                                           PROFILE.billing (cleared only when
                                           no failed invoice is left)
      invoice.payment_failed            -> also an immediate email to the
                                           operators (ALERT_TOPIC_ARN), once
                                           per invoice and attempt
      invoice.payment_action_required   -> the ledger row and an operator
                                           alert (the card needs 3-D Secure)
      checkout.session.completed        -> a card added in Booqy Admin or from
        (mode setup)                       the console's card link becomes the
                                           customer's default (Stripe call,
                                           idempotency key default-pm-<seti>)
      customer.updated                  -> the default card on PROFILE.billing
      payment_method.attached           -> the default card when there is none
      credit_note.created               -> finance table: creditedSek

    The customer resolves to its tenant through STRIPE_CUSTOMER#<cus> /
    TENANT (written by the console), and every PROFILE write is conditional
    on billing.customerId still being that customer. Revenue is booked once
    per invoice and kind (shared/billing.py), so redeliveries are harmless.

    Answers 200 for anything handled or deliberately ignored, 400 for a bad
    signature, 404 for an unknown path, 500 for a transient failure (Stripe
    retries with backoff for up to three days).

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    TENANT_TABLE_NAME -- tenant table (PROFILE + STRIPE_ACCOUNT# rows)
    CONNECT_WEBHOOK_SECRET_ARN -- whsec_ of the /connect endpoint (plain string)
    BILLING_WEBHOOK_SECRET_ARN -- whsec_ of the /billing endpoint (plain string)
    FINANCE_TABLE_NAME -- finance table (invoice ledger, revenue per day)
    STRIPE_SECRET_ARN / STRIPE_API_VERSION -- the platform key: setting the
        default card after a Checkout setup, reading a card's brand and last4
    ALERT_TOPIC_ARN -- platform alerts topic (operators' email)
    PLATFORM_ADMIN_APP_URL -- Booqy Console base URL (link in the alert)

AWS RESOURCE ACCESS:
    Tenant table GetItem (STRIPE_ACCOUNT#, STRIPE_CUSTOMER# rows, PROFILE
    name) and UpdateItem (PROFILE); finance table GetItem/PutItem/UpdateItem;
    GetSecretValue on the two signing secrets and the platform Stripe key;
    sns:Publish on the platform alerts topic.
"""

import base64
import json
import logging
import os
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from shared import billing, stripe_accounts
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


def _sek_text(minor):
    amount = billing.sek(minor or 0)
    whole = f"{int(amount):,}".replace(",", " ")
    cents = int((amount - int(amount)) * 100)
    return f"{whole},{cents:02d} kr" if cents else f"{whole} kr"


def _alert_ops(tenant_id, invoice, step, headline):
    """One email to the operators about one invoice, claimed on its ledger
    row first (never twice for the same invoice and step); released when
    publishing fails, so Stripe's redelivery tries again."""
    topic = os.environ.get("ALERT_TOPIC_ARN")
    if not topic or not billing.claim_alert(tenant_id, invoice, step):
        return False
    name, slug = billing.tenant_name(tenant_id)
    number = invoice.get("number") or invoice.get("id")
    due = billing._iso(invoice.get("due_date"))
    late = 0
    if due:
        late = max(0, (time.time() - invoice["due_date"]) // 86400)
    base = (os.environ.get("PLATFORM_ADMIN_APP_URL") or "").rstrip("/")
    intent = invoice.get("payment_intent")
    error = (invoice.get("last_finalization_error") or {}).get("message") or (
        (intent.get("last_payment_error") or {}).get("message") if isinstance(intent, dict) else None)
    lines = [
        f"{name}: {headline}", "",
        f"Restaurant: {name} ({slug or tenant_id})",
        f"Invoice: {number}",
        f"Amount due: {_sek_text(invoice.get('amount_remaining') or invoice.get('amount_due'))} incl. VAT",
        f"Due date: {(due or '')[:10] or 'now (card)'}",
        f"Days overdue: {int(late)}",
        f"Attempt: {invoice.get('attempt_count') or 1}" + (f", next retry by Stripe {billing._iso(invoice['next_payment_attempt'])}"
                                                           if invoice.get("next_payment_attempt") else ", no retry left"),
        *( [f"Stripe says: {error}"] if error else []),
        "", f"Customer in Booqy Console: {base}/tenants/{tenant_id}?tab=billing",
        *( [f"Invoice: {invoice['hosted_invoice_url']}"] if invoice.get("hosted_invoice_url") else []),
    ]
    subject = f"[{os.environ.get('ENVIRONMENT', 'dev')}] Booqy: {name} {headline} ({number})"[:100]
    try:
        boto3.client("sns").publish(TopicArn=topic, Subject=subject, Message="\n".join(lines))
    except (BotoCoreError, ClientError):
        billing.unclaim_alert(tenant_id, invoice, step)
        raise
    return True


def _set_default_card(tenant_id, customer_id, payment_method_id, key):
    """Makes the card the customer's default for invoices (platform key)."""
    stripe.request("POST", f"/v1/customers/{customer_id}",
                   {"invoice_settings": {"default_payment_method": payment_method_id}}, idempotency_key=key)
    pm = stripe.request("GET", f"/v1/payment_methods/{payment_method_id}")
    billing.record_payment_method(tenant_id, customer_id, pm)


def _billing(evt):
    kind = evt.get("type") or ""
    obj = (evt.get("data") or {}).get("object") or {}
    created = evt.get("created")
    if not isinstance(created, int):
        return "ignored"
    customer_id = obj.get("id") if kind == "customer.updated" else obj.get("customer")
    if isinstance(customer_id, dict):
        customer_id = customer_id.get("id")
    tenant_id = billing.tenant_of_customer(customer_id)
    if tenant_id is None:
        logger.warning(json.dumps({"ignored": "unknown_customer", "customer": customer_id, "type": kind}))
        return "ignored:unknown_customer"
    if kind.startswith("invoice."):
        result = billing.record_invoice(tenant_id, obj, created)
        when = billing.now_iso()
        if kind == "invoice.paid":
            billing.mark_payment(tenant_id, customer_id, paid=True, when=when, invoice_id=obj.get("id"))
        elif kind == "invoice.payment_failed":
            billing.mark_payment(tenant_id, customer_id, paid=False, when=when, invoice_id=obj.get("id"))
            if _alert_ops(tenant_id, obj, f"Fail{obj.get('attempt_count') or 1}", "card payment failed"):
                result += ":alerted"
        elif kind == "invoice.payment_action_required":
            if _alert_ops(tenant_id, obj, "Action", "card payment needs the restaurant's approval (3-D Secure)"):
                result += ":alerted"
        return result
    if kind == "checkout.session.completed":
        if obj.get("mode") != "setup" or not obj.get("setup_intent"):
            return "ignored"
        seti = obj["setup_intent"] if isinstance(obj["setup_intent"], str) else obj["setup_intent"].get("id")
        intent = stripe.request("GET", f"/v1/setup_intents/{seti}")
        pm = intent.get("payment_method")
        pm = pm.get("id") if isinstance(pm, dict) else pm
        if not pm:
            return "ignored:no_payment_method"
        _set_default_card(tenant_id, customer_id, pm, f"default-pm-{seti}")
        return "applied:card"
    if kind == "customer.updated":
        default = (obj.get("invoice_settings") or {}).get("default_payment_method")
        default = default.get("id") if isinstance(default, dict) else default
        if not default:
            billing.record_payment_method(tenant_id, customer_id, None)
            return "applied:no_card"
        pm = stripe.request("GET", f"/v1/payment_methods/{default}")
        billing.record_payment_method(tenant_id, customer_id, pm)
        return "applied:card"
    if kind == "payment_method.attached":
        if obj.get("type") != "card" or not obj.get("id"):
            return "ignored"
        customer = stripe.request("GET", f"/v1/customers/{customer_id}")
        if (customer.get("invoice_settings") or {}).get("default_payment_method"):
            return "ignored:has_default"
        _set_default_card(tenant_id, customer_id, obj["id"], f"default-pm-{obj['id']}")
        return "applied:card"
    if kind == "credit_note.created":
        return billing.record_credit_note(tenant_id, obj, created)
    return "ignored"


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

    try:
        result = _billing(evt) if path == "/billing" else _connect(evt)
    except (stripe.StripeUnavailable, stripe.StripeError):
        logger.exception(json.dumps({"event": evt.get("id"), "type": evt.get("type"), "stripe": True}))
        return _respond(500, {"error": "temporarily unavailable"})
    except (BotoCoreError, ClientError):
        logger.exception(json.dumps({"event": evt.get("id"), "type": evt.get("type")}))
        return _respond(500, {"error": "temporarily unavailable"})
    logger.info(json.dumps({"event": evt.get("id"), "type": evt.get("type"),
                            "account": evt.get("account"), "result": result}))
    return _respond(200, {"received": True, "result": result})

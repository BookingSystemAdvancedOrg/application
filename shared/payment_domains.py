"""Payment method domains on a restaurant's connected account.

The site's own payment step (Stripe's Payment Element, Checkout Sessions
ui_mode "elements") only offers Apple Pay, Google Pay, Klarna and Link on
domains registered for the account the charge runs on - for direct charges
that is the restaurant's connected account (platform key + Stripe-Account).
Hosted Checkout never needed this; the embedded step does.

ensure() registers every host of the tenant's site that the account does
not have yet, once per account and warm Lambda. Best effort: a failure is
logged and the payment goes on (cards always work); the next cold start
tries again. A domain the account has but disabled is left disabled.
"""

import json
import logging

from botocore.exceptions import BotoCoreError, ClientError

from shared import stripe_client as stripe

logger = logging.getLogger()

_done = set()


def reset_cache():
    _done.clear()


def ensure(account, hosts):
    """hosts: a list, or a function returning one (called only when the
    account hasn't been checked in this Lambda yet)."""
    if not account or account in _done:
        return
    try:
        wanted = hosts() if callable(hosts) else hosts
        for host in wanted or []:
            if host in ("localhost", "127.0.0.1"):
                continue
            found = stripe.request("GET", "/v1/payment_method_domains", {"domain_name": host, "limit": 1},
                                   account=account)
            if not found.get("data"):
                stripe.request("POST", "/v1/payment_method_domains", {"domain_name": host},
                               account=account, idempotency_key=f"pmd-{account}-{host}")
                logger.info(json.dumps({"paymentDomain": "registered", "account": account, "host": host}))
        _done.add(account)
    except (stripe.StripeError, stripe.StripeUnavailable, BotoCoreError, ClientError) as exc:
        logger.warning(json.dumps({"paymentDomain": "failed", "account": account,
                                   "error": getattr(exc, "code", None) or type(exc).__name__}))

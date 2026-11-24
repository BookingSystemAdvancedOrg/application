"""Payment method domains on a restaurant's connected account.

The site's own payment steps (Stripe's Payment Element: the order and
catering pay step, the booking's card guarantee) only offer Apple Pay,
Google Pay and Klarna on domains registered for the account the charge
runs on - for direct charges that is the restaurant's connected account
(platform key + Stripe-Account; Stripe's Dashboard can't do it for
connected accounts). Every host counts on its own: the slug address
(<slug>.booqy.se, <slug>.dev.booqy.se), the restaurant's own domain and
its www. A domain registered in live mode is registered in the sandbox
too; dev's sandbox registers its own. Stripe does the Apple merchant
validation itself (no association file on the site). Hosted Checkout
never needed this; the embedded steps do.

ensure() registers every host of the tenant's site that the account does
not have yet, at most once per account every RECHECK_SECONDS in a warm
Lambda (a domain attached later is picked up within that time). Best
effort: a failure is logged and the payment goes on (cards always work);
the next check tries again. A domain the account has but disabled is left
disabled; one whose Apple Pay or Google Pay check failed (the site was not
answering yet) is validated again.
"""

import json
import logging
import time

from botocore.exceptions import BotoCoreError, ClientError

from shared import stripe_client as stripe

logger = logging.getLogger()

RECHECK_SECONDS = 6 * 3600
_done = {}  # account -> time.monotonic() of the last complete check


def reset_cache():
    _done.clear()


def ensure(account, hosts):
    """hosts: a list, or a function returning one (called only when the
    account hasn't been checked in this Lambda yet)."""
    if not account or (account in _done and time.monotonic() - _done[account] < RECHECK_SECONDS):
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
                continue
            domain = found["data"][0]
            if domain.get("enabled") and any((domain.get(w) or {}).get("status") == "inactive"
                                             for w in ("apple_pay", "google_pay")):
                # Registered before the site answered (a new domain's
                # certificate): Stripe checks it again.
                stripe.request("POST", f"/v1/payment_method_domains/{domain['id']}/validate", {},
                               account=account)
                logger.info(json.dumps({"paymentDomain": "revalidated", "account": account, "host": host}))
        _done[account] = time.monotonic()
    except (stripe.StripeError, stripe.StripeUnavailable, BotoCoreError, ClientError) as exc:
        logger.warning(json.dumps({"paymentDomain": "failed", "account": account,
                                   "error": getattr(exc, "code", None) or type(exc).__name__}))

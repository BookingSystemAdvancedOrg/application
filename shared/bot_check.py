"""Bot check on the guest forms: proof that a booking, order, catering
request, gift card, waiting list entry, ticket purchase or newsletter
sign-up came from a person on the restaurant's website, not from a script.

The check itself runs in AWS WAF (infrastructure network/waf-sites), on the
CloudFront distributions that serve the restaurant sites. The sites send
these form posts to /api/* on their own domain; the web ACL lets a write
through only with a valid token from its JavaScript integration (a real
browser), and CloudFront then adds the x-booqy-waf origin header. A script
that calls the API Gateway address directly never gets that header.

Here we only check that header. Nothing to call, nothing to cache: the
check costs no time and can't be unavailable at runtime.

ENV_VARS:
    BOT_CHECK_ENFORCED -- "true": refuse guest form posts without the header
                          (infrastructure var.bot_check_enforced). Otherwise
                          they pass, and the ones that skipped the WAF are
                          logged (bot_check: not_through_waf) so the switch
                          can be turned on once no real site does that.
    SITE_WAF_SECRET    -- the shared secret CloudFront sends as x-booqy-waf.

Errors:
    ValueError  -- enforced and the post did not come through the WAF (400,
                   "bot_check_failed").
    Unavailable -- enforced but SITE_WAF_SECRET is missing: a deployment
                   fault, nothing is known about the visitor (503).
"""

import hmac
import json
import logging
import os

from shared import http

HEADER = "x-booqy-waf"
FAILED = "bot_check_failed"

logger = logging.getLogger(__name__)


class Unavailable(Exception):
    """The check can't be made - nothing is known about the visitor."""


def enforced():
    return os.environ.get("BOT_CHECK_ENFORCED") == "true"


def passed(event):
    """True when the request came through a restaurant site's web ACL."""
    secret = os.environ.get("SITE_WAF_SECRET") or ""
    sent = http.header(event, HEADER) or ""
    if not secret or not sent:
        return False
    return hmac.compare_digest(sent.encode("utf-8"), secret.encode("utf-8"))


def verify(event, form):
    """Returns when the post may go on. ``form`` names the form for the log
    ("booking", "order", ...). Raises ValueError(FAILED) when the check is
    enforced and the post skipped the WAF, Unavailable when it is enforced
    but not configured."""
    if passed(event):
        return
    if not enforced():
        logger.info(json.dumps({"bot_check": "not_through_waf", "form": form}))
        return
    if not os.environ.get("SITE_WAF_SECRET"):
        raise Unavailable("SITE_WAF_SECRET is not set")
    logger.warning(json.dumps({"bot_check": "refused", "form": form}))
    raise ValueError(FAILED)

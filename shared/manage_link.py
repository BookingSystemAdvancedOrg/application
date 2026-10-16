"""The guest's manage link: /bokning/<locationId>/<reservationId>#<token>.

The token is HMAC-SHA256 over the booking's identity with a platform key
in Secrets Manager - never stored anywhere. Every component holding the
key can rebuild the link from the booking alone: the create function
(returned to the guest once), the notification function (confirmation,
reminder and change emails/SMS) and the guest routes (verify). That is
what lets a reminder sent days later still carry a working link.

    token = base64url(HMAC(key, "reservation-manage|v1|<tenant>|<location>|<reservation>|<linkVersion>"))

Bumping a booking's ``linkVersion`` revokes every link sent for it so far.
Bookings made before signed links (they carry ``manageTokenHash``) keep
working with their original random token.

ENV_VARS:
    RESERVATION_LINK_KEY_SECRET_ARN -- Secrets Manager secret (plain string)
"""

import base64
import hashlib
import hmac
import os
import time

import boto3

_CACHE_SECONDS = 300


class LinkKeyUnavailable(RuntimeError):
    """The signing key is missing or malformed - answer 503, never guess."""


_cache ={"key": None, "at": 0.0}


def _key():
    now = time.monotonic()
    if _cache["key"] is None or now - _cache["at"] > _CACHE_SECONDS:
        secret_id = os.environ.get("RESERVATION_LINK_KEY_SECRET_ARN")
        if not secret_id:
            raise LinkKeyUnavailable("RESERVATION_LINK_KEY_SECRET_ARN is not set")
        secret = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id).get("SecretString")
        if not isinstance(secret, str) or len(secret) < 32:
            raise LinkKeyUnavailable("reservation link signing key is missing or too short")
        _cache.update(key=secret.encode("utf-8"), at=now)
    return _cache["key"]


def reset_cache():
    _cache.update(key=None, at=0.0)


def _version(item):
    try:
        return int(item.get("linkVersion", 1))
    except (TypeError, ValueError):
        return 1


def token_for(item):
    message = "|".join((
        "reservation-manage", "v1", str(item["tenantId"]), str(item["locationId"]),
        str(item["reservationId"]), str(_version(item)),
    )).encode("utf-8")
    digest = hmac.new(_key(), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def matches(item, token):
    """Constant-time check of a guest's token against the booking."""
    if not isinstance(token, str) or not token or len(token) > 256:
        return False
    legacy = item.get("manageTokenHash")
    if isinstance(legacy, str):
        return hmac.compare_digest(legacy, hashlib.sha256(token.encode("utf-8")).hexdigest())
    if not item.get("tenantId"):
        return False
    return hmac.compare_digest(token_for(item), token)


def url(site_base, item, *, slug=None):
    """Absolute manage link, or None when the site's address is unknown.
    ``slug`` is for sites that can't tell the restaurant from their host
    (dev on *.cloudfront.net): the site reads ``?restaurang=<slug>``."""
    if not site_base:
        return None
    query = f"?restaurang={slug}" if slug else ""
    return (f"{site_base.rstrip('/')}/bokning/{item['locationId']}/{item['reservationId']}"
            f"{query}#{token_for(item)}")

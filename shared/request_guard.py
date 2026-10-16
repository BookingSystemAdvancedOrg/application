"""Per-client limits on public writes (online bookings).

Each limit counts one client's attempts - an IP address, a phone number or
an e-mail address - in a fixed time window, in a small DynamoDB table of
counters. Past the limit the request is refused (429) until the window
ends. The identifiers are stored only as hashes, and the counters expire
with their window (TTL).

An attempt that doesn't end in a booking can be given back (``refund``):
the phone and e-mail limits count bookings, so a guest who loses a table to
someone else a few times isn't locked out for the day.

Fail open: if the table can't be reached the request goes through - the
Turnstile check stays the main defence - and the error is logged.

ENV_VARS:
    REQUEST_GUARD_TABLE_NAME -- the counters table. Unset: no limits.
"""

import hashlib
import json
import logging
import os

from botocore.exceptions import BotoCoreError, ClientError

from shared.dynamo import table

logger = logging.getLogger(__name__)


class Limited(Exception):
    """Too many attempts; ``retry_after`` seconds until the window ends."""

    def __init__(self, kind, retry_after):
        super().__init__(f"too many attempts ({kind})")
        self.kind = kind
        self.retry_after = max(1, int(retry_after))


def _key(kind, value, window, bucket):
    digest = hashlib.sha256(f"{kind}:{value}".encode("utf-8")).hexdigest()[:40]
    return f"{kind}#{digest}#{window}#{bucket}"


def check(limits, now):
    """``limits``: [(kind, value, max_attempts, window_seconds)]. Counts one
    attempt for each (an empty value is skipped) and raises Limited at the
    first one already at its maximum - the attempts this call counted before
    it are given back then. Returns the counters it counted (for
    ``refund``)."""
    name = os.environ.get("REQUEST_GUARD_TABLE_NAME")
    if not name:
        return []
    seconds = int(now.timestamp())
    counted = []
    for kind, value, max_attempts, window in limits:
        if not value:
            continue
        bucket = seconds // window
        window_end = (bucket + 1) * window
        key = _key(kind, value, window, bucket)
        try:
            table(name).update_item(
                Key={"PK": key},
                UpdateExpression="ADD attempts :one SET #ttl = if_not_exists(#ttl, :ttl)",
                ConditionExpression="attribute_not_exists(attempts) OR attempts < :max",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":one": 1,
                    ":max": max_attempts,
                    # Kept an hour past the window: TTL deletion isn't instant
                    # and the window end is what matters, not the row.
                    ":ttl": window_end + 3600,
                },
            )
            counted.append(key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                logger.warning(json.dumps({"limited": kind}))
                refund(counted)
                raise Limited(kind, window_end - seconds) from None
            logger.error(json.dumps({"guard": "unavailable", "kind": kind,
                                     "code": exc.response.get("Error", {}).get("Code")}))
        except BotoCoreError:
            logger.error(json.dumps({"guard": "unavailable", "kind": kind}))
    return counted


def refund(counted):
    """Gives back one attempt on each counter ``check`` returned (best
    effort; a counter that has expired is left alone)."""
    name = os.environ.get("REQUEST_GUARD_TABLE_NAME")
    if not name:
        return
    for key in counted:
        try:
            table(name).update_item(
                Key={"PK": key},
                UpdateExpression="ADD attempts :minus",
                ConditionExpression="attempts > :zero",
                ExpressionAttributeValues={":minus": -1, ":zero": 0},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                logger.error(json.dumps({"guard": "refund_failed",
                                         "code": exc.response.get("Error", {}).get("Code")}))
        except BotoCoreError:
            logger.error(json.dumps({"guard": "refund_failed"}))

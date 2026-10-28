"""catering-signing-webhook

TRIGGER:
    API Gateway POST /webhooks/signing/catering?l=<locationId>&r=<requestId>&t=<token>
    -- Auth: NONE; the signing provider calls it (the URL is set per signing
    order by catering-offer, shared/signing.py create_order).

PURPOSE:
    A signing provider tells us something changed on a signing order. The
    callback is never trusted:
      1. the request is loaded from l/r and its current signing order must
         verify the callback (shared/signing.py verify_webhook: the order's
         own random token in constant time, or the provider's signature);
         anything else is 401
      2. an event already handled (AUDIT#SIGNING#<eventId> in the history)
         answers 200 and stops
      3. the order's state is read again from the provider and every party
         that has signed there but not here is recorded with its evidence
         (shared/catering_signing.py apply_status), conditionally on the
         request's status and version
      4. the event is written as AUDIT#SIGNING#<eventId> (dedupe + audit)
    A failure answers 503 and logs SIGNING_WEBHOOK_FAILED (alarm), so
    the provider retries; step 3 is idempotent. Two cases also answer 503 for
    a retry but alarm only when they last: the provider has not sealed the
    signed document yet (Scrive seals after the last signature; alarm after
    SEAL_ALARM_MINUTES), and a party's own status call recording the same
    signature at the same moment (a conflict, logged as a race).

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    CATERING_REQUESTS_TABLE_NAME, CATERING_REQUEST_HISTORY_TABLE_NAME,
    CATERING_DOCUMENTS_BUCKET_NAME, SIGNING_PROVIDER_SECRET_ARN,
    DOCUMENT_LOCK_MODE, DOCUMENT_LOCK_DAYS, DOCUMENT_LOCK_YEAR_END

AWS RESOURCE ACCESS:
    catering-requests: GetItem, UpdateItem; catering-request-history: GetItem, PutItem;
    S3 catering-documents: GetObject offers/, PutObject + PutObjectRetention signed/;
    Secrets Manager: the signing-provider secret
"""

import base64
import json
import logging
from datetime import datetime, timedelta

from botocore.exceptions import BotoCoreError, ClientError

from shared import catering as c
from shared import catering_signing as sign
from shared import signing

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SEAL_ALARM_MINUTES = 30


def _respond(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def _raw_body(event):
    body = event.get("body") or ""
    return base64.b64decode(body).decode("utf-8") if event.get("isBase64Encoded") else body


def handler(event, context):
    query = event.get("queryStringParameters") or {}
    try:
        item = c.load(str(query.get("l") or ""), str(query.get("r") or ""))
    except c.NotFound:
        return _respond(404, {"error": "not found"})
    except (BotoCoreError, ClientError):
        logger.exception("signing webhook: load failed")
        logger.error("SIGNING_WEBHOOK_FAILED %s", json.dumps({"stage": "load"}))
        return _respond(503, {"error": "unavailable"})
    order = (item.get("signing") or {}).get("order")
    try:
        verified = signing.verify_webhook(order, {k.lower(): v for k, v in (event.get("headers") or {}).items()},
                                          _raw_body(event), query) if order else None
    except (signing.SigningUnavailable, ValueError):
        verified = None
    if not verified:
        logger.warning(json.dumps({"signingWebhook": "rejected", "requestId": item["requestId"]}))
        return _respond(401, {"error": "unauthorized"})
    event_id = verified["eventId"]
    history = c.dynamo.table(c.history_table())
    key = {"PK": f"REQUEST#{item['requestId']}", "SK": f"AUDIT#SIGNING#{event_id}"}
    try:
        if history.get_item(Key=key, ConsistentRead=True).get("Item"):
            return _respond(200, {"result": "duplicate"})
        try:
            done = sign.apply_status(item)
        except sign.NotReady:
            done = []  # the order is no longer the current one (new version, declined, signed)
        history.put_item(Item={**key, "action": "signing_webhook", "by": "provider", "at": c.iso(c.now_utc()),
                               "recorded": done}, ConditionExpression="attribute_not_exists(SK)")
    except signing.EvidenceNotReady:
        return _seal_pending(item)
    except c.InvalidState as exc:
        logger.info(json.dumps({"signingWebhook": "race", "requestId": item["requestId"], "code": exc.code}))
        return _respond(503, {"error": "retry"})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return _respond(200, {"result": "duplicate"})
        return _failed(item, exc)
    except (BotoCoreError, signing.SigningUnavailable, signing.SigningRejected, c.InvalidState, c.Unavailable,
            RuntimeError) as exc:
        return _failed(item, exc)
    logger.info(json.dumps({"signingWebhook": "handled", "requestId": item["requestId"], "recorded": done}))
    return _respond(200, {"result": "handled", "recorded": done})


def _seal_pending(item):
    fresh = c.load(item["locationId"], item["requestId"])
    signed_at = fresh.get("signedAt")
    late = bool(signed_at) and datetime.fromisoformat(signed_at.replace("Z", "+00:00")) \
        < c.now_utc() - timedelta(minutes=SEAL_ALARM_MINUTES)
    if late:
        logger.error("SIGNING_WEBHOOK_FAILED %s", json.dumps({"requestId": item["requestId"], "error": "seal_pending"}))
    else:
        logger.info(json.dumps({"signingWebhook": "seal_pending", "requestId": item["requestId"]}))
    return _respond(503, {"error": "seal_pending"})


def _failed(item, exc):
    logger.error("SIGNING_WEBHOOK_FAILED %s", json.dumps({"requestId": item["requestId"], "error": type(exc).__name__}))
    return _respond(503, {"error": "unavailable"})

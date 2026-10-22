"""guest-privacy

TRIGGER:
    API Gateway -- Auth: JWT (owner only)
    GET    /privacy/notice                  the saved privacy notice (or the
                                            filled template) + the template
    PUT    /privacy/notice                  {body} save a new version
    DELETE /privacy/notice                  back to the template
    GET    /privacy/consents?email=&phone=  one guest's consent state and
                                            history (newest first)
    POST   /privacy/consents/withdraw       {email?, phone?, channels}
                                            withdraw on the guest's behalf

PURPOSE:
    The restaurant (controller) edits the privacy notice its guests see
    (shared/privacy_notice.py) and handles a guest who asks, by phone or in
    person, to stop getting offers. A withdrawal is a new record in the
    consent log (shared/consent.py), never an overwrite, and it affects
    marketing only: booking and order messages go out as before. The
    subscriber list is updated in the same step so no campaign reaches the
    guest. Not behind the "marketing" feature: a restaurant that turned
    marketing off must still be able to answer a guest.

    Logs carry the tenant id and counts only, never the guest's address.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    USER_TABLE_NAME, CONSENT_TABLE_NAME, MARKETING_TABLE_NAME (optional)

AWS RESOURCE ACCESS:
    GetItem/UpdateItem on the tenant table (PROFILE); Query/PutItem on the
    consent table; UpdateItem on the marketing table (subscriber status).
"""

import json
import logging
import os

from botocore.exceptions import BotoCoreError, ClientError

from shared import consent, dynamo, http, marketing, privacy_notice, tenant
from shared import reservations as r

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_NOTICE = "/privacy/notice"
_CONSENTS = "/privacy/consents"
_WITHDRAW = "/privacy/consents/withdraw"


def _notice(ctx, method, event):
    row = tenant.get_tenant(ctx.tenant_id, fresh=True) or ctx.tenant
    if method == "GET":
        return http.respond(200, {"notice": privacy_notice.public(row), "template": privacy_notice.template(row)})
    key = {"PK": tenant.tenant_pk(ctx.tenant_id), "SK": "PROFILE"}
    table = dynamo.table(os.environ["TENANT_TABLE_NAME"])
    if method == "DELETE":
        table.update_item(Key=key, UpdateExpression="REMOVE privacyNotice",
                          ConditionExpression="attribute_exists(PK)")
        tenant.invalidate(ctx.tenant_id)
        logger.info(json.dumps({"privacyNotice": "reset", "tenantId": ctx.tenant_id}))
        return http.respond(200, {"notice": privacy_notice.public({**row, "privacyNotice": None})})
    body = privacy_notice.validate(http.body(event, allowed={"body"}).get("body"))
    previous = row.get("privacyNotice") if isinstance(row.get("privacyNotice"), dict) else {}
    version = int(previous.get("version") or 0) + 1
    saved = {"body": body, "version": version, "updatedAt": consent.now_iso(), "updatedBy": ctx.sub}
    table.update_item(Key=key, UpdateExpression="SET privacyNotice = :p", ConditionExpression="attribute_exists(PK)",
                      ExpressionAttributeValues={":p": saved})
    tenant.invalidate(ctx.tenant_id)
    logger.info(json.dumps({"privacyNotice": "saved", "tenantId": ctx.tenant_id, "version": version}))
    return http.respond(200, {"notice": privacy_notice.public({**row, "privacyNotice": saved})})


def _guest(raw_email, raw_phone):
    """(email, phone) normalised; at least one is required."""
    email = raw_email.strip().lower() if isinstance(raw_email, str) and raw_email.strip() else None
    if email and not marketing.valid_email(email):
        raise ValueError("email is invalid")
    phone = r.normalize_phone(raw_phone) if isinstance(raw_phone, str) and raw_phone.strip() else None
    if not email and not phone:
        raise ValueError("email or phone is required")
    return email, phone


def _view(record):
    keep = ("at", "action", "channel", "source", "text", "textVersion", "locationId", "ref")
    out = {k: record.get(k) for k in keep if record.get(k) is not None}
    actor = record.get("actor")
    out["by"] = "guest" if actor in (None, "guest") else ("system" if actor == "ses" else "restaurant")
    return out


def _lookup(ctx, table, email, phone):
    result = {}
    for channel, identifier in ((consent.EMAIL, email), (consent.SMS, phone)):
        if not identifier:
            continue
        sid = consent.subject(ctx.tenant_id, channel, identifier)
        history = consent.history(table, ctx.tenant_id, sid)
        state = "none" if not history else ("granted" if history[0].get("action") == consent.GRANT else "withdrawn")
        result[channel] = {"state": state, "history": [_view(h) for h in history]}
    return result


def _consents(ctx, method, path, event):
    name = consent.table_name()
    if not name:
        return http.error(503, "consent log unavailable")
    table = dynamo.table(name)
    if method == "GET" and path == _CONSENTS:
        q = http.query(event)
        email, phone = _guest(q.get("email"), q.get("phone"))
        return http.respond(200, _lookup(ctx, table, email, phone))
    data = http.body(event, allowed={"email", "phone", "channels"})
    email, phone = _guest(data.get("email"), data.get("phone"))
    channels = data.get("channels")
    if not isinstance(channels, list) or not channels or any(c not in consent.CHANNELS for c in channels):
        raise ValueError("channels must be a non-empty list of email/sms")
    withdrawn = []
    for channel in consent.CHANNELS:
        identifier = email if channel == consent.EMAIL else phone
        if channel not in channels or not identifier:
            continue
        if consent.state(table, ctx.tenant_id, channel, identifier) != "granted":
            continue  # nothing to withdraw: no record is added
        consent.withdraw(table, tenant_id=ctx.tenant_id, channel=channel, identifier=identifier, source="admin",
                         actor=ctx.sub)
        withdrawn.append(channel)
        if channel == consent.EMAIL and os.environ.get("MARKETING_TABLE_NAME"):
            _unsubscribe(ctx.tenant_id, email)
    logger.info(json.dumps({"consent": "withdrawn_by_restaurant", "tenantId": ctx.tenant_id, "channels": withdrawn}))
    return http.respond(200, {"withdrawn": withdrawn, **_lookup(ctx, table, email, phone)})


def _unsubscribe(tenant_id, email):
    try:
        dynamo.table(os.environ["MARKETING_TABLE_NAME"]).update_item(
            Key=marketing.sub_key(tenant_id, marketing.subscriber_id(tenant_id, email)),
            UpdateExpression="SET #st = :u, unsubscribedAt = :now, unsubscribedVia = :via",
            ConditionExpression="#st IN (:s, :p)", ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":u": "unsubscribed", ":s": "subscribed", ":p": "pending",
                                       ":now": marketing.now_iso(), ":via": "restaurant"})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


def handler(event, context):
    method, path = http.route(event)
    try:
        ctx = tenant.for_jwt(event, owner_only=True)
        if path == _NOTICE and method in ("GET", "PUT", "DELETE"):
            return _notice(ctx, method, event)
        if (path == _CONSENTS and method == "GET") or (path == _WITHDRAW and method == "POST"):
            return _consents(ctx, method, path, event)
        return http.error(404, "not found")
    except tenant.TenantError as exc:
        return exc.response()
    except ValueError as exc:
        return http.error(400, str(exc))
    except (BotoCoreError, ClientError):
        logger.exception("guest-privacy failed")
        return http.error(503, "privacy service unavailable")

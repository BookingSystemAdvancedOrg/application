"""google-business-jobs

TRIGGER:
    EventBridge Scheduler (hourly)  {"job": "sync"}
        Push opening hours for every linked location whose mapped hours
        changed since the last push (hash), one tenant token at a time;
        a failed location never stops the rest.
    EventBridge rule on <prefix>booqy-events
        source booqy.guest-crm, detail-type GuestVisit.Recorded
        -> one REVJOB row + one one-time schedule gbp-review-<kind>-<sourceId>
    EventBridge Scheduler (one-time)  {"reviewJob": {tenantId, locationId, kind, sourceId}}
        Send the review request once (REVJOB claimed with status = scheduled).

PURPOSE:
    ADR-D decisions 3-7. A review request is direct marketing: it goes out
    only with a current consent grant for the channel, at most once per
    guest per `minDaysBetween`, never to an erased/anonymised guest or a
    no-show, with the same text for everyone (no review gating) and the
    one-click withdraw link. The contact is read from the source row at
    send time; nothing about the guest goes to Google.

ENV_VARS:
    ENVIRONMENT, GOOGLE_BUSINESS_TABLE_NAME, TENANT_TABLE_NAME,
    LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME, RESERVATION_TABLE_NAME,
    ORDER_TABLE_NAME, CONSENT_TABLE_NAME, GOOGLE_BUSINESS_CLIENT,
    GOOGLE_BUSINESS_APP_SECRET_ARN, EVENT_BUS_NAME, SCHEDULE_GROUP_NAME,
    SCHEDULER_INVOKE_ROLE_ARN, SCHEDULER_DLQ_ARN (optional), JOBS_FUNCTION_ARN,
    NO_REPLY_EMAIL_ADDRESS, MARKETING_CONFIGURATION_SET, MARKETING_LINK_BASE,
    MARKETING_LINK_KEY_SECRET_ARN, MAIL_OUTBOX_BUCKET_NAME (dev only),
    NOTIFICATION_FUNCTION_NAME (SMS via notification:live), SMS_ALLOWED_PREFIXES (optional)
"""

import html
import json
import logging
import os
from datetime import timedelta

import boto3
from botocore.exceptions import ClientError

from shared import consent, dynamo, events, marketing_mail, tenant
from shared.features import google_business as gbp
from shared.features import guest_crm as crm
from shared.google_business import GoogleBusinessError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SOURCE_TABLES = {"reservation": "RESERVATION_TABLE_NAME", "order": "ORDER_TABLE_NAME"}
KIND_TABLE = {"booking": "reservation", "order": "order"}
SMS_ALLOWED_PREFIXES = ("+46", "+45", "+47", "+358", "+354")
MIN_LEAD_SECONDS = 60
_clients = {}


def _lambda():
    if "lambda" not in _clients:
        _clients["lambda"] = boto3.client("lambda")
    return _clients["lambda"]


def _publish(detail_type, detail):
    try:
        events.publish(gbp.FEATURE_ID, detail_type, detail)
    except Exception as exc:
        logger.warning(json.dumps({"event": detail_type, "publish": "failed", "type": type(exc).__name__}))


def _on(tenant_row) -> bool:
    features = ((tenant_row or {}).get("entitlements") or {}).get("features") or {}
    return (tenant_row or {}).get("status") == "active" and bool(features.get(gbp.FEATURE_KEY))


# --- hourly sync ------------------------------------------------------------------------

def run_sync() -> dict:
    counts = {"checked": 0, "changed": 0, "failed": 0, "skipped": 0}
    tokens = {}
    for row in gbp.linked_locations():
        tenant_id, _, location_id = str(row.get("GSI1SK") or "").partition("#")
        counts["checked"] += 1
        try:
            tenant_row = tenant.get_tenant(tenant_id)
            connection = gbp.get_connection(tenant_id) if _on(tenant_row) else None
            location = tenant.get_location(tenant_id, location_id) if connection else None
            link = gbp.get_link(tenant_id, location_id) if location else None
            if not connection or connection.get("status") != "connected" or not link:
                counts["skipped"] += 1
                continue
            if tenant_id not in tokens:
                tokens[tenant_id] = gbp.access_token(tenant_id)
            result = gbp.sync_link(tenant_id, location, link, tokens[tenant_id])
            if result["changed"]:
                counts["changed"] += 1
                _publish("GoogleHours.Synced", {"tenantId": tenant_id, "locationId": location_id, "changed": True})
        except GoogleBusinessError as exc:
            counts["failed"] += 1
            if exc.code in ("unauthorized", "invalid_grant"):
                gbp.table().update_item(Key=gbp.connection_key(tenant_id), UpdateExpression="SET #s = :r",
                                        ConditionExpression="#s = :c", ExpressionAttributeNames={"#s": "status"},
                                        ExpressionAttributeValues={":r": "needs_reauth", ":c": "connected"})
            _publish("GoogleHours.SyncFailed", {"tenantId": tenant_id, "locationId": location_id, "code": exc.code})
        except ClientError as exc:  # one bad location never stops the run
            counts["failed"] += 1
            logger.warning(json.dumps({"sync": "failed", "aws": exc.response.get("Error", {}).get("Code")}))
    logger.info(json.dumps({"job": "sync", **counts}))
    return counts


# --- GuestVisit.Recorded -> schedule ---------------------------------------------------

def schedule_review(detail: dict) -> str:
    tenant_id, location_id = detail.get("tenantId"), detail.get("locationId")
    kind, source_id, guest_key = detail.get("kind"), detail.get("sourceId"), detail.get("guestKey")
    ref = detail.get("contactRef") or {}
    if kind not in gbp.SOURCES or not all(isinstance(v, str) and v for v in (tenant_id, location_id, source_id,
                                                                              guest_key)):
        return "ignored"
    if ref.get("pk") != f"LOCATION#{location_id}" or ref.get("table") != KIND_TABLE[kind]:
        return "ignored"
    if tenant.tenant_of_location(location_id) != tenant_id or not _on(tenant.get_tenant(tenant_id)):
        return "ignored"
    settings = gbp.get_settings(tenant_id, location_id)
    if not settings.get("enabled") or kind not in (settings.get("sources") or []):
        return "disabled"
    at = gbp.parse_iso(detail.get("at")) or gbp.now()
    due = max(at + timedelta(minutes=int(settings["delayMinutes"])), gbp.now() + timedelta(seconds=MIN_LEAD_SECONDS))
    key = gbp.revjob_key(location_id, kind, source_id)
    try:
        gbp.table().put_item(Item={**key, "tenantId": tenant_id, "dueAt": gbp.iso(due), "status": "scheduled",
                                   "guestKey": guest_key, "contactRef": {k: ref[k] for k in ("table", "pk", "sk")},
                                   "ttl": int((due + timedelta(days=gbp.REVJOB_TTL_DAYS)).timestamp())},
                             ConditionExpression="attribute_not_exists(PK)")
    except ClientError as exc:
        if not gbp.conditional_failed(exc):
            raise
        existing = gbp.table().get_item(Key=key, ConsistentRead=True).get("Item") or {}
        if existing.get("status") != "scheduled":
            return "duplicate"
        due = gbp.parse_iso(existing.get("dueAt")) or due
    payload = {"reviewJob": {"tenantId": tenant_id, "locationId": location_id, "kind": kind, "sourceId": source_id}}
    created = gbp.create_schedule(gbp.schedule_name(kind, source_id), due, payload)
    return "scheduled" if created else "duplicate"


# --- the send --------------------------------------------------------------------------

def _skip(key, reason, *, from_status="scheduled"):
    try:
        gbp.table().update_item(Key=key, UpdateExpression="SET #s = :sk, reason = :r",
                                ConditionExpression="#s = :from", ExpressionAttributeNames={"#s": "status"},
                                ExpressionAttributeValues={":sk": "skipped", ":r": reason, ":from": from_status})
    except ClientError as exc:
        if not gbp.conditional_failed(exc):
            raise
    return f"skipped:{reason}"


def _source_row(job, location_id):
    ref = job.get("contactRef") or {}
    env = SOURCE_TABLES.get(ref.get("table"))
    name = os.environ.get(env) if env else None
    if not name or ref.get("pk") != f"LOCATION#{location_id}" or not ref.get("sk"):
        return None
    return dynamo.table(name).get_item(Key={"PK": ref["pk"], "SK": ref["sk"]}).get("Item")


def _sms_allowed(phone):
    raw = os.environ.get("SMS_ALLOWED_PREFIXES") or ",".join(SMS_ALLOWED_PREFIXES)
    return any(phone.startswith(p.strip()) for p in raw.split(",") if p.strip())


def _pick_channel(tenant_id, settings, row):
    contact = {"email": (row.get("customerEmail") or "").strip(), "sms": (row.get("customerPhone") or "").strip()}
    if not contact["email"] and not contact["sms"]:
        return None, None, "no_contact"
    table_name = consent.table_name()
    if not table_name:
        return None, None, "no_consent"
    tbl = dynamo.table(table_name)
    for channel in settings.get("channels") or []:
        value = contact.get(channel)
        if not value or (channel == "sms" and not _sms_allowed(value)):
            continue
        if consent.may_send(tbl, tenant_id, channel, value):
            return channel, value, None
    return None, None, "no_consent"


def _texts(restaurant, review_uri, withdraw_url):
    subject = f"Tack för besöket hos {restaurant}"
    text = (f"Hej!\n\nTack för att du besökte {restaurant}. Vi blir glada om du vill berätta hur du tyckte "
            f"att det var i ett omdöme på Google:\n{review_uri}\n\n"
            f"Vill du inte få fler utskick från oss? Avregistrera dig här: {withdraw_url}\n")
    esc = html.escape
    html_body = (f"<p>Hej!</p><p>Tack för att du besökte {esc(restaurant)}. Vi blir glada om du vill berätta "
                 f"hur du tyckte att det var i ett omdöme på Google.</p>"
                 f"<p><a href=\"{esc(review_uri, quote=True)}\">Skriv ett omdöme</a></p>"
                 f"<p style=\"font-size:12px\">Vill du inte få fler utskick från oss? "
                 f"<a href=\"{esc(withdraw_url, quote=True)}\">Avregistrera dig</a>.</p>")
    sms = f"Tack för besöket hos {restaurant}! Lämna gärna ett omdöme: {review_uri} Avregistrera: {withdraw_url}"
    return subject, text, html_body, sms


def send_review(job_ref: dict) -> str:
    tenant_id, location_id = job_ref.get("tenantId"), job_ref.get("locationId")
    kind, source_id = job_ref.get("kind"), job_ref.get("sourceId")
    if kind not in gbp.SOURCES or not all(isinstance(v, str) and v for v in (tenant_id, location_id, source_id)):
        return "ignored"
    key = gbp.revjob_key(location_id, kind, source_id)
    job = gbp.table().get_item(Key=key, ConsistentRead=True).get("Item")
    if not job or job.get("tenantId") != tenant_id or job.get("status") != "scheduled":
        return "done"
    tenant_row = tenant.get_tenant(tenant_id)
    location = tenant.get_location(tenant_id, location_id) if _on(tenant_row) else None
    settings = gbp.get_settings(tenant_id, location_id)
    link = gbp.get_link(tenant_id, location_id)
    if not location or not settings.get("enabled") or kind not in (settings.get("sources") or []) \
            or not link or not link.get("newReviewUri"):
        return _skip(key, "disabled")
    row = _source_row(job, location_id)
    if not row or row.get("anonymisedAt") or row.get("tenantId", tenant_id) != tenant_id:
        return _skip(key, "no_contact")
    if crm.classify(kind, row) != kind:
        return _skip(key, "no_show")
    channel, to, reason = _pick_channel(tenant_id, settings, row)
    if reason:
        return _skip(key, reason)
    # Claim first: a re-fired schedule finds status != scheduled and sends nothing.
    try:
        gbp.table().update_item(Key=key, UpdateExpression="SET #s = :sent, channel = :c, sentAt = :at",
                                ConditionExpression="#s = :sch", ExpressionAttributeNames={"#s": "status"},
                                ExpressionAttributeValues={":sent": "sent", ":c": channel, ":sch": "scheduled",
                                                           ":at": gbp.iso(gbp.now())})
    except ClientError as exc:
        if gbp.conditional_failed(exc):
            return "done"
        raise
    at = gbp.now()
    days = int(settings["minDaysBetween"])
    try:
        gbp.table().put_item(
            Item={**gbp.revsent_key(tenant_id, job["guestKey"]), "tenantId": tenant_id, "lastSentAt": gbp.iso(at),
                  "channel": channel, "ttl": int((at + timedelta(days=days + 2)).timestamp())},
            ConditionExpression="attribute_not_exists(PK) OR lastSentAt < :cutoff",
            ExpressionAttributeValues={":cutoff": gbp.iso(at - timedelta(days=days))})
    except ClientError as exc:
        if not gbp.conditional_failed(exc):
            raise
        return _skip(key, "recent", from_status="sent")
    restaurant = (location.get("name") or tenant_row.get("name") or "restaurangen").strip()
    withdraw = consent.withdraw_link(os.environ.get("MARKETING_LINK_BASE", ""), tenant_id,
                                     **({"email": to} if channel == "email" else {"phone": to})) or ""
    subject, text, html_body, sms_text = _texts(restaurant, link["newReviewUri"], withdraw)
    try:
        _deliver(channel, to, tenant_id, location_id, tenant_row, restaurant, subject, text, html_body, sms_text,
                 withdraw)
    except Exception:
        # Nothing went out: undo the claim and the cooldown row, then fail so the
        # schedule's retry (and in the end its DLQ) sends it again.
        _unclaim(key, tenant_id, job["guestKey"], gbp.iso(at))
        raise
    _publish("ReviewRequest.Sent", {"tenantId": tenant_id, "locationId": location_id, "kind": kind,
                                    "sourceId": source_id, "channel": channel})
    return "sent"


def _unclaim(key, tenant_id, guest_key, sent_at):
    # The cooldown row this attempt wrote was older than the cutoff (or absent), so deleting it is the same.
    try:
        gbp.table().update_item(Key=key, UpdateExpression="SET #s = :sch REMOVE channel, sentAt",
                                ConditionExpression="#s = :sent", ExpressionAttributeNames={"#s": "status"},
                                ExpressionAttributeValues={":sch": "scheduled", ":sent": "sent"})
    except ClientError as exc:
        if not gbp.conditional_failed(exc):
            raise
    try:
        gbp.table().delete_item(Key=gbp.revsent_key(tenant_id, guest_key), ConditionExpression="lastSentAt = :at",
                                ExpressionAttributeValues={":at": sent_at})
    except ClientError as exc:
        if not gbp.conditional_failed(exc):
            raise


def _deliver(channel, to, tenant_id, location_id, tenant_row, restaurant, subject, text, html_body, sms_text,
             withdraw):
    if channel == "email":
        identity = marketing_mail.sender_identity(tenant_row)
        if identity:
            text += f"\n{identity}\n"
            html_body += f"<p style=\"font-size:12px\">{html.escape(identity)}</p>"
        marketing_mail.send(to=to, subject=subject, text=text, html_body=html_body,
                            sender=marketing_mail.from_address(restaurant), unsubscribe_url=withdraw or None,
                            kind="google-business.review-request")
    else:
        # SMS only through notification:live (CONTRACTS.md 9.4): tenant sender, prefixes, part counting.
        notice = {"version": 1, "kind": "google-business.review-request", "tenantId": tenant_id,
                  "locationId": location_id, "channel": "sms", "to": to, "text": sms_text, "replyTo": None}
        resp = _lambda().invoke(FunctionName=os.environ["NOTIFICATION_FUNCTION_NAME"], InvocationType="Event",
                                Payload=json.dumps({"notice": notice}).encode("utf-8"))
        if (resp or {}).get("StatusCode") != 202:
            raise RuntimeError(f"notification invoke returned {(resp or {}).get('StatusCode')}")


# --- dispatch --------------------------------------------------------------------------

def handler(event, context=None):
    event = event or {}
    if event.get("job") == "sync":
        return run_sync()
    if isinstance(event.get("reviewJob"), dict):
        return {"result": send_review(event["reviewJob"])}
    if event.get("source") == "booqy.guest-crm" and event.get("detail-type") == "GuestVisit.Recorded":
        return {"result": schedule_review(event.get("detail") or {})}
    logger.warning(json.dumps({"event": "unknown"}))
    return {"result": "ignored"}

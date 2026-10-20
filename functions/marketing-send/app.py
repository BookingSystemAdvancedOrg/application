"""marketing-send

TRIGGER:
    EventBridge Scheduler, every 2 minutes ({"source": "schedule"}).
    Overlapping runs are safe: a campaign is leased by one run at a time and
    every recipient is claimed before the email is sent.

PURPOSE:
    Sends the campaigns that are due (SCHEDULE#... items written by the
    marketing function when the owner presses Skicka or schedules a time):
      * one email per subscriber in the campaign's audience who is still
        subscribed at send time (unsubscribed, bounced and complained
        addresses are skipped);
      * each recipient is written first (conditional put) and sent once,
        so a retried or resumed run never mails anyone twice (the recipient
        rows never expire); if nothing reached SES the recipient and the
        month's usage are released again;
      * Avbryt stops a campaign within one page (the status is re-read with
        a consistent read before each page, and every write is conditional
        on "sending", so a cancelled campaign is never set back to sent);
      * every link is a signed click link (marketing-public) and every email
        has the one-click unsubscribe headers;
      * at most MARKETING_MONTHLY_LIMIT emails per restaurant per calendar
        month - a campaign that reaches it stops as "paused_limit";
      * paced at MARKETING_SEND_RATE emails per second; a run stops a minute
        before its timeout and the next run continues from the saved cursor.
    The content was frozen when the campaign was sent (template, dishes,
    links, sender), so editing a template afterwards changes nothing.

ENV_VARS:
    FINANCE_TABLE_NAME -- finance table: +1 marketingEmails on the
        restaurant's day row per email sent (shared/finance.py).
    ENVIRONMENT, MARKETING_TABLE_NAME, MARKETING_LINK_KEY_SECRET_ARN,
    MARKETING_LINK_BASE (https://restaurant.<domain>/api), NO_REPLY_EMAIL_ADDRESS,
    MARKETING_CONFIGURATION_SET, MARKETING_MONTHLY_LIMIT (default 5000),
    MARKETING_SEND_RATE (default 10), MAIL_OUTBOX_BUCKET_NAME (dev only)

AWS RESOURCE ACCESS:
    Query/GetItem/PutItem/UpdateItem/DeleteItem on the marketing table;
    ses:SendRawEmail on the no-reply identity; GetSecretValue on the link
    key; dev only: s3:PutObject on the mail outbox.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from shared import dynamo, finance, marketing as m, marketing_mail

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PAGE = 50
LEASE_MINUTES = 10
MSG_TTL_DAYS = 45


def _table():
    return dynamo.table(m.table_name())


def _limit():
    return int(os.environ.get("MARKETING_MONTHLY_LIMIT") or 5000)


def _rate():
    return max(1.0, float(os.environ.get("MARKETING_SEND_RATE") or 10))


def _conditional_failed(exc):
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _lease(table, tenant_id, campaign_id, now):
    try:
        out = table.update_item(
            Key={"PK": m.cmp_pk(tenant_id), "SK": f"CMP#{campaign_id}"},
            UpdateExpression="SET #st = :sending, leaseUntil = :until, startedAt = if_not_exists(startedAt, :now)",
            ConditionExpression="#st IN (:scheduled, :sending) AND (attribute_not_exists(leaseUntil) OR leaseUntil < :now)",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":sending": "sending", ":scheduled": "scheduled", ":now": now.isoformat(),
                                       ":until": (now + timedelta(minutes=LEASE_MINUTES)).isoformat()},
            ReturnValues="ALL_NEW")
        return out["Attributes"]
    except ClientError as exc:
        if _conditional_failed(exc):
            return None
        raise


def _count_usage(table, tenant_id):
    """+1 on this month's counter; False when the monthly limit is reached."""
    try:
        table.update_item(
            Key={"PK": m.usage_pk(tenant_id), "SK": f"MONTH#{m.month_key()}"},
            UpdateExpression="ADD sent :one",
            ConditionExpression="attribute_not_exists(sent) OR sent < :limit",
            ExpressionAttributeValues={":one": Decimal(1), ":limit": Decimal(_limit())})
        return True
    except ClientError as exc:
        if _conditional_failed(exc):
            return False
        raise


def _link_base():
    return (os.environ.get("MARKETING_LINK_BASE") or "").rstrip("/")


def _recipient_key(campaign, sid):
    return {"PK": m.recipients_pk(campaign["tenantId"], campaign["campaignId"]), "SK": f"TO#{sid}"}


def _claim(table, campaign, sid):
    """Writes the recipient first; False when an earlier run already did."""
    try:
        table.put_item(Item={**_recipient_key(campaign, sid), "status": "sending", "at": m.now_iso()},
                       ConditionExpression="attribute_not_exists(PK)")
        return True
    except ClientError as exc:
        if _conditional_failed(exc):
            return False
        raise


class NotSent(Exception):
    """Nothing reached SES: the recipient and the month's usage are released."""


def _release(table, campaign, sid):
    table.delete_item(Key=_recipient_key(campaign, sid))
    table.update_item(Key={"PK": m.usage_pk(campaign["tenantId"]), "SK": f"MONTH#{m.month_key()}"},
                      UpdateExpression="ADD sent :minus", ExpressionAttributeValues={":minus": Decimal(-1)})


def _send_one(table, campaign, subscriber):
    try:
        return _deliver(table, campaign, subscriber)
    except NotSent:
        raise
    except ClientError:
        raise
    except BotoCoreError:
        # SES may or may not have accepted it: never risk a second copy.
        sid = subscriber["SK"].split("#", 1)[1]
        logger.warning(json.dumps({"marketing": "send_unknown", "campaignId": campaign["campaignId"]}))
        table.update_item(Key=_recipient_key(campaign, sid), UpdateExpression="SET #st = :f",
                          ExpressionAttributeNames={"#st": "status"}, ExpressionAttributeValues={":f": "failed"})
        return "failed"


def _deliver(table, campaign, subscriber):
    tenant_id, campaign_id = campaign["tenantId"], campaign["campaignId"]
    sid = subscriber["SK"].split("#", 1)[1]
    base = _link_base()
    links = campaign.get("links") or []

    def link_for(index, url):
        if index >= len(links):
            return url
        return f"{base}/m/c/{m.sign({'k': 'c', 't': tenant_id, 'c': campaign_id, 's': sid, 'i': index})}"

    try:
        unsubscribe_url = f"{base}/m/u/{m.sign({'k': 'u', 't': tenant_id, 's': sid, 'c': campaign_id})}"
        snapshot = campaign["content"]
        values = {"förnamn": m.first_name(subscriber.get("name")) or "där",
                  "restaurang": snapshot.get("restaurant") or ""}
        subject, text, html_body = m.render_email(
            snapshot["template"], tenant_row={"branding": snapshot.get("branding") or {}},
            restaurant=snapshot.get("restaurant") or "", values=values, menu_items=snapshot.get("dishes") or {},
            site_url=snapshot.get("siteUrl"), link_for=link_for, unsubscribe_url=unsubscribe_url,
            postal_address=snapshot.get("postalAddress") or "")
    except Exception as exc:  # link key, rendering: nothing was sent
        raise NotSent(str(exc)) from exc
    try:
        message_id = marketing_mail.send(
            to=subscriber["email"], subject=subject, text=text, html_body=html_body,
            sender=marketing_mail.from_address(snapshot.get("restaurant")),
            reply_to=snapshot.get("replyTo"), unsubscribe_url=unsubscribe_url)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in ("Throttling", "ThrottlingException", "ServiceUnavailable", "TooManyRequestsException"):
            raise NotSent(code) from exc
        logger.warning(json.dumps({"marketing": "send_failed", "code": code, "campaignId": campaign_id}))
        table.update_item(Key={"PK": m.recipients_pk(tenant_id, campaign_id), "SK": f"TO#{sid}"},
                          UpdateExpression="SET #st = :f", ExpressionAttributeNames={"#st": "status"},
                          ExpressionAttributeValues={":f": "failed"})
        return "failed"
    ttl = int((datetime.now(timezone.utc) + timedelta(days=MSG_TTL_DAYS)).timestamp())
    # The recipient row is kept for good (no TTL): it is what stops a resumed
    # or retried run from mailing anyone twice. Only the SES lookup expires.
    table.update_item(Key={"PK": m.recipients_pk(tenant_id, campaign_id), "SK": f"TO#{sid}"},
                      UpdateExpression="SET #st = :s, messageId = :id",
                      ExpressionAttributeNames={"#st": "status"},
                      ExpressionAttributeValues={":s": "sent", ":id": message_id})
    table.put_item(Item={"PK": f"MSG#{message_id}", "SK": "MSG", "tenantId": tenant_id,
                         "campaignId": campaign_id, "subscriberId": sid, "ttl": ttl})
    finance.count(tenant_id, marketingEmails=1)
    return "sent"


def _cmp_key(campaign):
    return {"PK": m.cmp_pk(campaign["tenantId"]), "SK": f"CMP#{campaign['campaignId']}"}


def _finish(table, campaign, status, schedule_sk, cursor=None, **extra):
    """Ends a run's campaign. Only while it is still "sending": a campaign the
    owner cancelled meanwhile stays cancelled. A campaign paused at the
    monthly limit keeps its cursor, so resuming continues where it stopped."""
    names = {"#st": "status", "#cursor": "cursor"}
    values = {":st": status, ":now": m.now_iso(), ":sending": "sending"}
    sets = ["#st = :st", "finishedAt = :now"]
    for k, v in extra.items():
        names[f"#{k}"] = k
        values[f":{k}"] = v
        sets.append(f"#{k} = :{k}")
    if cursor:
        sets.append("#cursor = :cursor")
        values[":cursor"] = cursor
        remove = " REMOVE leaseUntil"
    else:
        remove = " REMOVE leaseUntil, #cursor"
    try:
        table.update_item(Key=_cmp_key(campaign), UpdateExpression="SET " + ", ".join(sets) + remove,
                          ConditionExpression="#st = :sending",
                          ExpressionAttributeNames=names, ExpressionAttributeValues=values)
    except ClientError as exc:
        if not _conditional_failed(exc):
            raise
        table.update_item(Key=_cmp_key(campaign), UpdateExpression="REMOVE leaseUntil")
    table.delete_item(Key={"PK": "SCHEDULE", "SK": schedule_sk})


def _still_sending(table, campaign):
    item = table.get_item(Key=_cmp_key(campaign), ConsistentRead=True,
                          ProjectionExpression="#st", ExpressionAttributeNames={"#st": "status"}).get("Item")
    return bool(item) and item.get("status") == "sending"


def _run_campaign(table, campaign, schedule_sk, deadline):
    tenant_id, campaign_id = campaign["tenantId"], campaign["campaignId"]
    audience = campaign.get("audience") or {"type": "all"}
    today = datetime.now(timezone.utc).date()
    cursor = campaign.get("cursor")
    interval = 1.0 / _rate()
    counts = {"sent": 0, "failed": 0, "targeted": 0}
    try:
        while True:
            # Avbryt takes effect within one page (at most PAGE emails).
            if not _still_sending(table, campaign):
                return "cancelled", cursor
            params = {"KeyConditionExpression": Key("PK").eq(m.sub_pk(tenant_id)), "Limit": PAGE}
            if cursor:
                params["ExclusiveStartKey"] = cursor
            page = table.query(**params)
            for subscriber in page.get("Items") or []:
                if time.monotonic() > deadline:
                    return "paused", cursor
                if not m.in_audience(subscriber, audience, today):
                    continue
                sid = subscriber["SK"].split("#", 1)[1]
                if not _claim(table, campaign, sid):
                    continue  # already sent (or being sent) by an earlier run
                if not _count_usage(table, tenant_id):
                    table.delete_item(Key=_recipient_key(campaign, sid))
                    return "limit", cursor
                started = time.monotonic()
                try:
                    result = _send_one(table, campaign, subscriber)
                except NotSent as exc:
                    _release(table, campaign, sid)
                    logger.warning(json.dumps({"marketing": "not_sent", "reason": str(exc)[:200],
                                               "campaignId": campaign_id}))
                    return "paused", cursor
                counts["targeted"] += 1
                counts[result] += 1
                spent = time.monotonic() - started
                if spent < interval:
                    time.sleep(interval - spent)
            cursor = page.get("LastEvaluatedKey")
            if not cursor:
                return "done", None
            # Save progress after each page so a crash resumes here.
            try:
                table.update_item(Key={"PK": m.cmp_pk(tenant_id), "SK": f"CMP#{campaign_id}"},
                                  UpdateExpression="SET #cursor = :c", ConditionExpression="#st = :sending",
                                  ExpressionAttributeNames={"#cursor": "cursor", "#st": "status"},
                                  ExpressionAttributeValues={":c": cursor, ":sending": "sending"})
            except ClientError as exc:
                if not _conditional_failed(exc):
                    raise
                return "cancelled", cursor
    finally:
        m.bump_campaign(table, tenant_id, campaign_id, **counts)


def handler(event, context):
    table = _table()
    now = datetime.now(timezone.utc)
    remaining = context.get_remaining_time_in_millis() / 1000 if context else 240
    deadline = time.monotonic() + max(10.0, remaining - 60)
    due = table.query(KeyConditionExpression=Key("PK").eq("SCHEDULE") & Key("SK").lte(now.isoformat() + "~"),
                      Limit=20).get("Items") or []
    handled = 0
    for entry in due:
        if time.monotonic() > deadline:
            break
        tenant_id, campaign_id = entry.get("tenantId"), entry.get("campaignId")
        campaign = _lease(table, tenant_id, campaign_id, now)
        if campaign is None:
            current = table.get_item(Key={"PK": m.cmp_pk(tenant_id), "SK": f"CMP#{campaign_id}"}).get("Item")
            if not current or current.get("status") not in ("scheduled", "sending"):
                table.delete_item(Key={"PK": "SCHEDULE", "SK": entry["SK"]})  # cancelled or gone
            continue
        try:
            outcome, cursor = _run_campaign(table, campaign, entry["SK"], deadline)
        except (BotoCoreError, ClientError):
            logger.exception("marketing campaign run failed; next run resumes")
            table.update_item(Key={"PK": m.cmp_pk(tenant_id), "SK": f"CMP#{campaign_id}"},
                              UpdateExpression="REMOVE leaseUntil")
            continue
        if outcome == "done":
            _finish(table, campaign, "sent", entry["SK"], sentAt=m.now_iso())
        elif outcome == "limit":
            _finish(table, campaign, "paused_limit", entry["SK"], cursor=cursor)
        elif outcome == "cancelled":
            table.update_item(Key={"PK": m.cmp_pk(tenant_id), "SK": f"CMP#{campaign_id}"},
                              UpdateExpression="REMOVE leaseUntil")
            table.delete_item(Key={"PK": "SCHEDULE", "SK": entry["SK"]})
        else:
            key = {"PK": m.cmp_pk(tenant_id), "SK": f"CMP#{campaign_id}"}
            if cursor:
                table.update_item(Key=key, UpdateExpression="SET #cursor = :c REMOVE leaseUntil",
                                  ExpressionAttributeNames={"#cursor": "cursor"},
                                  ExpressionAttributeValues={":c": cursor})
            else:
                table.update_item(Key=key, UpdateExpression="REMOVE leaseUntil")
        handled += 1
        logger.info(json.dumps({"marketing": "campaign_run", "tenantId": tenant_id, "campaignId": campaign_id,
                                "outcome": outcome}))
    return {"campaigns": handled}

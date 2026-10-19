"""marketing

TRIGGER:
    API Gateway -- GET|POST|PUT|DELETE /marketing/{proxy+} -- Auth: JWT (owner, plan feature "marketing")

    GET    /marketing/overview?from=&to=&locationId=   visits, results and campaigns for a period
    GET    /marketing/starters                         ready-made templates to start from
    GET    /marketing/templates                        the restaurant's templates
    POST   /marketing/templates
    GET    /marketing/templates/{id}
    PUT    /marketing/templates/{id}
    DELETE /marketing/templates/{id}
    POST   /marketing/preview                          {template} -> {subject, html} (nothing sent)
    GET    /marketing/audience?type=&days=&locationId= how many subscribers an audience reaches
    GET    /marketing/campaigns
    POST   /marketing/campaigns                        {name, templateId, subject?, audience}
    GET    /marketing/campaigns/{id}
    PUT    /marketing/campaigns/{id}                   drafts only
    DELETE /marketing/campaigns/{id}                   drafts only
    POST   /marketing/campaigns/{id}/test              one email to the signed-in owner
    POST   /marketing/campaigns/{id}/send              {scheduledAt?} freeze and queue
    POST   /marketing/campaigns/{id}/cancel            scheduled or sending
    POST   /marketing/campaigns/{id}/resume            continue after the monthly limit (new month)
    GET    /marketing/offer                            welcome offer settings + results
    PUT    /marketing/offer                            save the settings
    GET    /marketing/subscribers?status=&cursor=
    GET    /marketing/subscribers/export               CSV
    POST   /marketing/subscribers/{id}/unsubscribe
    DELETE /marketing/subscribers/{id}                 erase (GDPR)

PURPOSE:
    The owner's marketing section in the admin app. Sending is done by
    marketing-send; this function freezes what is sent (template, dishes,
    links, sender) when the owner presses Skicka, so later edits change
    nothing in a campaign that is on its way. Audiences are only ever
    subscribers who opted in (marketing-sync).

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    MARKETING_TABLE_NAME, MENU_TABLE_NAME, MENU_IMAGE_BASE_URL, CUSTOMER_SITE_URL, USER_TABLE_NAME,
    NO_REPLY_EMAIL_ADDRESS, MARKETING_CONFIGURATION_SET, MARKETING_MONTHLY_LIMIT,
    MAIL_OUTBOX_BUCKET_NAME (dev only)

AWS RESOURCE ACCESS:
    Query/GetItem/PutItem/UpdateItem/DeleteItem on the marketing table;
    GetItem on the menu and user tables; tenant-context read policy; ses:SendRawEmail
    on the no-reply identity (test emails to the owner only); dev only:
    s3:PutObject on the mail outbox.
"""

import csv
import io
import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from shared import dynamo, http, marketing as m, marketing_mail, site, tenant
from shared import marketing_offer as offer

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MAX_TEMPLATES = 100
MAX_CAMPAIGNS = 500
RESUME_DAYS = 31  # a paused campaign can be continued the next month, not much later
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_SID = re.compile(r"[0-9a-f]{24}")
_CAMPAIGN_FIELDS = {"name", "templateId", "subject", "audience"}
_TEMPLATE_FIELDS = {"name", "subject", "preheader", "blocks"}


def _table():
    return dynamo.table(m.table_name())


def _path(event):
    raw = event.get("rawPath") or ((event.get("requestContext") or {}).get("http") or {}).get("path") or ""
    return [p for p in raw.split("/") if p][1:]  # after "marketing"


def _decimal_safe(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _decimal_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_decimal_safe(v) for v in value]
    if isinstance(value, set):
        return sorted(_decimal_safe(v) for v in value)
    return value


def _ok(status, payload):
    return http.respond(status, _decimal_safe(payload))


def _query_all(table, **params):
    items = []
    while True:
        page = table.query(**params)
        items += page.get("Items") or []
        if not page.get("LastEvaluatedKey"):
            return items
        params["ExclusiveStartKey"] = page["LastEvaluatedKey"]


# --- overview ---------------------------------------------------------------------------

def _days(query):
    today = date.today()
    to = query.get("to") or today.isoformat()
    frm = query.get("from") or (today - timedelta(days=29)).isoformat()
    if not _DAY.fullmatch(to) or not _DAY.fullmatch(frm) or frm > to:
        raise ValueError("from/to must be dates, from on or before to")
    span = (date.fromisoformat(to) - date.fromisoformat(frm)).days
    if span > 366:
        raise ValueError("at most one year at a time")
    return frm, to, span


def _sum(rows):
    total = {k: 0 for k in m.STAT_FIELDS}
    for row in rows:
        for k in m.STAT_FIELDS:
            total[k] += int(row.get(k) or 0)
    return total


def _stats(table, tenant_id, frm, to, location_id):
    rows = _query_all(table, KeyConditionExpression=Key("PK").eq(m.stats_pk(tenant_id))
                      & Key("SK").between(f"DAY#{frm}", f"DAY#{to}~"))
    if location_id:
        rows = [r for r in rows if r.get("locationId") == location_id]
    return rows


def _overview(ctx, query):
    table = _table()
    frm, to, span = _days(query)
    location_id = query.get("locationId") or None
    rows = _stats(table, ctx.tenant_id, frm, to, location_id)
    prev_to = (date.fromisoformat(frm) - timedelta(days=1)).isoformat()
    prev_from = (date.fromisoformat(frm) - timedelta(days=span + 1)).isoformat()
    previous = _sum(_stats(table, ctx.tenant_id, prev_from, prev_to, location_id))

    by_channel, by_campaign, by_day = {}, {}, {}
    for row in rows:
        for bucket, key in ((by_channel, row.get("channel") or "direct"),
                            (by_day, row.get("date"))):
            acc = bucket.setdefault(key, {k: 0 for k in m.STAT_FIELDS})
            for k in m.STAT_FIELDS:
                acc[k] += int(row.get(k) or 0)
        campaign = row.get("campaign")
        if campaign and campaign != "-":
            acc = by_campaign.setdefault(campaign, {"channel": row.get("channel"), **{k: 0 for k in m.STAT_FIELDS}})
            for k in m.STAT_FIELDS:
                acc[k] += int(row.get(k) or 0)
    series = []
    day = date.fromisoformat(frm)
    while day <= date.fromisoformat(to):
        iso = day.isoformat()
        series.append({"date": iso, **by_day.get(iso, {k: 0 for k in m.STAT_FIELDS})})
        day += timedelta(days=1)

    subscribers = table.query(KeyConditionExpression=Key("PK").eq(m.sub_pk(ctx.tenant_id)),
                              FilterExpression="#st = :s", ExpressionAttributeNames={"#st": "status"},
                              ExpressionAttributeValues={":s": "subscribed"}, Select="COUNT")
    count = subscribers.get("Count", 0)
    while subscribers.get("LastEvaluatedKey"):
        subscribers = table.query(KeyConditionExpression=Key("PK").eq(m.sub_pk(ctx.tenant_id)),
                                  FilterExpression="#st = :s", ExpressionAttributeNames={"#st": "status"},
                                  ExpressionAttributeValues={":s": "subscribed"}, Select="COUNT",
                                  ExclusiveStartKey=subscribers["LastEvaluatedKey"])
        count += subscribers.get("Count", 0)
    usage = table.get_item(Key={"PK": m.usage_pk(ctx.tenant_id), "SK": f"MONTH#{m.month_key()}"}).get("Item") or {}
    return _ok(200, {
        "from": frm, "to": to, "totals": _sum(rows), "previous": previous,
        "channels": [{"channel": k, **v} for k, v in sorted(by_channel.items(), key=lambda kv: -kv[1]["visits"])],
        "campaigns": [{"campaign": k, **v} for k, v in sorted(by_campaign.items(), key=lambda kv: -kv[1]["visits"])],
        "series": series,
        "subscribers": count,
        "emailsThisMonth": int(usage.get("sent") or 0),
        "monthlyLimit": int(os.environ.get("MARKETING_MONTHLY_LIMIT") or 5000),
    })


# --- templates --------------------------------------------------------------------------

def _tpl_key(tenant_id, template_id):
    return {"PK": m.tpl_pk(tenant_id), "SK": f"TPL#{template_id}"}


def _tpl_view(item):
    return {k: item.get(k) for k in ("templateId", "name", "subject", "preheader", "blocks", "createdAt", "updatedAt")}


def _templates(ctx, method, rest, event):
    table = _table()
    if not rest:
        if method == "GET":
            items = _query_all(table, KeyConditionExpression=Key("PK").eq(m.tpl_pk(ctx.tenant_id)))
            items.sort(key=lambda i: i.get("updatedAt") or "", reverse=True)
            return _ok(200, {"templates": [_tpl_view(i) for i in items]})
        if method == "POST":
            data = m.validate_template(http.body(event, allowed=_TEMPLATE_FIELDS))
            existing = table.query(KeyConditionExpression=Key("PK").eq(m.tpl_pk(ctx.tenant_id)), Select="COUNT")
            if existing.get("Count", 0) >= MAX_TEMPLATES:
                raise ValueError(f"at most {MAX_TEMPLATES} templates")
            template_id, now = m.new_id(), m.now_iso()
            item = {**_tpl_key(ctx.tenant_id, template_id), "templateId": template_id, **data,
                    "createdAt": now, "updatedAt": now, "createdBy": ctx.sub}
            table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
            return _ok(201, _tpl_view(item))
        return http.error(405, "method not allowed")
    template_id = rest[0]
    if len(rest) != 1 or not m.valid_id(template_id):
        return http.error(404, "not found")
    key = _tpl_key(ctx.tenant_id, template_id)
    if method == "GET":
        item = table.get_item(Key=key).get("Item")
        return _ok(200, _tpl_view(item)) if item else http.error(404, "not found")
    if method == "PUT":
        data = m.validate_template(http.body(event, allowed=_TEMPLATE_FIELDS))
        try:
            out = table.update_item(
                Key=key, ConditionExpression="attribute_exists(PK)",
                UpdateExpression="SET #n = :n, subject = :s, preheader = :p, blocks = :b, updatedAt = :u",
                ExpressionAttributeNames={"#n": "name"},
                ExpressionAttributeValues={":n": data["name"], ":s": data["subject"], ":p": data["preheader"],
                                           ":b": data["blocks"], ":u": m.now_iso()},
                ReturnValues="ALL_NEW")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return http.error(404, "not found")
            raise
        return _ok(200, _tpl_view(out["Attributes"]))
    if method == "DELETE":
        table.delete_item(Key=key)
        return http.respond(204, {})
    return http.error(405, "method not allowed")


# --- rendering ---------------------------------------------------------------------------

def _dishes(ctx, template):
    """menuItemId -> {name, description, price, imageUrl} for the dish blocks."""
    out = {}
    menu = dynamo.table(os.environ["MENU_TABLE_NAME"])
    image_base = (os.environ.get("MENU_IMAGE_BASE_URL") or "").rstrip("/")
    for block in template["blocks"]:
        if block["type"] != "dish" or block["menuItemId"] in out:
            continue
        if not tenant.get_location(ctx.tenant_id, block["locationId"]):
            continue  # another restaurant's location: never rendered
        item = menu.get_item(Key={"PK": f"LOCATION#{block['locationId']}",
                                  "SK": f"MENU#{block['menuItemId']}"}).get("Item")
        if not item:
            continue
        image_key = item.get("imageKey")
        out[block["menuItemId"]] = {
            "name": item.get("name") or "", "description": item.get("description") or "",
            "price": item.get("price"),
            "imageUrl": f"{image_base}/{image_key}" if image_base and isinstance(image_key, str) and image_key else None,
        }
    return out


def _sender_snapshot(ctx, template):
    base, slug = site.base(ctx.tenant_id, ctx.tenant)
    branding = ctx.tenant.get("branding") or {}
    return {
        "template": template,
        "restaurant": ctx.tenant.get("name") or "Restaurangen",
        "branding": {k: branding.get(k) for k in ("primaryColor", "logoUrl") if branding.get(k)},
        "dishes": _dishes(ctx, template),
        "siteUrl": base,
        "siteQuery": {"restaurang": slug} if slug else {},
        "replyTo": ctx.tenant.get("replyToEmail") or ctx.tenant.get("contactEmail"),
        "postalAddress": ((ctx.tenant.get("legal") or {}).get("address") or "") if isinstance(
            (ctx.tenant.get("legal") or {}).get("address"), str) else "",
    }


def _render(snapshot, links, *, name="", unsubscribe_url="#"):
    values = {"förnamn": m.first_name(name) or "där", "restaurang": snapshot["restaurant"]}
    return m.render_email(
        snapshot["template"], tenant_row={"branding": snapshot["branding"]}, restaurant=snapshot["restaurant"],
        values=values, menu_items=snapshot["dishes"], site_url=snapshot["siteUrl"],
        link_for=lambda i, url: links[i] if i < len(links) else url, unsubscribe_url=unsubscribe_url,
        postal_address=snapshot["postalAddress"])


def _preview(ctx, event):
    data = http.body(event, allowed={"template"})
    template = m.validate_template(data.get("template") or {})
    snapshot = _sender_snapshot(ctx, template)
    links = m.campaign_links(template, snapshot["siteUrl"], "forhandsvisning", "0" * 16, snapshot["siteQuery"])
    subject, _, html_body = _render(snapshot, links, name="Anna Andersson")
    return _ok(200, {"subject": subject, "html": html_body})


# --- audience -----------------------------------------------------------------------------

def _audience_count(ctx, audience):
    table = _table()
    rows = _query_all(table, KeyConditionExpression=Key("PK").eq(m.sub_pk(ctx.tenant_id)),
                      ProjectionExpression="#st, lastVisit, locations",
                      ExpressionAttributeNames={"#st": "status"})
    today = datetime.now(timezone.utc).date()
    return sum(1 for r in rows if m.in_audience(r, audience, today))


def _audience(ctx, query):
    audience = {"type": query.get("type") or "all"}
    if query.get("days"):
        try:
            audience["days"] = int(query["days"])
        except ValueError:
            raise ValueError("days must be a whole number") from None
    if query.get("locationId"):
        audience["locationId"] = query["locationId"]
    audience = m.validate_audience(audience)
    return _ok(200, {"audience": audience, "count": _audience_count(ctx, audience)})


# --- campaigns ----------------------------------------------------------------------------

_EDITABLE = ("draft",)


def _cmp_key(tenant_id, campaign_id):
    return {"PK": m.cmp_pk(tenant_id), "SK": f"CMP#{campaign_id}"}


def _cmp_view(item, *, full=False):
    keys = ("campaignId", "name", "templateId", "subject", "audience", "status", "scheduledAt", "startedAt",
            "sentAt", "finishedAt", "createdAt", "updatedAt", "stats")
    view = {k: item.get(k) for k in keys}
    if full and item.get("content"):
        view["frozen"] = True
    return view


def _validate_campaign(ctx, data):
    name = (data.get("name") or "").strip() if isinstance(data.get("name"), str) else ""
    if not name or len(name) > 80:
        raise ValueError("name is required (at most 80 characters)")
    template_id = data.get("templateId")
    if not m.valid_id(template_id):
        raise ValueError("templateId is required")
    if not _table().get_item(Key=_tpl_key(ctx.tenant_id, template_id)).get("Item"):
        raise ValueError("templateId does not exist")
    subject = data.get("subject")
    if subject not in (None, ""):
        if not isinstance(subject, str) or len(subject.strip()) > 120:
            raise ValueError("subject must be at most 120 characters")
        subject = subject.strip()
    else:
        subject = None
    audience = m.validate_audience(data.get("audience") or {"type": "all"})
    if audience.get("locationId") and not tenant.get_location(ctx.tenant_id, audience["locationId"]):
        raise ValueError("audience.locationId is not one of the restaurant's locations")
    return {"name": name, "templateId": template_id, "subject": subject, "audience": audience}


def _frozen_template(ctx, campaign):
    item = _table().get_item(Key=_tpl_key(ctx.tenant_id, campaign["templateId"])).get("Item")
    if not item:
        raise ValueError("the campaign's template no longer exists")
    template = m.validate_template(_decimal_safe({k: item.get(k) for k in ("name", "subject", "preheader", "blocks")}))
    if campaign.get("subject"):
        template["subject"] = campaign["subject"]
    return template


def _campaign_action(ctx, campaign, action, event):
    table = _table()
    key = _cmp_key(ctx.tenant_id, campaign["campaignId"])
    if action == "test":
        # Only ever to the signed-in owner's own address (their user profile):
        # a test send can't be used to mail anyone else.
        profile = dynamo.table(os.environ["USER_TABLE_NAME"]).get_item(
            Key={"PK": f"USER#{ctx.sub}", "SK": "PROFILE"}).get("Item") or {}
        to = profile.get("email") if profile.get("tenantId") == ctx.tenant_id else None
        if not m.valid_email(to):
            raise ValueError("your account has no email address to send a test to")
        template = _frozen_template(ctx, campaign)
        snapshot = _sender_snapshot(ctx, template)
        links = m.campaign_links(template, snapshot["siteUrl"], m.slug(campaign["name"]), campaign["campaignId"],
                                 snapshot["siteQuery"])
        subject, text, html_body = _render(snapshot, links, name=profile.get("name") or "")
        marketing_mail.send(to=to, subject=f"[Test] {subject}", text=text, html_body=html_body,
                            sender=marketing_mail.from_address(snapshot["restaurant"]),
                            reply_to=snapshot["replyTo"], kind="marketing_test")
        return _ok(200, {"sentTo": to})
    if action == "send":
        if campaign.get("status") not in _EDITABLE:
            return http.error(409, "campaign_not_draft")
        data = http.body(event, allowed={"scheduledAt"}, required=False)
        now = datetime.now(timezone.utc)
        when = now
        if data.get("scheduledAt"):
            try:
                when = datetime.fromisoformat(str(data["scheduledAt"]).replace("Z", "+00:00")).astimezone(timezone.utc)
            except ValueError:
                raise ValueError("scheduledAt must be an ISO date and time") from None
            if when < now - timedelta(minutes=1) or when > now + timedelta(days=90):
                raise ValueError("scheduledAt must be between now and 90 days ahead")
        template = _frozen_template(ctx, campaign)
        snapshot = _sender_snapshot(ctx, template)
        if not snapshot["siteUrl"] and any(b["type"] == "button" and b.get("destination") != "custom"
                                           for b in template["blocks"]):
            raise ValueError("the restaurant has no website address yet for the buttons")
        links = m.campaign_links(template, snapshot["siteUrl"], m.slug(campaign["name"]), campaign["campaignId"],
                                 snapshot["siteQuery"])
        audience_size = _audience_count(ctx, campaign["audience"])
        if audience_size == 0:
            raise ValueError("no subscribers in this audience")
        scheduled = when.isoformat()
        try:
            table.update_item(
                Key=key, ConditionExpression="#st = :draft",
                UpdateExpression=("SET #st = :scheduled, scheduledAt = :at, content = :content, links = :links, "
                                  "audienceSize = :n, queuedBy = :by, updatedAt = :now"),
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":draft": "draft", ":scheduled": "scheduled", ":at": scheduled,
                                           ":content": json.loads(json.dumps(_decimal_safe(snapshot)),
                                                                  parse_float=Decimal),
                                           ":links": links, ":n": audience_size, ":by": ctx.sub,
                                           ":now": m.now_iso()})
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return http.error(409, "campaign_not_draft")
            raise
        table.put_item(Item={"PK": "SCHEDULE", "SK": f"{scheduled}#{ctx.tenant_id}#{campaign['campaignId']}",
                             "tenantId": ctx.tenant_id, "campaignId": campaign["campaignId"]})
        logger.info(json.dumps({"marketing": "campaign_queued", "tenantId": ctx.tenant_id,
                                "campaignId": campaign["campaignId"], "audience": audience_size}))
        return _ok(200, {**_cmp_view({**campaign, "status": "scheduled", "scheduledAt": scheduled}),
                         "audienceSize": audience_size})
    if action == "resume":
        # Stopped at the monthly limit: continue where it stopped (recipients
        # already sent to are claimed and are never sent to twice).
        if campaign.get("status") != "paused_limit":
            return http.error(409, "campaign_not_paused")
        paused_at = campaign.get("finishedAt") or ""
        if paused_at and paused_at < (datetime.now(timezone.utc) - timedelta(days=RESUME_DAYS)).isoformat():
            raise ValueError("too late to resume")
        usage = table.get_item(Key={"PK": m.usage_pk(ctx.tenant_id), "SK": f"MONTH#{m.month_key()}"}).get("Item") or {}
        if int(usage.get("sent") or 0) >= int(os.environ.get("MARKETING_MONTHLY_LIMIT") or 5000):
            raise ValueError("monthly limit reached")
        scheduled = datetime.now(timezone.utc).isoformat()  # same format as the schedule query
        try:
            table.update_item(
                Key=key, ConditionExpression="#st = :paused",
                UpdateExpression="SET #st = :scheduled, scheduledAt = :at, updatedAt = :at REMOVE finishedAt",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":paused": "paused_limit", ":scheduled": "scheduled", ":at": scheduled})
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return http.error(409, "campaign_not_paused")
            raise
        table.put_item(Item={"PK": "SCHEDULE", "SK": f"{scheduled}#{ctx.tenant_id}#{campaign['campaignId']}",
                             "tenantId": ctx.tenant_id, "campaignId": campaign["campaignId"]})
        return _ok(200, {"status": "scheduled", "scheduledAt": scheduled})
    if action == "cancel":
        try:
            table.update_item(
                Key=key, ConditionExpression="#st IN (:scheduled, :sending, :paused)",
                UpdateExpression="SET #st = :cancelled, updatedAt = :now",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":scheduled": "scheduled", ":sending": "sending",
                                           ":paused": "paused_limit", ":cancelled": "cancelled",
                                           ":now": m.now_iso()})
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return http.error(409, "campaign_not_cancellable")
            raise
        return _ok(200, {"status": "cancelled"})
    return http.error(404, "not found")


def _campaigns(ctx, method, rest, event):
    table = _table()
    if not rest:
        if method == "GET":
            items = _query_all(table, KeyConditionExpression=Key("PK").eq(m.cmp_pk(ctx.tenant_id)),
                               ProjectionExpression="campaignId, #n, templateId, subject, audience, #st, scheduledAt, "
                                                    "startedAt, sentAt, finishedAt, createdAt, updatedAt, stats",
                               ExpressionAttributeNames={"#n": "name", "#st": "status"})
            items.sort(key=lambda i: i.get("createdAt") or "", reverse=True)
            return _ok(200, {"campaigns": [_cmp_view(i) for i in items]})
        if method == "POST":
            data = _validate_campaign(ctx, http.body(event, allowed=_CAMPAIGN_FIELDS))
            count = table.query(KeyConditionExpression=Key("PK").eq(m.cmp_pk(ctx.tenant_id)), Select="COUNT")
            if count.get("Count", 0) >= MAX_CAMPAIGNS:
                raise ValueError(f"at most {MAX_CAMPAIGNS} campaigns")
            campaign_id, now = m.new_id(), m.now_iso()
            item = {**_cmp_key(ctx.tenant_id, campaign_id), "campaignId": campaign_id, "tenantId": ctx.tenant_id,
                    **{k: v for k, v in data.items() if v is not None}, "status": "draft",
                    "stats": dict(m.EMPTY_CAMPAIGN_STATS), "createdAt": now, "updatedAt": now, "createdBy": ctx.sub}
            table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
            return _ok(201, _cmp_view(item))
        return http.error(405, "method not allowed")
    campaign_id = rest[0]
    if not m.valid_id(campaign_id) or len(rest) > 2:
        return http.error(404, "not found")
    key = _cmp_key(ctx.tenant_id, campaign_id)
    campaign = table.get_item(Key=key).get("Item")
    if not campaign:
        return http.error(404, "not found")
    if len(rest) == 2:
        if method != "POST":
            return http.error(405, "method not allowed")
        return _campaign_action(ctx, campaign, rest[1], event)
    if method == "GET":
        return _ok(200, _cmp_view(campaign, full=True))
    if method == "PUT":
        if campaign.get("status") not in _EDITABLE:
            return http.error(409, "campaign_not_draft")
        data = _validate_campaign(ctx, http.body(event, allowed=_CAMPAIGN_FIELDS))
        out = table.update_item(
            Key=key, ConditionExpression="#st = :draft",
            UpdateExpression="SET #n = :n, templateId = :t, subject = :s, audience = :a, updatedAt = :u",
            ExpressionAttributeNames={"#n": "name", "#st": "status"},
            ExpressionAttributeValues={":n": data["name"], ":t": data["templateId"], ":s": data["subject"],
                                       ":a": data["audience"], ":u": m.now_iso(), ":draft": "draft"},
            ReturnValues="ALL_NEW")
        return _ok(200, _cmp_view(out["Attributes"]))
    if method == "DELETE":
        if campaign.get("status") not in _EDITABLE:
            return http.error(409, "campaign_not_draft")
        table.delete_item(Key=key)
        return http.respond(204, {})
    return http.error(405, "method not allowed")


# --- welcome offer ---------------------------------------------------------------------

def _offer(ctx, method, event):
    table = _table()
    key = offer.offer_key(ctx.tenant_id)
    if method == "GET":
        return _ok(200, offer.settings_view(table.get_item(Key=key).get("Item")))
    if method != "PUT":
        return http.error(405, "method not allowed")
    data = offer.validate_settings(http.body(event))
    image_url = dish_name = None
    if data["imageMenuItemId"]:
        # The card shows one of the restaurant's own dishes; resolved now so
        # the public card never reads the menu.
        if not tenant.get_location(ctx.tenant_id, data["imageLocationId"]):
            raise ValueError("imageLocationId is not one of the restaurant's locations")
        dish = _dishes(ctx, {"blocks": [{"type": "dish", "menuItemId": data["imageMenuItemId"],
                                         "locationId": data["imageLocationId"]}]}).get(data["imageMenuItemId"])
        if not dish:
            raise ValueError("imageMenuItemId is not on the menu")
        image_url, dish_name = dish.get("imageUrl"), dish.get("name")
    values = {**data, "imageUrl": image_url, "dishName": dish_name}
    names = {f"#{k}": k for k in values}
    exprs = [f"#{k} = :{k}" for k in values] + ["updatedAt = :u", "updatedBy = :by"]
    out = table.update_item(
        Key=key, UpdateExpression="SET " + ", ".join(exprs),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues={**{f":{k}": v for k, v in values.items()}, ":u": m.now_iso(), ":by": ctx.sub},
        ReturnValues="ALL_NEW")
    logger.info(json.dumps({"marketing": "offer_saved", "tenantId": ctx.tenant_id, "enabled": data["enabled"]}))
    return _ok(200, offer.settings_view(out["Attributes"]))


# --- subscribers --------------------------------------------------------------------------

def _sub_view(item):
    return {"subscriberId": item["SK"].split("#", 1)[1], "email": item.get("email"), "name": item.get("name"),
            "status": item.get("status"), "consentAt": item.get("consentAt"),
            "consentSource": item.get("consentSource"), "lastVisit": item.get("lastVisit"),
            "locations": sorted(item.get("locations") or []), "unsubscribedAt": item.get("unsubscribedAt")}


def _csv_cell(value):
    """Guest-typed text must not run as a formula when the file is opened in Excel."""
    text = "" if value is None else str(value)
    return f"'{text}" if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _subscribers(ctx, method, rest, event):
    table = _table()
    query = http.query(event)
    if not rest and method == "GET":
        params = {"KeyConditionExpression": Key("PK").eq(m.sub_pk(ctx.tenant_id)), "Limit": 100}
        status = query.get("status")
        if status:
            if status not in ("subscribed", "pending", "unsubscribed", "bounced", "complained"):
                raise ValueError("status is invalid")
            params.update(FilterExpression="#st = :s", ExpressionAttributeNames={"#st": "status"},
                          ExpressionAttributeValues={":s": status})
        cursor = query.get("cursor")
        if cursor:
            if not _SID.fullmatch(cursor):
                raise ValueError("cursor is invalid")
            params["ExclusiveStartKey"] = m.sub_key(ctx.tenant_id, cursor)
        # A status filter is applied after Limit, so keep reading until the
        # page has rows or the list ends (never "nothing" next to "more").
        items, last = [], None
        for _ in range(20):
            page = table.query(**params)
            items += page.get("Items") or []
            last = page.get("LastEvaluatedKey")
            if len(items) >= 50 or not last:
                break
            params["ExclusiveStartKey"] = last
        return _ok(200, {"subscribers": [_sub_view(i) for i in items],
                         "cursor": last["SK"].split("#", 1)[1] if last else None})
    if rest == ["export"] and method == "GET":
        rows = _query_all(table, KeyConditionExpression=Key("PK").eq(m.sub_pk(ctx.tenant_id)))
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["email", "namn", "status", "samtycke", "samtycke_via", "senaste_besok", "avregistrerad"])
        for r in rows:
            writer.writerow([_csv_cell(v) for v in (r.get("email"), r.get("name"), r.get("status"), r.get("consentAt"),
                                                     r.get("consentSource"), r.get("lastVisit"),
                                                     r.get("unsubscribedAt") or "")])
        return {"statusCode": 200, "headers": {"Content-Type": "text/csv; charset=utf-8", "Cache-Control": "no-store",
                                               "Content-Disposition": 'attachment; filename="mottagare.csv"'},
                "body": out.getvalue()}
    if not rest or not _SID.fullmatch(rest[0]):
        return http.error(404, "not found")
    key = m.sub_key(ctx.tenant_id, rest[0])
    if rest[1:] == ["unsubscribe"] and method == "POST":
        try:
            table.update_item(Key=key, ConditionExpression="#st = :s",
                              UpdateExpression="SET #st = :u, unsubscribedAt = :now, unsubscribedVia = :via",
                              ExpressionAttributeNames={"#st": "status"},
                              ExpressionAttributeValues={":s": "subscribed", ":u": "unsubscribed",
                                                         ":now": m.now_iso(), ":via": "staff"})
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
        return http.respond(204, {})
    if len(rest) == 1 and method == "DELETE":
        table.delete_item(Key=key)
        logger.info(json.dumps({"marketing": "subscriber_erased", "tenantId": ctx.tenant_id}))
        return http.respond(204, {})
    return http.error(404, "not found")


# --- dispatch -----------------------------------------------------------------------------

def handler(event, context):
    method, _ = http.route(event)
    method = ((event.get("requestContext") or {}).get("http") or {}).get("method", method).upper()
    parts = _path(event)
    try:
        ctx = tenant.for_jwt(event, feature="marketing", owner_only=True)
        if not parts:
            return http.error(404, "not found")
        head, rest = parts[0], parts[1:]
        if head == "overview" and method == "GET" and not rest:
            return _overview(ctx, http.query(event))
        if head == "starters" and method == "GET" and not rest:
            return _ok(200, {"starters": m.starter_templates()})
        if head == "templates":
            return _templates(ctx, method, rest, event)
        if head == "preview" and method == "POST" and not rest:
            return _preview(ctx, event)
        if head == "audience" and method == "GET" and not rest:
            return _audience(ctx, http.query(event))
        if head == "campaigns":
            return _campaigns(ctx, method, rest, event)
        if head == "offer" and not rest:
            return _offer(ctx, method, event)
        if head == "subscribers":
            return _subscribers(ctx, method, rest, event)
        return http.error(404, "not found")
    except tenant.TenantError as exc:
        return exc.response()
    except ValueError as exc:
        return http.error(400, str(exc))
    except (BotoCoreError, ClientError):
        logger.exception("marketing failed")
        return http.error(503, "marketing service unavailable")

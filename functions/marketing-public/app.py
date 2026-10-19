"""marketing-public

TRIGGER:
    API Gateway -- Auth: NONE
    POST /t                     a visit or page view on a restaurant site
    GET  /m/c/{token}           click on a link in a marketing email (redirect)
    GET  /m/u/{token}           unsubscribe page (a button, so link scanners
                                in mail filters can't unsubscribe anyone)
    POST /m/u/{token}           unsubscribe (the page's button, and RFC 8058
                                one-click from Gmail/Outlook's own button)

PURPOSE:
    Visits: the restaurant site reports page views without cookies, browser
    storage or any visitor id. The function counts them per day, location,
    channel (Google, Instagram, campaign tag ...) and campaign. It stores no
    IP address and no referrer path, only the referring host's channel.
    Tenants without the "marketing" feature are not counted (404).

    Email links are signed (shared/marketing.py sign/verify): a click is
    recorded once per recipient and redirected to the link the campaign
    was sent with (by index, so the redirect can never be pointed at another
    site). Unsubscribing takes effect at once and is never undone by the
    platform; only a new opt-in on a later booking subscribes again.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    MARKETING_TABLE_NAME, MARKETING_LINK_KEY_SECRET_ARN

AWS RESOURCE ACCESS:
    GetItem/UpdateItem on the marketing table; tenant-context read policy;
    GetSecretValue on the marketing link key.
"""

import html
import json
import logging
import re
import time

from botocore.exceptions import BotoCoreError, ClientError

from shared import dynamo, http, marketing as m, tenant

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_BOTS = re.compile(r"bot|crawl|spider|slurp|preview|monitor|lighthouse|headless|curl|wget|python",
                   re.IGNORECASE)
_PATH = re.compile(r"/[\w\-/.]{0,200}")
_TRACK_FIELDS = {"locationId", "type", "path", "referrer", "source", "medium", "campaign", "campaignId"}


def _table():
    return dynamo.table(m.table_name())


# --- visits -----------------------------------------------------------------------------

def _track(event):
    if _BOTS.search(http.header(event, "user-agent") or "") or not http.header(event, "user-agent"):
        return http.respond(204, {})
    data = http.body(event, allowed=_TRACK_FIELDS)
    location_id = data.get("locationId")
    ctx = tenant.for_public(location_id if isinstance(location_id, str) else "", feature="marketing")
    kind = data.get("type")
    if kind not in ("visit", "page"):
        raise ValueError("type must be visit or page")
    path = data.get("path")
    if path is not None and (not isinstance(path, str) or not _PATH.fullmatch(path)):
        raise ValueError("path is invalid")
    site_host = (http.header(event, "origin") or "").split("://", 1)[-1].split("/", 1)[0].lower() or None
    channel = m.channel_of(data.get("source"), data.get("medium"),
                           m.referrer_host(data.get("referrer")), own_host=site_host)
    campaign = data.get("campaign") if isinstance(data.get("campaign"), str) else None
    day = m.local_day(tz=ctx.location.get("timezone"))
    if campaign and not _campaign_allowed(ctx.tenant_id, day, m.slug(campaign)):
        campaign = None
    m.bump_stats(_table(), ctx.tenant_id, day, location_id, channel, campaign,
                 visits=1 if kind == "visit" else 0, pageviews=1)
    return http.respond(204, {})


MAX_CAMPAIGNS_PER_DAY = 50


def _campaign_allowed(tenant_id, day, key):
    """At most MAX_CAMPAIGNS_PER_DAY distinct campaign tags per restaurant and
    day get their own statistics row. Anyone can put any utm_campaign in a
    link, so without a cap a script could create unlimited rows; extra tags
    are still counted, just without a campaign."""
    if not key:
        return False
    try:
        _table().update_item(
            Key={"PK": m.stats_pk(tenant_id), "SK": f"CAMPAIGNS#{day}"},
            UpdateExpression="ADD #k :key SET #ttl = if_not_exists(#ttl, :ttl)",
            ConditionExpression="attribute_not_exists(#k) OR size(#k) < :max OR contains(#k, :one)",
            ExpressionAttributeNames={"#k": "keys", "#ttl": "ttl"},
            ExpressionAttributeValues={":key": {key}, ":one": key, ":max": MAX_CAMPAIGNS_PER_DAY,
                                       ":ttl": int(time.time()) + 3 * 86400})
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


# --- email links ------------------------------------------------------------------------

_PAGE = """<!doctype html><html lang="sv"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<meta name="robots" content="noindex"></head>
<body style="margin:0;background:#f4f2ee;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1a1a1a">
<main style="max-width:460px;margin:12vh auto;padding:32px 28px;background:#fff;border-radius:10px;line-height:1.5">
{body}</main></body></html>"""


def _html(status, title, body):
    return {"statusCode": status,
            "headers": {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
                        "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex",
                        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"},
            "body": _PAGE.format(title=html.escape(title), body=body)}


def _invalid_link():
    return _html(404, "Länken fungerar inte",
                 "<h1 style='font-size:20px;margin:0 0 8px'>Länken fungerar inte</h1>"
                 "<p style='margin:0'>Länken är ogiltig eller för gammal.</p>")


def _click(token):
    payload = m.verify(token)
    if not payload or payload.get("k") != "c":
        return _invalid_link()
    tenant_id, campaign_id, sid, index = payload.get("t"), payload.get("c"), payload.get("s"), payload.get("i")
    if not isinstance(tenant_id, str) or not m.valid_id(campaign_id) or not isinstance(sid, str) \
            or not isinstance(index, int):
        return _invalid_link()
    table = _table()
    campaign = table.get_item(Key={"PK": m.cmp_pk(tenant_id), "SK": f"CMP#{campaign_id}"}).get("Item")
    links = (campaign or {}).get("links") or []
    if not 0 <= index < len(links):
        return _invalid_link()
    try:
        table.update_item(
            Key={"PK": m.recipients_pk(tenant_id, campaign_id), "SK": f"TO#{sid}"},
            UpdateExpression="SET clickedAt = :now",
            ConditionExpression="attribute_exists(PK) AND attribute_not_exists(clickedAt)",
            ExpressionAttributeValues={":now": m.now_iso()})
        m.bump_campaign(table, tenant_id, campaign_id, clicked=1)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
    return {"statusCode": 302, "headers": {"Location": links[index], "Cache-Control": "no-store",
                                           "Referrer-Policy": "no-referrer"}, "body": ""}


def _unsubscribe(event, token, method):
    payload = m.verify(token)
    if not payload or payload.get("k") != "u":
        return _invalid_link()
    tenant_id, sid, campaign_id = payload.get("t"), payload.get("s"), payload.get("c")
    if not isinstance(tenant_id, str) or not isinstance(sid, str) or not re.fullmatch(r"[0-9a-f]{24}", sid):
        return _invalid_link()
    table = _table()
    row = table.get_item(Key=m.sub_key(tenant_id, sid)).get("Item")
    restaurant = html.escape((row or {}).get("restaurantName") or "restaurangen")
    if method == "GET":
        if not row or row.get("status") != "subscribed":
            return _html(200, "Avregistrerad",
                         f"<h1 style='font-size:20px;margin:0 0 8px'>Du får inga fler utskick</h1>"
                         f"<p style='margin:0'>Du är inte längre med på utskicken från {restaurant}.</p>")
        return _html(200, "Avsluta prenumerationen",
                     f"<h1 style='font-size:20px;margin:0 0 8px'>Avsluta prenumerationen?</h1>"
                     f"<p style='margin:0 0 20px'>Du slutar få erbjudanden och nyheter från {restaurant} via e-post. "
                     "Bokningsbekräftelser påverkas inte.</p>"
                     "<form method='post'><button type='submit' style='background:#1a1a1a;color:#fff;border:0;"
                     "border-radius:6px;padding:12px 20px;font-size:15px;font-weight:600;cursor:pointer'>"
                     "Avsluta prenumerationen</button></form>")
    if row and row.get("status") == "subscribed":
        try:
            table.update_item(
                Key=m.sub_key(tenant_id, sid),
                UpdateExpression="SET #st = :u, unsubscribedAt = :now, unsubscribedVia = :via",
                ConditionExpression="#st = :s",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":u": "unsubscribed", ":s": "subscribed", ":now": m.now_iso(),
                                           ":via": "email_link"})
            m.bump_campaign(table, tenant_id, campaign_id, unsubscribed=1)
            logger.info(json.dumps({"marketing": "unsubscribed", "tenantId": tenant_id}))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
    return _html(200, "Avregistrerad",
                 f"<h1 style='font-size:20px;margin:0 0 8px'>Klart, du är avregistrerad</h1>"
                 f"<p style='margin:0'>Du får inga fler utskick från {restaurant}.</p>")


def handler(event, context):
    method, route = http.route(event)
    try:
        if route == "/t" and method == "POST":
            return _track(event)
        if route == "/m/c/{token}" and method == "GET":
            return _click((event.get("pathParameters") or {}).get("token"))
        if route == "/m/u/{token}" and method in ("GET", "POST"):
            return _unsubscribe(event, (event.get("pathParameters") or {}).get("token"), method)
        return http.error(404, "not found")
    except tenant.TenantError:
        # Unknown site, inactive tenant or no marketing in the plan: nothing to count.
        return http.respond(204, {}) if route == "/t" else http.error(404, "not found")
    except m.LinkKeyUnavailable:
        logger.exception("marketing link key unavailable")
        return http.error(503, "marketing service unavailable")
    except ValueError as exc:
        return http.error(400, str(exc))
    except (BotoCoreError, ClientError):
        logger.exception("marketing-public failed")
        return http.error(503, "marketing service unavailable")


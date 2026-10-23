"""marketing-public

TRIGGER:
    API Gateway -- Auth: NONE
    POST /t                     a visit or page view on a restaurant site
    GET  /m/c/{token}           click on a link in a marketing email (redirect)
    GET  /m/u/{token}           unsubscribe page (a button, so link scanners
                                in mail filters can't unsubscribe anyone)
    POST /m/u/{token}           unsubscribe (the page's button, and RFC 8058
                                one-click from Gmail/Outlook's own button)
    GET  /m/offer?locationId=   the welcome offer card's settings (or none)
    POST /m/signup              {locationId, email, name?, consent: true,
                                turnstileToken} -> subscribed + a single-use
                                discount code by email
    POST /m/code                {locationId, code} -> the code's terms, so the
                                order page can show the discount before paying
    GET|POST /m/s/{token}       confirm a welcome-offer sign-up (double opt-in)
    GET|POST /m/w/{token}       withdraw marketing consent (signed link that
                                expires, shared/consent.py; GET shows a button)

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
    MARKETING_TABLE_NAME, MARKETING_LINK_KEY_SECRET_ARN, MARKETING_LINK_BASE,
    NO_REPLY_EMAIL_ADDRESS, MARKETING_CONFIGURATION_SET, CUSTOMER_SITE_URL,
    TURNSTILE_REQUIRED, TURNSTILE_SECRET_ARN, REQUEST_GUARD_TABLE_NAME,
    MAIL_OUTBOX_BUCKET_NAME (dev only), CONSENT_TABLE_NAME (consent log)

AWS RESOURCE ACCESS:
    Query/PutItem on the consent table;
    GetItem/PutItem/UpdateItem/DeleteItem on the marketing table; tenant-context
    read policy; GetSecretValue on the marketing link key and the Turnstile
    secret; UpdateItem on the request-guard table; ses:SendRawEmail from the
    no-reply address (the code email); dev only: s3:PutObject on the outbox.
"""

import html
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

from botocore.exceptions import BotoCoreError, ClientError

from shared import consent, dynamo, http, marketing as m, marketing_mail, request_guard, site, tenant, turnstile
from shared import marketing_offer as offer

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_BOTS = re.compile(r"bot|crawl|spider|slurp|preview|monitor|lighthouse|headless|curl|wget|python",
                   re.IGNORECASE)
_PATH = re.compile(r"/[\w\-/.]{0,200}")
_TRACK_FIELDS = {"locationId", "type", "path", "referrer", "source", "medium", "campaign", "campaignId"}
_SIGNUP_FIELDS = {"locationId", "email", "name", "consent", "turnstileToken"}
SIGNUP_IP_LIMIT = (8, 60 * 60)
SIGNUP_EMAIL_LIMIT = (3, 24 * 60 * 60)
CODE_IP_LIMIT = (30, 10 * 60)
MSG_TTL_DAYS = 45


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
    if kind == "offer":
        # The welcome offer card was shown: counted on the offer, not as a visit.
        try:
            _table().update_item(Key=offer.offer_key(ctx.tenant_id), UpdateExpression="ADD #v :one",
                                 ConditionExpression="attribute_exists(PK)",
                                 ExpressionAttributeNames={"#v": "views"}, ExpressionAttributeValues={":one": 1})
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
        return http.respond(204, {})
    if kind not in ("visit", "page"):
        raise ValueError("type must be visit, page or offer")
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


# --- welcome offer ----------------------------------------------------------------------

def _live_offer(ctx):
    if not marketing_mail.sender_identity(ctx.tenant):
        return None  # marketing mail needs the legal sender in the footer: no card, no sign-up
    item = _table().get_item(Key=offer.offer_key(ctx.tenant_id)).get("Item")
    today = m.local_day(tz=ctx.location.get("timezone"))
    return item if offer.is_live(item, today) else None


def _offer_public(event):
    query = http.query(event)
    ctx = tenant.for_public(query.get("locationId") or "", feature="marketing")
    item = _live_offer(ctx)
    return http.respond(200, {"offer": offer.public_view(item) if item else None},
                        {"Cache-Control": "public, max-age=120"})


def _order_link(ctx, code):
    """The "Beställ nu" button: the order page with the code filled in."""
    base, slug = site.base(ctx.tenant_id, ctx.tenant)
    features = (ctx.tenant.get("entitlements") or {}).get("features") or {}
    if not base or not features.get("ordering"):
        return None
    page = site.page(base, slug, "/bestall")
    return (page + ("&" if "?" in page else "?")
            + f"kod={code}&utm_source=email&utm_medium=email&utm_campaign=valkomsterbjudande")


CONSENT_TEXT = ("Ja, skicka mig erbjudanden och nyheter från {restaurant} via e-post. "
                "Jag kan avsluta när jag vill.")
CONSENT_VERSION = "welcome_offer_v1"


def _signed_link(path, payload):
    return f"{(os.environ.get('MARKETING_LINK_BASE') or '').rstrip('/')}/{path}/{m.sign(payload)}"


def _mail(ctx, sid, email, name, template):
    """One email from the restaurant (code, or why there is none)."""
    restaurant = ctx.tenant.get("name") or "Restaurangen"
    branding = ctx.tenant.get("branding") or {}
    unsubscribe_url = _signed_link("m/u", {"k": "u", "t": ctx.tenant_id, "s": sid, "c": None})
    subject, text, html_body = m.render_email(
        template, tenant_row={"branding": {k: branding.get(k) for k in ("primaryColor", "logoUrl") if branding.get(k)}},
        restaurant=restaurant, values={"förnamn": m.first_name(name) or "där", "restaurang": restaurant},
        menu_items={}, site_url=None, link_for=lambda i, url: url, unsubscribe_url=unsubscribe_url,
        postal_address=marketing_mail.sender_identity(ctx.tenant))
    message_id = marketing_mail.send(
        to=email, subject=subject, text=text, html_body=html_body, sender=marketing_mail.from_address(restaurant),
        reply_to=ctx.tenant.get("replyToEmail") or ctx.tenant.get("contactEmail"),
        unsubscribe_url=unsubscribe_url, kind="marketing_offer")
    table = _table()
    # Bounces and complaints on these emails suppress the address too.
    table.put_item(Item={"PK": f"MSG#{message_id}", "SK": "MSG", "tenantId": ctx.tenant_id,
                         "subscriberId": sid, "ttl": int(time.time()) + MSG_TTL_DAYS * 86400})
    # Counted with the campaigns (never blocked: the guest was promised it).
    table.update_item(Key={"PK": m.usage_pk(ctx.tenant_id), "SK": f"MONTH#{m.month_key()}"},
                      UpdateExpression="ADD sent :one", ExpressionAttributeValues={":one": 1})


def _code_template(ctx, terms, code, expires, *, confirm_url):
    restaurant = ctx.tenant.get("name") or "Restaurangen"
    percent = int(terms["percent"])
    notes = [f"Gäller till och med {expires[:10]}"]
    if int(terms.get("minOrderOre") or 0):
        notes.append(f"vid beställning för minst {offer.kronor(terms['minOrderOre'])}")
    notes.append("en gång, med den här e-postadressen")
    order_url = _order_link(ctx, code)
    blocks = [
        {"type": "heading", "text": f"Välkommen! Här är dina {percent} %"},
        {"type": "text", "text": "Hej {förnamn}!\n\nAnge koden när du beställer på vår webbplats."},
        {"type": "code", "code": code, "label": "Din rabattkod", "note": ", ".join(notes) + "."},
        *([{"type": "button", "label": "Beställ nu", "destination": "custom", "url": order_url}] if order_url else []),
    ]
    if confirm_url:
        blocks += [
            {"type": "divider"},
            {"type": "text", "text": f"Vill du också få våra erbjudanden och nyheter? Bekräfta din prenumeration, "
                                     f"så hör {restaurant} av sig ibland. Avsluta när du vill."},
            {"type": "button", "label": "Ja, bekräfta prenumerationen", "destination": "custom", "url": confirm_url},
            {"type": "text", "text": "Har du inte bett om det här? Då kan du strunta i mejlet."},
        ]
    return {"subject": f"Din rabattkod hos {restaurant}: {percent} %",
            "preheader": f"{percent} % på din nästa beställning", "blocks": blocks}


def _info_template(ctx, used):
    restaurant = ctx.tenant.get("name") or "Restaurangen"
    text = ("Hej {förnamn}!\n\nDen här e-postadressen har redan använt vårt välkomsterbjudande, så vi kan "
            "tyvärr inte skicka en ny kod." if used else
            "Hej {förnamn}!\n\nRabattkoden som skickades till den här e-postadressen har gått ut, och "
            "välkomsterbjudandet gäller en gång per adress.")
    return {"subject": f"Om ditt välkomsterbjudande hos {restaurant}", "preheader": "",
            "blocks": [{"type": "text", "text": text + "\n\nVälkommen tillbaka!"},
                       {"type": "text", "text": "Har du inte bett om det här? Då kan du strunta i mejlet."}]}


def _new_code(table, ctx, item, sid, owner, expires):
    for _ in range(5):
        code = offer.new_code()
        try:
            table.put_item(Item={**offer.code_key(ctx.tenant_id, code), "code": code, "tenantId": ctx.tenant_id,
                                 "subscriberId": sid, "ownerKey": owner, "percent": int(item["percent"]),
                                 "minOrderOre": int(item.get("minOrderOre") or 0), "expiresAt": expires,
                                 "status": "issued", "createdAt": m.now_iso()},
                           ConditionExpression="attribute_not_exists(PK)")
            return code
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
    raise ClientError({"Error": {"Code": "CodeCollision", "Message": "no free code"}}, "PutItem")


def _pending_subscriber(table, ctx, sid, email, name, location_id):
    """Double opt-in: the sign-up is stored as a pending consent (text and
    version kept). The address becomes "subscribed" only when its owner
    confirms from the email, or uses the code (which proves the same). An
    address already subscribed is left as it is; bounced and complained
    addresses are never touched. Returns the status after the call."""
    now = m.now_iso()
    text = CONSENT_TEXT.format(restaurant=ctx.tenant.get("name") or "restaurangen")
    try:
        table.update_item(
            Key=m.sub_key(ctx.tenant_id, sid),
            UpdateExpression=("SET email = :e, #n = if_not_exists(#n, :n), #st = :pending, pendingAt = :now, "
                              "pendingConsent = :consent, restaurantName = :r, updatedAt = :now, "
                              "createdAt = if_not_exists(createdAt, :now) ADD locations :loc"),
            ConditionExpression="attribute_not_exists(#st) OR #st IN (:pending, :unsub)",
            ExpressionAttributeNames={"#n": "name", "#st": "status"},
            ExpressionAttributeValues={":e": email, ":n": name, ":pending": "pending", ":unsub": "unsubscribed",
                                       ":now": now, ":r": (ctx.tenant.get("name") or "")[:120],
                                       ":consent": {"source": "welcome_offer", "text": text,
                                                    "version": CONSENT_VERSION, "at": now},
                                       ":loc": {location_id}})
        return "pending"
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
    return (table.get_item(Key=m.sub_key(ctx.tenant_id, sid)).get("Item") or {}).get("status")


def _signup(event):
    data = http.body(event, allowed=_SIGNUP_FIELDS)
    location_id = data.get("locationId") if isinstance(data.get("locationId"), str) else ""
    ctx = tenant.for_public(location_id, feature="marketing")
    item = _live_offer(ctx)
    if not item:
        return http.error(404, "no_offer")
    if data.get("consent") is not True:
        raise ValueError("consent must be true")
    email = data.get("email")
    if not m.valid_email(email):
        raise ValueError("email must be a valid email address")
    email = email.strip().lower()
    name = " ".join((data.get("name") if isinstance(data.get("name"), str) else "").split())[:80]
    ip = http.source_ip(event)
    now = datetime.now(timezone.utc)
    request_guard.check([("signup-ip", ip, *SIGNUP_IP_LIMIT)], now)
    if turnstile.required():
        turnstile.verify(data.get("turnstileToken"), ip, turnstile.SIGNUP_ACTION)
    request_guard.check([("signup-email", offer.canonical_email(email), *SIGNUP_EMAIL_LIMIT)], now)

    # The answer is the same whatever happens below, so nobody can find out
    # through this form whether an address has signed up or ordered here.
    sent = http.respond(200, {"sent": True})
    table = _table()
    sid = m.subscriber_id(ctx.tenant_id, email)
    status = _pending_subscriber(table, ctx, sid, email, name, location_id)
    if status in ("bounced", "complained"):
        return sent  # never mail an address that bounced or marked us as spam
    confirm_url = None if status == "subscribed" else _signed_link("m/s", {"k": "s", "t": ctx.tenant_id, "s": sid})

    owner = offer.owner_key(ctx.tenant_id, email)
    existing = table.get_item(Key=offer.email_key(ctx.tenant_id, owner)).get("Item")
    if existing:
        # One code per person, ever: signing up again re-sends the unused code.
        code_item = table.get_item(Key=offer.code_key(ctx.tenant_id, existing["code"])).get("Item") or {}
        if not code_item or code_item.get("status") == "redeemed":
            _mail(ctx, sid, email, name, _info_template(ctx, used=True))
        elif code_item.get("expiresAt", "") < m.now_iso():
            _mail(ctx, sid, email, name, _info_template(ctx, used=False))
        else:
            _mail(ctx, sid, email, name, _code_template(ctx, code_item, code_item["code"], code_item["expiresAt"],
                                                        confirm_url=confirm_url))
        return sent

    expires = offer.expires_at(item.get("validDays") or 30, now)
    code = _new_code(table, ctx, item, sid, owner, expires)
    try:
        table.put_item(Item={**offer.email_key(ctx.tenant_id, owner), "code": code, "createdAt": m.now_iso()},
                       ConditionExpression="attribute_not_exists(PK)")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        table.delete_item(Key=offer.code_key(ctx.tenant_id, code))  # a parallel sign-up won
        return sent
    _mail(ctx, sid, email, name, _code_template(ctx, item, code, expires, confirm_url=confirm_url))
    table.update_item(Key=offer.offer_key(ctx.tenant_id), UpdateExpression="ADD signups :one",
                      ExpressionAttributeValues={":one": 1})
    logger.info(json.dumps({"marketing": "offer_signup", "tenantId": ctx.tenant_id}))
    return sent


def _confirm(token, method):
    """Double opt-in: GET shows a button (mail scanners follow links), POST confirms."""
    payload = m.verify(token)
    if not payload or payload.get("k") != "s":
        return _invalid_link()
    tenant_id, sid = payload.get("t"), payload.get("s")
    if not isinstance(tenant_id, str) or not isinstance(sid, str) or not re.fullmatch(r"[0-9a-f]{24}", sid):
        return _invalid_link()
    table = _table()
    row = table.get_item(Key=m.sub_key(tenant_id, sid)).get("Item")
    restaurant = html.escape((row or {}).get("restaurantName") or "restaurangen")
    if not row or row.get("status") in ("bounced", "complained"):
        return _invalid_link()
    if row.get("status") == "subscribed":
        return _html(200, "Bekräftad", f"<h1 style='font-size:20px;margin:0 0 8px'>Du är redan med</h1>"
                                        f"<p style='margin:0'>Du får erbjudanden från {restaurant}.</p>")
    if method == "GET":
        return _html(200, "Bekräfta prenumerationen",
                     f"<h1 style='font-size:20px;margin:0 0 8px'>Vill du få erbjudanden från {restaurant}?</h1>"
                     "<p style='margin:0 0 20px'>Nyheter och erbjudanden via e-post. Avsluta när du vill.</p>"
                     "<form method='post'><button type='submit' style='background:#1a1a1a;color:#fff;border:0;"
                     "border-radius:6px;padding:12px 20px;font-size:15px;font-weight:600;cursor:pointer'>"
                     "Ja, bekräfta</button></form>")
    if m.confirm_subscriber(table, tenant_id, sid, via="email_link") and row.get("email"):
        _log_consent("grant", tenant_id, row["email"], restaurant=row.get("restaurantName"),
                     location_id=next(iter(row.get("locations") or []), None))
    return _html(200, "Bekräftad", f"<h1 style='font-size:20px;margin:0 0 8px'>Tack, nu är du med!</h1>"
                                    f"<p style='margin:0'>Du får erbjudanden från {restaurant}. "
                                    "Varje mejl har en länk för att avsluta.</p>")


def _check_code(event):
    data = http.body(event, allowed={"locationId", "code"})
    ctx = tenant.for_public(data.get("locationId") if isinstance(data.get("locationId"), str) else "",
                            feature="marketing")
    request_guard.check([("code-ip", http.source_ip(event), *CODE_IP_LIMIT)], datetime.now(timezone.utc))
    code = offer.normalize_code(data.get("code"))
    item = _table().get_item(Key=offer.code_key(ctx.tenant_id, code)).get("Item") if code else None
    if not item:
        return http.error(404, "discount_invalid")
    if item.get("status") == "redeemed":
        return http.error(409, "discount_used")
    if item.get("expiresAt", "") < m.now_iso():
        return http.error(409, "discount_expired")
    return http.respond(200, {"code": code, "percent": int(item["percent"]),
                              "minOrderOre": int(item.get("minOrderOre") or 0), "expiresAt": item["expiresAt"]})


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
            if row.get("email"):
                _log_consent("withdraw", tenant_id, row["email"], source="unsubscribe_link")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
    return _html(200, "Avregistrerad",
                 f"<h1 style='font-size:20px;margin:0 0 8px'>Klart, du är avregistrerad</h1>"
                 f"<p style='margin:0'>Du får inga fler utskick från {restaurant}.</p>")


# --- consent log (shared/consent.py) ----------------------------------------------------

def _log_consent(action, tenant_id, email, *, restaurant=None, location_id=None, source=None):
    """A grant (welcome offer confirmed) or a withdrawal (unsubscribe link)
    in the consent log. Never fails the page: the subscriber row is already
    updated and the failure is logged without the address."""
    name = consent.table_name()
    if not name:
        return
    try:
        table = dynamo.table(name)
        if action == "grant":
            consent.grant(table, tenant_id=tenant_id, restaurant=restaurant, version=CONSENT_VERSION,
                          channels=[consent.EMAIL], email=email, location_id=location_id, source="welcome_offer")
        else:
            consent.withdraw(table, tenant_id=tenant_id, channel=consent.EMAIL, identifier=email, source=source)
    except Exception:  # noqa: BLE001
        logger.exception("consent log write failed (%s)", action)


_CHANNEL_NAMES = {consent.EMAIL: "e-post", consent.SMS: "sms"}


def _withdraw_page(token, method):
    """The signed, expiring withdrawal link (consent.withdraw_link): GET shows
    what the guest has agreed to and a button (mail scanners follow links,
    they don't press buttons), POST withdraws every channel in the link.
    Marketing only: booking and order messages are not affected."""
    found = consent.read_token(token)
    name = consent.table_name()
    if not found or not name:
        return _invalid_link()
    tenant_id, subjects = found
    table = dynamo.table(name)
    profile = tenant.get_tenant(tenant_id)
    restaurant = html.escape((profile or {}).get("name") or "restaurangen")
    granted = [c for c, s in subjects.items() if consent.state(table, tenant_id, c, subject_id=s) == "granted"]
    if not granted:
        return _html(200, "Inga utskick",
                     "<h1 style='font-size:20px;margin:0 0 8px'>Du får inga erbjudanden</h1>"
                     f"<p style='margin:0'>Du har inget samtycke till erbjudanden från {restaurant}. "
                     "Bekräftelser och kvitton på bokningar och beställningar skickas som vanligt.</p>")
    channels = " och ".join(_CHANNEL_NAMES[c] for c in granted)
    if method == "GET":
        return _html(200, "Återkalla samtycke",
                     "<h1 style='font-size:20px;margin:0 0 8px'>Sluta få erbjudanden?</h1>"
                     f"<p style='margin:0 0 20px'>Du får i dag erbjudanden och nyheter från {restaurant} via "
                     f"{channels}. Om du återkallar ditt samtycke slutar vi skicka dem. Bekräftelser och kvitton "
                     "på bokningar och beställningar påverkas inte.</p>"
                     "<form method='post'><button type='submit' style='background:#1a1a1a;color:#fff;border:0;"
                     "border-radius:6px;padding:12px 20px;font-size:15px;font-weight:600;cursor:pointer'>"
                     "Återkalla samtycke</button></form>")
    for channel in granted:
        consent.withdraw(table, tenant_id=tenant_id, channel=channel, subject_id=subjects[channel],
                         source="withdraw_page")
        if channel == consent.EMAIL:
            email = next((r.get("identifier") for r in consent.history(table, tenant_id, subjects[channel])
                          if r.get("identifier")), None)
            if email:
                _unsubscribe_row(tenant_id, email, via="withdraw_page")
    logger.info(json.dumps({"consent": "withdrawn", "tenantId": tenant_id, "channels": granted}))
    return _html(200, "Samtycket återkallat",
                 "<h1 style='font-size:20px;margin:0 0 8px'>Klart, samtycket är återkallat</h1>"
                 f"<p style='margin:0'>Du får inga fler erbjudanden från {restaurant} via {channels}.</p>")


def _unsubscribe_row(tenant_id, email, *, via):
    """Keeps the subscriber list in step with the consent log."""
    try:
        _table().update_item(
            Key=m.sub_key(tenant_id, m.subscriber_id(tenant_id, email)),
            UpdateExpression="SET #st = :u, unsubscribedAt = :now, unsubscribedVia = :via",
            ConditionExpression="#st IN (:s, :p)", ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":u": "unsubscribed", ":s": "subscribed", ":p": "pending",
                                       ":now": m.now_iso(), ":via": via})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


def handler(event, context):
    method, route = http.route(event)
    try:
        if route == "/t" and method == "POST":
            return _track(event)
        if route == "/m/c/{token}" and method == "GET":
            return _click((event.get("pathParameters") or {}).get("token"))
        if route == "/m/u/{token}" and method in ("GET", "POST"):
            return _unsubscribe(event, (event.get("pathParameters") or {}).get("token"), method)
        if route == "/m/offer" and method == "GET":
            return _offer_public(event)
        if route == "/m/s/{token}" and method in ("GET", "POST"):
            return _confirm((event.get("pathParameters") or {}).get("token"), method)
        if route == "/m/w/{token}" and method in ("GET", "POST"):
            return _withdraw_page((event.get("pathParameters") or {}).get("token"), method)
        if route == "/m/signup" and method == "POST":
            return _signup(event)
        if route == "/m/code" and method == "POST":
            return _check_code(event)
        return http.error(404, "not found")
    except tenant.TenantError:
        # Unknown site, inactive tenant or no marketing in the plan: nothing to count or show.
        if route == "/t":
            return http.respond(204, {})
        if route == "/m/offer":
            return http.respond(200, {"offer": None}, {"Cache-Control": "public, max-age=120"})
        return http.error(404, "not found")
    except request_guard.Limited as exc:
        return http.respond(429, {"error": "too_many_requests"}, {"Retry-After": str(exc.retry_after)})
    except turnstile.Unavailable:
        return http.error(503, "verification_unavailable")
    except m.LinkKeyUnavailable:
        logger.exception("marketing link key unavailable")
        return http.error(503, "marketing service unavailable")
    except ValueError as exc:
        return http.error(400, str(exc))
    except (BotoCoreError, ClientError):
        logger.exception("marketing-public failed")
        return http.error(503, "marketing service unavailable")


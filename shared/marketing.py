"""Marketing for restaurants: cookieless visit statistics, campaign
attribution, and email campaigns to guests who opted in.

Pure helpers shared by the marketing functions (marketing, marketing-public,
marketing-send, marketing-sync). Nothing here talks to AWS except the link
key lookup (Secrets Manager) and the table name.

Data (one table, MARKETING_TABLE_NAME, PK/SK):

    TENANT#<t>#STATS     DAY#<date>#<loc>#<channel>#<campaign>   daily counters
    TENANT#<t>#STATS     CAMPAIGNS#<date>                         campaign tags seen that day (cap, TTL)
    TENANT#<t>#SUB       SUB#<sha256(tenant|email)[:24]>          subscriber (pending/subscribed/unsubscribed/...)
    TENANT#<t>#SEEN      ORDER#<id> / BOOKING#<id>                stream records already counted (TTL)
    TENANT#<t>#TPL       TPL#<id>                                 email template
    TENANT#<t>#CMP       CMP#<id>                                 campaign
    TENANT#<t>#USAGE     MONTH#<yyyy-mm>                          emails sent
    CMP#<t>#<id>         TO#<subscriber id>                       one recipient (kept: stops double sends)
    MSG#<ses message id> MSG                                      SES event lookup (TTL)
    SCHEDULE             <iso time>#<t>#<id>                      due campaigns

Privacy model:
  * Visits are counted, never people: the site sends page, the referring
    host and campaign tags; nothing is stored on the device, no visitor id,
    no IP address. Only daily per-restaurant counts are written.
  * Emails go only to guests who ticked "offers" when booking (consent with
    time and source), every email has a one-click unsubscribe, and bounces
    and complaints suppress the address for good. Clicks are measured through
    signed redirect links; there is no open-tracking pixel.
"""

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import boto3

# --- table ---------------------------------------------------------------------------


def table_name():
    name = os.environ.get("MARKETING_TABLE_NAME")
    if not name:
        raise RuntimeError("MARKETING_TABLE_NAME is not set")
    return name


def stats_pk(tenant_id):
    return f"TENANT#{tenant_id}#STATS"


def sub_pk(tenant_id):
    return f"TENANT#{tenant_id}#SUB"


def tpl_pk(tenant_id):
    return f"TENANT#{tenant_id}#TPL"


def cmp_pk(tenant_id):
    return f"TENANT#{tenant_id}#CMP"


def usage_pk(tenant_id):
    return f"TENANT#{tenant_id}#USAGE"


def recipients_pk(tenant_id, campaign_id):
    return f"CMP#{tenant_id}#{campaign_id}"


def new_id():
    return secrets.token_hex(8)


_ID = re.compile(r"[0-9a-f]{16}")


def valid_id(value):
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def month_key(when=None):
    return (when or datetime.now(timezone.utc)).strftime("%Y-%m")


# --- attribution and channels ----------------------------------------------------------

_TAG = re.compile(r"[\w\- .:/åäöÅÄÖ]{1,60}")
_SLUG = re.compile(r"[^a-z0-9åäö]+")

# Referring hosts we know, by suffix -> channel.
_REFERRERS = (
    ("google.", "google"), ("bing.com", "bing"), ("duckduckgo.com", "duckduckgo"),
    ("instagram.com", "instagram"), ("facebook.com", "facebook"), ("fb.com", "facebook"),
    ("m.facebook.com", "facebook"), ("l.facebook.com", "facebook"), ("tiktok.com", "tiktok"),
    ("linkedin.com", "linkedin"), ("t.co", "x"), ("twitter.com", "x"), ("x.com", "x"),
    ("youtube.com", "youtube"), ("tripadvisor.", "tripadvisor"), ("thefork.", "thefork"),
    ("maps.apple.com", "apple-maps"),
)
CHANNELS = ("direct", "google", "bing", "duckduckgo", "instagram", "facebook", "tiktok",
            "linkedin", "x", "youtube", "tripadvisor", "thefork", "apple-maps", "email", "qr",
            "sms", "other")


def _clean_tag(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or not _TAG.fullmatch(value):
        return None
    return value


def slug(value):
    """Lowercase campaign key: 'Höst-kampanj 2026' -> 'höst-kampanj-2026'."""
    return _SLUG.sub("-", (value or "").lower()).strip("-")[:48]


def referrer_host(referrer):
    """The referring host only ('www.google.com'), never the path."""
    if not isinstance(referrer, str) or not referrer:
        return None
    try:
        host = (urlsplit(referrer).hostname or "").lower()
    except ValueError:
        return None
    return host or None


def channel_of(source=None, medium=None, ref_host=None, own_host=None):
    """Where a visit came from, as one of CHANNELS.

    Campaign tags win (utm_source / utm_medium), then the referring host,
    else 'direct'. A referrer on the restaurant's own site is direct."""
    src = (_clean_tag(source) or "").lower()
    med = (_clean_tag(medium) or "").lower()
    if med in ("email", "e-mail", "newsletter") or src in ("email", "newsletter"):
        return "email"
    if med == "qr" or src == "qr":
        return "qr"
    if med == "sms" or src == "sms":
        return "sms"
    if src:
        for channel in CHANNELS:
            # Exact, or the channel followed by a separator (google-ads, instagram_bio);
            # never a prefix of another word (xmas-affisch is not X).
            if src == channel or re.match(rf"{re.escape(channel)}[-_. ]", src):
                return channel
        if src in ("ig",):
            return "instagram"
        if src in ("fb", "meta"):
            return "facebook"
        return "other"
    if ref_host and (not own_host or ref_host != own_host.lower()):
        for suffix, channel in _REFERRERS:
            if suffix.endswith("."):  # any country domain: google.se, google.com
                if ref_host.startswith(suffix) or f".{suffix}" in ref_host:
                    return channel
            elif ref_host == suffix or ref_host.endswith("." + suffix):
                return channel
        return "other"
    return "direct"


def validate_attribution(raw):
    """The `attribution` a booking, order or catering request may carry:
    {source, medium, campaign, campaignId, channel}. Unknown or malformed
    parts are dropped, never an error - attribution must not block a
    booking. Returns None when nothing usable remains."""
    if not isinstance(raw, dict):
        return None
    out = {}
    for field in ("source", "medium", "campaign"):
        value = _clean_tag(raw.get(field))
        if value:
            out[field] = value
    campaign_id = raw.get("campaignId")
    if valid_id(campaign_id):
        out["campaignId"] = campaign_id
    channel = raw.get("channel")
    if isinstance(channel, str) and channel in CHANNELS:
        out["channel"] = channel
    elif out:
        out["channel"] = channel_of(out.get("source"), out.get("medium"))
    return out or None


def stats_sk(day, location_id, channel, campaign):
    return f"DAY#{day}#{location_id or '-'}#{channel or 'direct'}#{slug(campaign) or '-'}"


def parse_stats_sk(sk):
    _, day, loc, channel, campaign = sk.split("#", 4)
    return {"date": day, "locationId": None if loc == "-" else loc, "channel": channel,
            "campaign": None if campaign == "-" else campaign}


STAT_FIELDS = ("visits", "pageviews", "bookings", "guests", "orders", "catering", "revenueOre")


# --- signed links (unsubscribe, click redirect) ----------------------------------------

_KEY_CACHE = {"key": None, "at": 0.0}


class LinkKeyUnavailable(RuntimeError):
    pass


def _link_key():
    now = time.monotonic()
    if _KEY_CACHE["key"] is None or now - _KEY_CACHE["at"] > 300:
        secret_id = os.environ.get("MARKETING_LINK_KEY_SECRET_ARN")
        if not secret_id:
            raise LinkKeyUnavailable("MARKETING_LINK_KEY_SECRET_ARN is not set")
        secret = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id).get("SecretString")
        if not isinstance(secret, str) or len(secret) < 32:
            raise LinkKeyUnavailable("marketing link key is missing or too short")
        _KEY_CACHE.update(key=secret.encode("utf-8"), at=now)
    return _KEY_CACHE["key"]


def reset_key_cache():
    _KEY_CACHE.update(key=None, at=0.0)


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(payload):
    """'<payload>.<mac>' for a small dict; the payload is readable, not secret."""
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    mac = _b64(hmac.new(_link_key(), f"mkt|v1|{body}".encode("ascii"), hashlib.sha256).digest()[:20])
    return f"{body}.{mac}"


def verify(token):
    """The payload of a signed token, or None."""
    if not isinstance(token, str) or len(token) > 600 or token.count(".") != 1:
        return None
    body, mac = token.split(".")
    try:
        expected = _b64(hmac.new(_link_key(), f"mkt|v1|{body}".encode("ascii"), hashlib.sha256).digest()[:20])
        if not hmac.compare_digest(expected, mac):
            return None
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


# --- templates -----------------------------------------------------------------------

BLOCK_TYPES = ("heading", "text", "image", "button", "dish", "divider", "spacer")
DESTINATIONS = ("home", "book", "order", "menu", "catering", "custom")
_DEST_PATHS = {"home": "/", "book": "/boka-bord", "order": "/bestall", "menu": "/meny",
               "catering": "/catering"}
MAX_BLOCKS = 40
_HTTPS = re.compile(r"https://[^\s<>\"']{3,500}")
_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def _text(value, field, *, max_length, required=False):
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(f"{field} is required")
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    value = value.strip()
    if len(value) > max_length:
        raise ValueError(f"{field} must be at most {max_length} characters")
    return value


def _https(value, field):
    if not isinstance(value, str) or not _HTTPS.fullmatch(value.strip()):
        raise ValueError(f"{field} must be an https:// link")
    return value.strip()


def validate_block(block, index):
    where = f"blocks[{index}]"
    if not isinstance(block, dict):
        raise ValueError(f"{where} must be an object")
    kind = block.get("type")
    if kind not in BLOCK_TYPES:
        raise ValueError(f"{where}.type must be one of {', '.join(BLOCK_TYPES)}")
    if kind == "heading":
        return {"type": kind, "text": _text(block.get("text"), f"{where}.text", max_length=120, required=True)}
    if kind == "text":
        return {"type": kind, "text": _text(block.get("text"), f"{where}.text", max_length=2000, required=True)}
    if kind == "image":
        out = {"type": kind, "url": _https(block.get("url"), f"{where}.url"),
               "alt": _text(block.get("alt"), f"{where}.alt", max_length=160)}
        return out
    if kind == "button":
        dest = block.get("destination", "book")
        if dest not in DESTINATIONS:
            raise ValueError(f"{where}.destination must be one of {', '.join(DESTINATIONS)}")
        out = {"type": kind, "label": _text(block.get("label"), f"{where}.label", max_length=40, required=True),
               "destination": dest}
        if dest == "custom":
            out["url"] = _https(block.get("url"), f"{where}.url")
        return out
    if kind == "dish":
        item_id = block.get("menuItemId")
        location_id = block.get("locationId")
        if not isinstance(item_id, str) or not item_id.strip() or len(item_id) > 128 or "#" in item_id:
            raise ValueError(f"{where}.menuItemId is required")
        if not isinstance(location_id, str) or not location_id.strip() or len(location_id) > 128 \
                or "#" in location_id:
            raise ValueError(f"{where}.locationId is required")
        return {"type": kind, "menuItemId": item_id.strip(), "locationId": location_id.strip()}
    if kind == "divider":
        return {"type": kind}
    size = block.get("size", "m")
    if size not in ("s", "m", "l"):
        raise ValueError(f"{where}.size must be s, m or l")
    return {"type": kind, "size": size}


def validate_theme(theme):
    """A template's own colours: `color` (header bar, buttons, code box) and
    `background` (around the email), each "#rrggbb". Empty or missing means
    the restaurant's brand colour and the light default background."""
    if theme in (None, {}):
        return None
    if not isinstance(theme, dict):
        raise ValueError("theme must be an object")
    unknown = set(theme) - {"color", "background"}
    if unknown:
        raise ValueError(f"theme has unknown fields: {', '.join(sorted(unknown))}")
    out = {}
    for key in ("color", "background"):
        value = theme.get(key)
        if value in (None, ""):
            continue
        if not isinstance(value, str) or not _COLOR.fullmatch(value):
            raise ValueError(f"theme.{key} must be a colour like #1a120e")
        out[key] = value.lower()
    return out or None


def validate_template(body):
    name = _text(body.get("name"), "name", max_length=80, required=True)
    subject = _text(body.get("subject"), "subject", max_length=120, required=True)
    preheader = _text(body.get("preheader"), "preheader", max_length=140)
    blocks = body.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("blocks must be a non-empty list")
    if len(blocks) > MAX_BLOCKS:
        raise ValueError(f"at most {MAX_BLOCKS} blocks")
    return {"name": name, "subject": subject, "preheader": preheader,
            "blocks": [validate_block(b, i) for i, b in enumerate(blocks)],
            "theme": validate_theme(body.get("theme"))}


def starter_templates():
    """Ready-made templates a restaurant can start from (copied, never shared)."""
    return [
        {"key": "new-menu", "name": "Ny meny", "subject": "Nyheter på menyn hos {restaurang}",
         "preheader": "Säsongens nya rätter är här",
         "blocks": [{"type": "heading", "text": "Nyheter på menyn"},
                    {"type": "text", "text": "Hej {förnamn}!\n\nVi har tagit fram nya rätter för säsongen och vill gärna att du provar dem."},
                    {"type": "button", "label": "Se menyn", "destination": "menu"},
                    {"type": "divider"},
                    {"type": "button", "label": "Boka bord", "destination": "book"}]},
        {"key": "weekend", "name": "Helgerbjudande", "subject": "Den här helgen hos {restaurang}",
         "preheader": "Boka bord till helgen",
         "blocks": [{"type": "heading", "text": "Välkommen i helgen"},
                    {"type": "text", "text": "Hej {förnamn}!\n\nHelgen närmar sig. Boka ett bord redan nu så har vi plats för dig."},
                    {"type": "button", "label": "Boka bord", "destination": "book"}]},
        {"key": "event", "name": "Event eller högtid", "subject": "Fira med oss på {restaurang}",
         "preheader": "Vi har något särskilt på gång",
         "blocks": [{"type": "heading", "text": "Fira med oss"},
                    {"type": "text", "text": "Hej {förnamn}!\n\nBerätta här vad som händer: datum, tider och vad som ingår."},
                    {"type": "button", "label": "Boka bord", "destination": "book"}]},
        {"key": "miss-you", "name": "Vi saknar dig", "subject": "Länge sedan, {förnamn}!",
         "preheader": "Vi skulle bli glada att se dig igen",
         "blocks": [{"type": "heading", "text": "Vi saknar dig"},
                    {"type": "text", "text": "Hej {förnamn}!\n\nDet var ett tag sedan du var hos oss. Välkommen tillbaka!"},
                    {"type": "button", "label": "Boka bord", "destination": "book"}]},
    ]


# --- audiences -----------------------------------------------------------------------

AUDIENCES = ("all", "recent", "lapsed")


def validate_audience(raw):
    if not isinstance(raw, dict):
        raise ValueError("audience must be an object")
    kind = raw.get("type", "all")
    if kind not in AUDIENCES:
        raise ValueError(f"audience.type must be one of {', '.join(AUDIENCES)}")
    out = {"type": kind}
    if kind in ("recent", "lapsed"):
        days = raw.get("days")
        if isinstance(days, Decimal):
            days = int(days)
        if not isinstance(days, int) or isinstance(days, bool) or not 7 <= days <= 730:
            raise ValueError("audience.days must be a whole number between 7 and 730")
        out["days"] = days
    location = raw.get("locationId")
    if location not in (None, ""):
        if not isinstance(location, str) or len(location) > 64:
            raise ValueError("audience.locationId is invalid")
        out["locationId"] = location
    return out


def in_audience(subscriber, audience, today):
    """Whether a subscriber row belongs to an audience on `today` (date)."""
    if subscriber.get("status") != "subscribed":
        return False
    location = audience.get("locationId")
    if location and location not in (subscriber.get("locations") or []):
        return False
    kind = audience.get("type", "all")
    if kind == "all":
        return True
    last = subscriber.get("lastVisit")
    if not isinstance(last, str):
        return kind == "lapsed"
    try:
        age = (today - datetime.strptime(last[:10], "%Y-%m-%d").date()).days
    except ValueError:
        return False
    days = int(audience.get("days") or 0)
    return age <= days if kind == "recent" else age > days


# --- rendering -----------------------------------------------------------------------

_DEFAULT_COLOR = "#1f2a24"
_DEFAULT_BACKGROUND = "#f4f2ee"


def brand_color(tenant_row):
    color = ((tenant_row or {}).get("branding") or {}).get("primaryColor")
    return color if isinstance(color, str) and _COLOR.fullmatch(color) else _DEFAULT_COLOR


def _ink_on(color):
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return "#1a1a1a" if (0.299 * r + 0.587 * g + 0.114 * b) > 160 else "#ffffff"


def fill(text, values):
    """{förnamn} / {restaurang} placeholders; unknown ones are left as is."""
    def repl(match):
        key = match.group(1).lower()
        return values.get(key, match.group(0))
    return re.sub(r"\{([a-zA-ZåäöÅÄÖ]+)\}", repl, text or "")


def destination_url(block, site_url):
    if block.get("destination") == "custom":
        return block["url"]
    if not site_url:
        return None
    return site_url.rstrip("/") + _DEST_PATHS.get(block.get("destination"), "/")


def with_campaign_tags(url, campaign_slug, campaign_id, site_url, extra_query=None):
    """Adds utm tags (and our campaign id) to links that point at the
    restaurant's own site, so bookings and orders from the email count
    for the campaign. Other sites' links are left untouched."""
    if not site_url:
        return url
    parts, site = urlsplit(url), urlsplit(site_url)
    # Same scheme and host exactly (https://pizza.se, not https://pizza.se.evil.com).
    if parts.scheme != site.scheme or parts.netloc.lower() != site.netloc.lower():
        return url
    query = dict(parse_qsl(parts.query))
    query.update(extra_query or {})
    query.update({"utm_source": "email", "utm_medium": "email", "utm_campaign": campaign_slug,
                  "bq_c": campaign_id})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def campaign_links(template, site_url, campaign_slug, campaign_id, extra_query=None):
    """The final URL of every button, in the order render_email numbers
    them. Frozen on the campaign; the click redirect only ever goes to one
    of these."""
    links = []
    for block in template["blocks"]:
        if block["type"] != "button":
            continue
        target = destination_url(block, site_url)
        if target:
            links.append(with_campaign_tags(target, campaign_slug, campaign_id, site_url, extra_query))
    return links


def render_email(template, *, tenant_row, restaurant, values, menu_items, site_url, link_for,
                 unsubscribe_url, postal_address=""):
    """(subject, text, html) for one recipient.

    `link_for(index, url)` turns the index-th link into a tracked link (the
    click redirect) or returns it unchanged (preview, test send)."""
    e = html.escape
    theme = template.get("theme") or {}
    color = theme.get("color") or brand_color(tenant_row)
    ink = _ink_on(color)
    background = theme.get("background") or _DEFAULT_BACKGROUND
    # the small print under the email stays readable on a dark background too
    fine_print = "#8a8a8a" if _ink_on(background) == "#1a1a1a" else "#d9d2c8"
    subject = fill(template["subject"], values)
    preheader = fill(template.get("preheader") or "", values)
    parts_html, parts_text = [], []
    link_index = 0
    for block in template["blocks"]:
        kind = block["type"]
        if kind == "heading":
            text = fill(block["text"], values)
            parts_html.append(f'<h2 style="margin:0 0 12px;font-size:22px;line-height:1.25;color:#1a1a1a">{e(text)}</h2>')
            parts_text += [text.upper(), ""]
        elif kind == "text":
            text = fill(block["text"], values)
            paras = "".join(f'<p style="margin:0 0 14px">{e(p).replace(chr(10), "<br>")}</p>'
                            for p in text.split("\n\n") if p.strip())
            parts_html.append(paras)
            parts_text += [text, ""]
        elif kind == "image":
            parts_html.append(f'<p style="margin:0 0 16px"><img src="{e(block["url"], quote=True)}" alt="{e(block.get("alt") or "", quote=True)}" '
                              'width="520" style="display:block;width:100%;max-width:520px;height:auto;border-radius:8px;border:0"></p>')
        elif kind == "button":
            target = destination_url(block, site_url)
            if not target:
                continue
            url = link_for(link_index, target)
            link_index += 1
            label = fill(block["label"], values)
            parts_html.append(
                f'<p style="margin:8px 0 20px"><a href="{e(url, quote=True)}" style="background:{color};color:{ink};'
                'text-decoration:none;padding:13px 24px;border-radius:6px;display:inline-block;font-weight:600">'
                f'{e(label)}</a></p>')
            parts_text += [f"{label}: {url}", ""]
        elif kind == "dish":
            item = (menu_items or {}).get(block["menuItemId"])
            if not item:
                continue
            price = item.get("price")
            price_text = f"{int(price)} kr" if isinstance(price, (int, Decimal)) else ""
            img = item.get("imageUrl")
            img_html = (f'<td width="96" style="padding:0 14px 0 0;vertical-align:top"><img src="{e(img, quote=True)}" alt="" width="96" '
                        'style="display:block;width:96px;height:96px;object-fit:cover;border-radius:48px;border:0"></td>') if img else ""
            parts_html.append(
                '<table role="presentation" style="border-collapse:collapse;margin:0 0 16px;width:100%"><tr>'
                f'{img_html}<td style="vertical-align:top"><strong style="font-size:16px">{e(item.get("name") or "")}</strong>'
                + (f'<span style="float:right;font-weight:600">{e(price_text)}</span>' if price_text else "")
                + (f'<div style="color:#6b6b6b;font-size:14px;margin-top:4px">{e(item.get("description") or "")}</div>' if item.get("description") else "")
                + '</td></tr></table>')
            parts_text += [f"{item.get('name')}{' - ' + price_text if price_text else ''}", ""]
        elif kind == "code":
            # Only built by the platform (welcome offer email), never by a template.
            code = block["code"]
            parts_html.append(
                f'<div style="margin:4px 0 20px;padding:18px;border:2px dashed {color};border-radius:8px;text-align:center">'
                f'<div style="font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:#6b6b6b">{e(block.get("label") or "Din kod")}</div>'
                f'<div style="font:700 28px/1.3 ui-monospace,Menlo,Consolas,monospace;letter-spacing:.08em;color:#1a1a1a">{e(code)}</div>'
                + (f'<div style="font-size:13px;color:#6b6b6b;margin-top:6px">{e(block["note"])}</div>' if block.get("note") else "")
                + '</div>')
            parts_text += [f"{block.get('label') or 'Din kod'}: {code}", block.get("note") or "", ""]
        elif kind == "divider":
            parts_html.append('<hr style="border:0;border-top:1px dashed #d9d4c8;margin:20px 0">')
            parts_text += ["----", ""]
        else:
            height = {"s": 8, "m": 20, "l": 36}[block.get("size", "m")]
            parts_html.append(f'<div style="height:{height}px;line-height:{height}px">&nbsp;</div>')

    footer = (f"Du får det här mejlet för att du har sagt ja till erbjudanden från {restaurant}."
              + (f" {postal_address}." if postal_address else ""))
    unsub_label = "Avsluta prenumerationen"
    text = "\n".join(parts_text + ["--", footer, f"{unsub_label}: {unsubscribe_url}"])
    logo = ((tenant_row or {}).get("branding") or {}).get("logoUrl")
    header = (f'<img src="{e(logo, quote=True)}" alt="{e(restaurant, quote=True)}" height="40" style="display:block;height:40px;width:auto;border:0">'
              if isinstance(logo, str) and logo.startswith("https://")
              else f'<div style="font-size:18px;font-weight:700;color:{ink}">{e(restaurant)}</div>')
    html_body = (
        '<!doctype html><html lang="sv"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><title>'
        f'{e(subject)}</title></head><body style="margin:0;background:{background}">'
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0">{e(preheader)}</div>'
        '<div style="max-width:600px;margin:0 auto;padding:24px 16px;'
        'font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.55;color:#1a1a1a">'
        f'<div style="background:{color};border-radius:10px 10px 0 0;padding:18px 28px">{header}</div>'
        f'<div style="background:#ffffff;border-radius:0 0 10px 10px;padding:28px">{"".join(parts_html)}</div>'
        f'<p style="margin:16px 4px 0;color:{fine_print};font-size:12px">{e(footer)} '
        f'<a href="{e(unsubscribe_url, quote=True)}" style="color:{fine_print}">{unsub_label}</a></p>'
        '</div></body></html>'
    )
    return subject, text, html_body


def first_name(full):
    return (full or "").strip().split(" ")[0] if full else ""


# --- subscribers and counters (DynamoDB) ------------------------------------------------

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
STOCKHOLM = "Europe/Stockholm"


def subscriber_id(tenant_id, email):
    """Stable id for a subscriber: never the address itself, so links and
    keys don't carry it."""
    digest = hashlib.sha256(f"{tenant_id}|{(email or '').strip().lower()}".encode("utf-8")).hexdigest()
    return digest[:24]


def sub_key(tenant_id, sid):
    return {"PK": sub_pk(tenant_id), "SK": f"SUB#{sid}"}


def valid_email(value):
    return isinstance(value, str) and len(value) <= 254 and bool(_EMAIL.fullmatch(value.strip()))


def local_day(iso=None, tz=STOCKHOLM):
    from zoneinfo import ZoneInfo
    if iso:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    else:
        when = datetime.now(timezone.utc)
    return when.astimezone(ZoneInfo(tz or STOCKHOLM)).date().isoformat()


def bump_stats(table, tenant_id, day, location_id, channel, campaign, **counts):
    """Adds to the day's counters for one (location, channel, campaign).
    counts: any of STAT_FIELDS -> number to add."""
    counts = {k: v for k, v in counts.items() if k in STAT_FIELDS and v}
    if not counts:
        return
    names = {f"#{k}": k for k in counts}
    values = {f":{k}": Decimal(int(v)) for k, v in counts.items()}
    expr = "ADD " + ", ".join(f"#{k} :{k}" for k in counts)
    values[":d"] = day
    values[":l"] = location_id or "-"
    values[":c"] = channel or "direct"
    values[":g"] = slug(campaign) or "-"
    table.update_item(
        Key={"PK": stats_pk(tenant_id), "SK": stats_sk(day, location_id, channel, campaign)},
        UpdateExpression=expr + " SET #date = :d, #loc = :l, #ch = :c, #cg = :g",
        ExpressionAttributeNames={**names, "#date": "date", "#loc": "locationId", "#ch": "channel",
                                  "#cg": "campaign"},
        ExpressionAttributeValues=values,
    )


def upsert_subscriber(table, *, tenant_id, restaurant, email, name, source, ref_id="", location_id=None,
                      visit=None, opted_in=True):
    """A guest who gave consent (ticked "offers" when booking, ordering or
    asking for catering, or signed up for the welcome offer) is subscribed,
    again if they had unsubscribed: a new ticked box is a new consent.
    Bounced and complained addresses stay suppressed. Without consent only
    an existing subscriber's last visit is updated; no row is ever created.
    Returns the subscriber id."""
    from botocore.exceptions import ClientError
    email = email.strip().lower()
    sid = subscriber_id(tenant_id, email)
    key = sub_key(tenant_id, sid)
    now = now_iso()
    if opted_in:
        sets = ("SET email = :e, #n = :n, #st = :s, consentAt = :now, consentSource = :src, consentRef = :ref, "
                "restaurantName = :r, updatedAt = :now, createdAt = if_not_exists(createdAt, :now) "
                "REMOVE unsubscribedAt, unsubscribedVia")
        values = {":e": email, ":n": (name or "")[:120], ":s": "subscribed", ":now": now, ":src": source,
                  ":ref": ref_id or "", ":r": (restaurant or "")[:120], ":b": "bounced", ":c": "complained"}
        if visit:
            sets = sets.replace("createdAt = if_not_exists(createdAt, :now)",
                                "createdAt = if_not_exists(createdAt, :now), lastVisit = if_not_exists(lastVisit, :v)")
            values[":v"] = visit
        if location_id:
            sets += " ADD locations :loc"
            values[":loc"] = {location_id}
        try:
            table.update_item(Key=key, UpdateExpression=sets,
                              ConditionExpression="attribute_not_exists(#st) OR NOT #st IN (:b, :c)",
                              ExpressionAttributeNames={"#n": "name", "#st": "status"},
                              ExpressionAttributeValues=values)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
    if visit:
        expr, values = "SET lastVisit = :v", {":v": visit}
        if location_id:
            expr += " ADD locations :loc"
            values[":loc"] = {location_id}
        try:
            table.update_item(Key=key, UpdateExpression=expr,
                              ConditionExpression="attribute_exists(PK) AND (attribute_not_exists(lastVisit) OR lastVisit < :v)",
                              ExpressionAttributeValues=values)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
    return sid


def first_time(table, tenant_id, kind, ref_id, days=3):
    """True the first time (kind, ref_id) is seen: stream records are
    delivered at least once, so counters are bumped only after this."""
    from botocore.exceptions import ClientError
    if not ref_id:
        return True
    try:
        table.put_item(Item={"PK": f"TENANT#{tenant_id}#SEEN", "SK": f"{kind}#{ref_id}",
                             "ttl": int(datetime.now(timezone.utc).timestamp()) + days * 86400},
                       ConditionExpression="attribute_not_exists(PK)")
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def confirm_subscriber(table, tenant_id, sid, *, via):
    """A pending sign-up (welcome offer) becomes subscribed: its owner
    confirmed from the email, or used the code. The pending consent becomes
    the consent on record."""
    from botocore.exceptions import ClientError
    try:
        table.update_item(
            Key=sub_key(tenant_id, sid),
            UpdateExpression=("SET #st = :s, consentAt = :now, consentSource = :src, consentText = pendingConsent.#t, "
                              "consentVersion = pendingConsent.version, confirmedVia = :via, updatedAt = :now "
                              "REMOVE pendingConsent, pendingAt, unsubscribedAt, unsubscribedVia"),
            ConditionExpression="#st = :pending AND attribute_exists(pendingConsent)",
            ExpressionAttributeNames={"#st": "status", "#t": "text"},
            ExpressionAttributeValues={":s": "subscribed", ":pending": "pending", ":now": now_iso(),
                                       ":src": "welcome_offer", ":via": via})
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def bump_campaign(table, tenant_id, campaign_id, **counts):
    """Adds to a campaign's result counters (stats.<field>). Missing
    campaigns are ignored (deleted, or a forged id)."""
    counts = {k: v for k, v in counts.items() if v}
    if not counts or not valid_id(campaign_id):
        return
    from botocore.exceptions import ClientError
    names = {"#s": "stats", **{f"#{k}": k for k in counts}}
    values = {f":{k}": Decimal(int(v)) for k, v in counts.items()}
    try:
        table.update_item(
            Key={"PK": cmp_pk(tenant_id), "SK": f"CMP#{campaign_id}"},
            UpdateExpression="ADD " + ", ".join(f"#s.#{k} :{k}" for k in counts),
            ConditionExpression="attribute_exists(PK) AND attribute_exists(#s)",
            ExpressionAttributeNames=names, ExpressionAttributeValues=values)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


EMPTY_CAMPAIGN_STATS = {k: 0 for k in ("targeted", "sent", "failed", "delivered", "bounced", "complained", "clicked",
                                      "unsubscribed", "bookings", "guests", "orders", "revenueOre")}

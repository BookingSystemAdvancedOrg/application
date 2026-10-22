"""Marketing consent log: append-only proof of what a guest agreed to.

The restaurant is the controller; we keep, per restaurant and guest, every
"yes" and every "no" as its own record. Nothing is ever updated in place: a
withdrawal is a new record, and the latest record per guest and channel is
the consent state. Transactional messages (confirmations, receipts,
reminders) never look here: consent governs marketing only.

Table (CONSENT_TABLE_NAME, PK/SK, GSI byTenant on tenantId + at):

    CONSENT#<t>#<subject>   <at>#<id>    grant / withdraw (full record)
    CONSENT#<t>#<subject>   <at>#S       suppression: written by data-retention
                                          after a withdrawal, no identifier

subject = sha256(tenant | channel | canonical identifier)[:32]. One subject
per channel: email consent is keyed by the address, SMS consent by the phone
number, so "the latest record" is always about one channel.

Every marketing send must call `may_send` first (marketing-send does).
"""

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

EMAIL, SMS = "email", "sms"
CHANNELS = (EMAIL, SMS)
GRANT, WITHDRAW, SUPPRESSED = "grant", "withdraw", "suppressed"
SOURCES = ("booking", "order", "catering", "welcome_offer", "withdraw_page", "unsubscribe_link", "admin",
           "complaint", "legacy", "erasure")
TENANT_INDEX = "byTenant"
WITHDRAW_LINK_DAYS = 90

# The checkbox wording, versioned. A version is never edited once it has been
# shown to guests: change the text by adding a new version and pointing
# CURRENT at it. {restaurang} is the restaurant's name.
TEXTS = {
    # Legacy wording (the single e-mail checkbox before the consent log).
    "offers_booking_v0": "Ja, {restaurang} får skicka mig erbjudanden och nyheter (valfritt).",
    "offers_order_v0": "Skicka mig erbjudanden och nyheter från {restaurang} via e-post (valfritt).",
    "welcome_offer_v1": ("Ja, skicka mig erbjudanden och nyheter från {restaurang} via e-post. "
                         "Jag kan avsluta när jag vill."),
    "offers_v1": ("Ja, {restaurang} får skicka mig erbjudanden och nyheter. Frivilligt, och jag kan ta "
                  "tillbaka samtycket när jag vill."),
    "legacy": "Samtycke lämnat före samtyckesloggen (ordalydelsen sparades inte).",
}
CURRENT = "offers_v1"
CHANNEL_QUESTION = "Hur vill du få dem?"
CHANNEL_LABELS = {EMAIL: "E-post", SMS: "SMS"}
# Channels the guest forms offer. SMS is not offered while nothing sends
# marketing SMS (no consent is collected for a channel that is never used);
# existing SMS records stay valid and can still be withdrawn.
OFFERED = (EMAIL,)
# Versions a guest form may still send (the current one, plus the one before
# while a cached old page is open).
ACCEPTED = (CURRENT,)
LEGACY_BY_SOURCE = {"booking": "offers_booking_v0", "order": "offers_order_v0", "catering": "offers_order_v0"}


def table_name():
    return os.environ.get("CONSENT_TABLE_NAME") or None


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical(channel, identifier):
    """The form a guest is recognised by: lower-case e-mail, E.164 phone."""
    value = (identifier or "").strip()
    return value.lower() if channel == EMAIL else value.replace(" ", "")


def subject(tenant_id, channel, identifier):
    raw = f"{tenant_id}|{channel}|{canonical(channel, identifier)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def pk(tenant_id, subject_id):
    return f"CONSENT#{tenant_id}#{subject_id}"


def render(version, restaurant):
    """The exact wording the guest saw for a version."""
    return TEXTS[version].format(restaurang=restaurant or "restaurangen")


def public_text(restaurant):
    """What site-config gives the guest forms, so they show the server's words."""
    return {"version": CURRENT, "label": render(CURRENT, restaurant), "channelQuestion": CHANNEL_QUESTION,
            "channels": [{"value": c, "label": CHANNEL_LABELS[c]} for c in OFFERED]}


def parse_request(raw, *, email=None, phone=None, legacy_opt_in=False, source="booking"):
    """The guest form's consent field -> (version, channels), or None when the
    guest did not tick the box. Accepts the new `marketingConsent` object and,
    for one release, the old `marketingOptIn: true` (= e-mail, old wording).
    Raises ValueError for a malformed field; a channel without its contact
    detail is dropped (an SMS consent needs a phone number), and so is a
    channel that is not offered (an old cached page may still send "sms")."""
    if raw is None:
        if legacy_opt_in is True and email:
            return LEGACY_BY_SOURCE.get(source, "offers_order_v0"), [EMAIL]
        return None
    if not isinstance(raw, dict) or set(raw) - {"channels", "textVersion"}:
        raise ValueError("marketingConsent must be {channels, textVersion}")
    version = raw.get("textVersion")
    if version not in ACCEPTED:
        raise ValueError("marketingConsent.textVersion is not a current wording")
    channels = raw.get("channels")
    if not isinstance(channels, list) or not channels or any(c not in CHANNELS for c in channels):
        raise ValueError("marketingConsent.channels must be a non-empty list of email/sms")
    have = {EMAIL: bool(email), SMS: bool(phone)}
    kept = [c for c in OFFERED if c in channels and have[c]]
    return (version, kept) if kept else None


def _put(table, item):
    """Append only: a record is written once and never replaced."""
    table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)")
    return item


def _record(tenant_id, channel, identifier, *, action, location_id, source, ref, actor, version, text, at,
            subject_id=None):
    sid = subject_id or subject(tenant_id, channel, identifier)
    item = {"PK": pk(tenant_id, sid), "SK": f"{at}#{secrets.token_hex(6)}", "tenantId": tenant_id, "at": at,
            "subject": sid, "action": action, "channel": channel, "source": source}
    if identifier:
        item["identifier"] = canonical(channel, identifier)
        item["identifierKind"] = "email" if channel == EMAIL else "phone"
    if location_id:
        item["locationId"] = location_id
    if ref:
        item["ref"] = str(ref)[:80]
    if actor:
        item["actor"] = str(actor)[:80]
    if version:
        item["textVersion"] = version
        item["text"] = text
    return item


def grant(table, *, tenant_id, restaurant, version, channels, email=None, phone=None, location_id=None,
          source, ref=None, at=None):
    """One grant record per chosen channel. Returns the records written."""
    if source not in SOURCES:
        raise ValueError("unknown consent source")
    at = at or now_iso()
    text = render(version, restaurant)
    written = []
    for channel in channels:
        identifier = email if channel == EMAIL else phone
        if not identifier:
            continue
        written.append(_put(table, _record(tenant_id, channel, identifier, action=GRANT, location_id=location_id,
                                           source=source, ref=ref, actor="guest", version=version, text=text,
                                           at=at)))
    return written


def marketing_on(tenant_row):
    return bool(((tenant_row or {}).get("entitlements") or {}).get("features", {}).get("marketing"))


def record_from_form(tenant_row, tenant_id, *, parsed, email, phone, location_id, source, ref):
    """After a guest form was saved: the grant records for the boxes the guest
    ticked. Only for restaurants with marketing on. Never raises: the booking
    or order is already made and must not fail because of the log (the
    failure is logged without personal data)."""
    import logging
    if not parsed or not ref or not marketing_on(tenant_row) or not table_name():
        return []
    version, channels = parsed
    try:
        from shared import dynamo
        return grant(dynamo.table(table_name()), tenant_id=tenant_id, restaurant=(tenant_row or {}).get("name"),
                     version=version, channels=channels, email=email, phone=phone, location_id=location_id,
                     source=source, ref=ref)
    except Exception:  # noqa: BLE001
        logging.getLogger().exception("consent record failed (source %s)", source)
        return []


def ref_from_response(response, field):
    """The new booking's or order's id from an API Gateway response (None
    when nothing was created)."""
    import json
    if not isinstance(response, dict) or response.get("statusCode") not in (200, 201):
        return None
    try:
        return json.loads(response.get("body") or "{}").get(field)
    except (ValueError, AttributeError):
        return None


def withdraw(table, *, tenant_id, channel, identifier=None, subject_id=None, source, actor="guest",
             location_id=None, at=None):
    """A withdrawal record for one channel. Idempotent in effect: a second
    withdrawal only adds another record, the state stays withdrawn."""
    if source not in SOURCES or channel not in CHANNELS:
        raise ValueError("unknown consent source or channel")
    if not identifier and not subject_id:
        raise ValueError("identifier or subject is required")
    return _put(table, _record(tenant_id, channel, identifier, action=WITHDRAW, location_id=location_id,
                               source=source, ref=None, actor=actor, version=None, text=None, at=at or now_iso(),
                               subject_id=subject_id))


def history(table, tenant_id, subject_id, limit=50):
    """Records for one subject, newest first."""
    out = table.query(KeyConditionExpression=Key("PK").eq(pk(tenant_id, subject_id)), ScanIndexForward=False,
                      Limit=limit, ConsistentRead=True)
    return out.get("Items") or []


def latest(table, tenant_id, subject_id):
    items = history(table, tenant_id, subject_id, limit=1)
    return items[0] if items else None


def state(table, tenant_id, channel, identifier=None, subject_id=None):
    """'granted', 'withdrawn' or 'none' for one guest and channel."""
    sid = subject_id or subject(tenant_id, channel, identifier)
    item = latest(table, tenant_id, sid)
    if not item:
        return "none"
    return "granted" if item.get("action") == GRANT else "withdrawn"


def may_send(table, tenant_id, channel, identifier):
    """The check every marketing send makes: only an explicit, current grant
    allows it (no record at all means no)."""
    return state(table, tenant_id, channel, identifier) == "granted"


# --- withdrawal links ---------------------------------------------------------------


def withdraw_token(tenant_id, subjects, *, days=WITHDRAW_LINK_DAYS, now=None):
    """A signed, expiring token for the withdrawal page. `subjects` maps a
    channel to its subject id. Signed with the marketing link key."""
    from shared import marketing
    when = now or datetime.now(timezone.utc)
    clean = {c: s for c, s in (subjects or {}).items() if c in CHANNELS and s}
    if not clean:
        raise ValueError("no subjects")
    return marketing.sign({"k": "w", "t": tenant_id, "s": clean,
                           "e": int((when + timedelta(days=days)).timestamp())})


def withdraw_link(base, tenant_id, *, email=None, phone=None, days=WITHDRAW_LINK_DAYS):
    """The withdrawal page URL for a guest (for e-mail footers), or None when
    the guest gave no contact detail. base = MARKETING_LINK_BASE."""
    subjects = {}
    if email:
        subjects[EMAIL] = subject(tenant_id, EMAIL, email)
    if phone:
        subjects[SMS] = subject(tenant_id, SMS, phone)
    if not subjects or not base:
        return None
    return f"{base.rstrip('/')}/m/w/{withdraw_token(tenant_id, subjects, days=days)}"


def read_token(token, *, now=None):
    """(tenant_id, {channel: subject}) of a valid, unexpired token, or None."""
    from shared import marketing
    payload = marketing.verify(token)
    if not payload or payload.get("k") != "w":
        return None
    tenant_id, subjects, expires = payload.get("t"), payload.get("s"), payload.get("e")
    if not isinstance(tenant_id, str) or not isinstance(subjects, dict) or not isinstance(expires, int):
        return None
    if expires < int((now or datetime.now(timezone.utc)).timestamp()):
        return None
    clean = {c: s for c, s in subjects.items()
             if c in CHANNELS and isinstance(s, str) and len(s) == 32 and all(ch in "0123456789abcdef" for ch in s)}
    return (tenant_id, clean) if clean else None


def conditional_failed(exc):
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") == \
        "ConditionalCheckFailedException"

"""create-pending-reservation

TRIGGER:
    API Gateway
    POST /locations/{locationId}/reservations          -- Auth: NONE (guests)
    POST /locations/{locationId}/reservations/manual   -- Auth: JWT (staff:
                                                          phone bookings,
                                                          walk-ins)
    POST /locations/{locationId}/reservations/{reservationId}/confirm
                                    -- Auth: NONE, header X-Manage-Token
                                       (card guarantee completed)
    POST /locations/{locationId}/reservations/{reservationId}/guarantee-link
                                    -- Auth: JWT (staff: send the card link
                                       SMS again)
    POST   /locations/{locationId}/holds             -- Auth: NONE (guests)
    DELETE /locations/{locationId}/holds/{holdId}    -- Auth: NONE, header
                                                         X-Manage-Token

TABLE HOLDS:
    The guest picks their table(s) and confirms them (POST .../holds with
    date, startTime, tableIds, partySize; bot check as below): the
    tables are claimed exactly like a booking - same locks and slot rows,
    same 409 "table_taken" if someone was faster - as a booking in status
    "held" with no guest data, for HOLD_MINUTES (15). The answer carries
    holdId, the hold's token (X-Manage-Token) and expiresAt; the site shows
    the countdown while the guest fills in name, phone and e-mail.

    POST .../reservations with "holdId" (+ X-Manage-Token) turns the hold
    into the booking - reserved, or pending for the card step, which keeps
    the hold's clock - without claiming the tables again. After expiresAt it
    answers 409 "hold_expired".

    The hold ends by itself: once expiresAt passes, the slot rows no longer
    count (availability shows the tables free, another booking may take
    them over) - a closed browser never keeps tables blocked. The site also
    lets go at once (DELETE .../holds/{holdId}, idempotent) when the timer
    runs out or the guest changes tables. reservation-reminders marks
    held/expired records and deletes their rows.

PURPOSE:
    Creates a table booking. The guest chose a date, a start time from
    GET /locations/{locationId}/availability, one or more of the tables that
    response listed as free, and the party size.

    The request is validated with the same availability engine the guest was
    shown (shared/availability.py) and committed in one DynamoDB transaction
    with per-table locks (shared/reservations.py), so two guests can never
    get the same table at overlapping times - the second gets 409
    "table_taken" and picks again.

    Online bookings return the manage token (the guest's link to view or
    cancel the booking - an HMAC, never stored; shared/manage_link.py).
    Staff bookings may omit email/phone and may book the slot that is
    already running (walk-ins), except that a phone booking (source
    "phone") by a person needs the guest's phone number (400 "phone is
    required for a phone booking"); the AI phone agent (an agent: sub) may
    book a hidden caller ID without one.

    Booking length (shared/booking_rules.py bookingTimeRules): the rule of
    the local start time sets the end; the rule is snapshotted on the hold
    and the booking (bookingRule, extendedMinutes) so later rule changes
    never move existing bookings.

    Online bookings follow the location's booking rules
    (shared/booking_rules.py): the chosen table(s) must be one of the
    options the availability response offered that party - one table whose
    online party range fits, or a restaurant-defined combination - and a
    day without online booking takes none. Staff may combine any free
    tables with enough seats, also on a day without online booking.

    Abuse protection for online bookings: at most a few attempts per IP
    address (10 minutes) and a few bookings per phone number / e-mail
    (24 hours - an attempt that books nothing is given back)
    (shared/request_guard.py, 429), and the bot check: the post came
    through the site's AWS WAF (shared/bot_check.py).

    Notifications: the booking is written with a "confirmed" notice, which
    the Reservation stream turns into the guest's confirmation (email/SMS,
    with the manage link) and, for online bookings, the restaurant's
    new-booking email. Staff bookings notify the guest unless
    notifyGuest=false (walk-ins default to false).

    Card guarantee (shared/guarantee.py): when the location's policy asks
    for a card (party size >= minPartySize, Stripe ready), the online
    booking is created "pending" - tables held for 20 minutes - with a
    Stripe Customer + SetupIntent on the restaurant's account; the response
    carries the SetupIntent client secret for the embedded card form. After
    the guest confirms the card (3-D Secure), POST .../confirm (or the
    setup_intent.succeeded webhook, whichever is first) checks the
    SetupIntent with Stripe and turns the booking "reserved" - that is
    when the confirmation goes out. Unconfirmed bookings expire
    (reservation-reminders). Staff bookings never ask for a card, except
    a phone booking with a guarantee link (below).

    Phone guarantee link (POST .../reservations/manual with source "phone",
    a phone number and "guarantee"): when the location's policy asks a card
    for the party, the booking is created "pending" like an online one (same
    tables claimed, snapshot, marker), held for
    guarantee.PHONE_PENDING_MINUTES (30) plus a small margin, with a Stripe
    Customer + Checkout Session (mode setup) on the restaurant's account
    instead of a SetupIntent. Who texts the link:
      "link"    the caller does (the AI phone agent): the answer carries
                guaranteeLink {url, expiresAt, minutes, page}.
      "sms"     Booqy does (staff on the call), synchronously after the
                booking is written: one invoke of notification:live
                (RequestResponse, kind reservations.guarantee-link, botocore
                retries off) to the booking's own number. A failed SMS (an
                error, a timeout, a FunctionError or a dropped notice)
                releases the booking at once (expired, history link_failed,
                the Checkout page expired) and answers 502 "sms_failed"; a
                card added in that instant keeps it reserved (201). The
                answer carries guaranteeLink {by, sends, textedAt,
                canResend, minutes, expiresAt, page, sms: "sent"}, never
                the url. Rules, checked before Stripe is asked: a Nordic
                mobile number (shared/sms.py textable, else 400 "guarantee
                sms needs a mobile number"), not the restaurant's own number
                or a filler like 070 000 00 00 (400 "guarantee sms needs the
                guest's own mobile number"), NOTIFICATION_FUNCTION_NAME set
                (else 503 "sms_unavailable"), at most 60 link SMS per
                location a day and 20 per staff user an hour (429
                "sms_limit"; given back when nothing was sent). No card is
                asked when the booking starts less than 47 minutes ahead (the
                hold could lapse after the start): reserved at once with
                guaranteeSkipped "too_soon" (history cardSkipped). The
                location's phone agent setting phoneGuarantee "skip" is
                honoured only with PHONE_AGENT_TABLE_NAME and read access to
                that table (not configured in Terraform today: the card is
                asked).
      "waived"  staff book without the card the policy asks for ("Boka utan
                kort"): reserved at once, history guarantee_waived.
    Both texting modes store guaranteeLink {url, page, by: "booqy" |
    "caller", sends, textedAt?} on the booking (the url is a bearer link:
    never in a view, history or log). POST .../guarantee-link sends the same
    link again to the booking's own number: pending only (409
    "not_pending"), at least 3 minutes of the window left ("link_closed"),
    3 SMS on the booking's counter ("resend_limit"; the AI agent's own
    resend during its call is not counted), the guest's own mobile number
    ("not_textable", the same rule as the booking); the
    counter is claimed first (a double click: 409 "changed_retry"); a
    failed SMS answers 502 and keeps the booking pending.
    The Checkout page's SetupIntent carries the booking's metadata: the
    setup_intent.succeeded webhook turns it reserved (confirmation then,
    always by SMS as well: notification); so does the guest's return to the
    booking's manage page (Checkout's success_url), whose POST .../confirm
    reads the completed Checkout Session. Without a card requirement the
    booking is reserved as usual.
    PHONE_CARD_PAGE "site" instead texts the booking's manage link on the
    restaurant's own site: the booking gets a SetupIntent exactly like an
    online one (guaranteeLink.page "site"), the manage page's GET hands it
    the card form (get-reservation) and POST .../confirm or the webhook
    confirms it. Only for a tenant whose row has cardStepOnManagePage true
    (its live site runs site-kit 0.4.0 or later, which has the card step);
    without it, or without a site address or link key: the Stripe page.
    Either page reaches the guest as a short plain link (SHORT_LINK_BASE,
    shared/short_link.py: <base>/<10 random characters>, stored in the same
    transaction, valid while the booking is held; GET /k/{code} in
    get-reservation redirects to it): guaranteeLink.url is that short link.

    Multi-tenant: the location decides the tenant (public route) or the
    token does (staff route, staff limited to their own location). Tenants
    without the "reservations" feature, or inactive ones, get 404.

ENV_VARS:
    ENVIRONMENT, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME, TENANT_TABLE_NAME,
    PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME, SLOT_OCCUPANCY_TABLE_NAME,
    RESERVATION_TABLE_NAME, USER_TABLE_NAME (staff location check),
    RESERVATION_RETENTION_DAYS (optional, default 395),
    RESERVATION_LINK_KEY_SECRET_ARN (manage link signing key)
    STRIPE_SECRET_ARN, STRIPE_API_VERSION (card guarantee)
    PHONE_CARD_PAGE (optional: "site" or "stripe", default "stripe"; a phone guest's card page)
    SHORT_LINK_BASE (optional: e.g. https://restaurang.booqy.se/api/k; the SMS
        link's short form, shared/short_link.py; unset: the long URL)
    BOT_CHECK_ENFORCED, SITE_WAF_SECRET (bot check, shared/bot_check.py)
    REQUEST_GUARD_TABLE_NAME (attempt limits, shared/request_guard.py)
    NOTIFICATION_FUNCTION_NAME (the notification:live alias ARN: staff phone
        bookings text the card link through it; unset: 503 sms_unavailable)
    PHONE_AGENT_TABLE_NAME (optional: the phone agent's phoneGuarantee
        "skip"; unset: a staff phone booking asks the card the policy asks)

AWS RESOURCE ACCESS:
    Read on the location and published layout tables; item access on slot
    occupancy, reservation and payment delinquency; GetSecretValue on the
    Stripe key and the link signing key; UpdateItem on the request-guard
    table; lambda:InvokeFunction on notification:live (the card link SMS,
    RequestResponse).

Full details: docs/RESERVATIONS.md
"""

import json
import logging
import os
from datetime import timedelta

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, ConnectionClosedError, ReadTimeoutError
from zoneinfo import ZoneInfo

from shared import availability, booking_rules, consent, guarantee, http, manage_link, marketing
from shared import bot_check, messages, payment_domains, request_guard, short_link, site, sms, tenant
from shared import reservations as r
from shared import stripe_client as stripe

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_ONLINE = "/locations/{locationId}/reservations"
_MANUAL = "/locations/{locationId}/reservations/manual"
_CONFIRM = "/locations/{locationId}/reservations/{reservationId}/confirm"
_HOLDS = "/locations/{locationId}/holds"
_HOLD = "/locations/{locationId}/holds/{holdId}"
_RESEND = "/locations/{locationId}/reservations/{reservationId}/guarantee-link"
_HOLD_FIELDS = {"date", "startTime", "tableIds", "partySize", "turnstileToken"}
_ONLINE_FIELDS = {
    "date", "startTime", "tableIds", "partySize", "name", "email", "phone",
    "notes", "language", "marketingOptIn", "acceptTerms", "turnstileToken", "holdId",
    "attribution", "marketingConsent",
}
_MANUAL_FIELDS = (_ONLINE_FIELDS - {"acceptTerms", "marketingOptIn", "turnstileToken", "holdId",
                                    "attribution", "marketingConsent"}) | {
    "source", "notifyGuest", "guarantee",
}
# Where a phone guest lands after the Stripe page when the site's address is unknown.
FALLBACK_RETURN_URL = "https://booqy.se"
# Online booking attempts: (max attempts, window seconds) per client.
IP_LIMIT = (10, 10 * 60)
CONTACT_LIMIT = (5, 24 * 60 * 60)
# Table holds per client - each blocks tables for up to 15 minutes.
HOLD_IP_LIMIT = (8, 15 * 60)
# A staff phone booking's "guarantee": "link" (the AI agent texts the card
# link itself), "sms" (Booqy texts it), "waived" (staff book without the
# card the policy asks for, logged in history).
GUARANTEE_MODES = (None, "link", "sms", "waived")
# Card link SMS Booqy sends (first texts and resends): per location a day and
# per staff user an hour, counted before Stripe is asked and given back when
# nothing was sent.
LINK_SMS_LOCATION_LIMIT = (60, 24 * 60 * 60)
LINK_SMS_STAFF_LIMIT = (20, 60 * 60)
# The SMS invoke of notification:live (RequestResponse, botocore retries
# off): connect 2 s, answer within 8 s (less when the Lambda is short of
# time). Below connect + 1 s + the release's 3 s left, nothing is sent.
LINK_SMS_CONNECT_SECONDS = 2
LINK_SMS_READ_SECONDS = 8
LINK_SMS_RELEASE_SECONDS = 3
# Stripe's answer when the card page of a released booking is closed.
CARD_PAGE_CLOSE_SECONDS = 5


class SmsUnavailable(Exception):
    """The card link SMS cannot be sent from this function
    (NOTIFICATION_FUNCTION_NAME unset): 503 sms_unavailable, nothing booked."""


class LinkSmsFailed(Exception):
    """The card link SMS did not go out: a new booking was released, a
    resend left the booking as it was. 502 sms_failed."""


def _guard_online(event, data, guest, now):
    """Attempt limits and the bot check, before anything is read or
    written for an online booking. Returns the phone/e-mail counters - given
    back if the attempt doesn't end in a booking (the IP address keeps it)."""
    ip = http.source_ip(event)
    request_guard.check([("ip", ip, *IP_LIMIT)], now)
    bot_check.verify(event, "booking")
    return request_guard.check([
        ("phone", guest.get("customerPhone"), *CONTACT_LIMIT),
        ("email", guest.get("customerEmail"), *CONTACT_LIMIT),
    ], now)


def _max_online_party(location):
    value = location.get("maxPartySizeOnline")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _create(event, *, staff, lambda_context=None):
    location_id = http.path_id(event, "locationId")
    if staff:
        ctx = tenant.for_jwt(event, location_id=location_id, feature="reservations", area=("bokningar", "layout"))
    else:
        ctx = tenant.for_public(location_id, feature="reservations")
    data = http.body(event, allowed=_MANUAL_FIELDS if staff else _ONLINE_FIELDS)

    date_str = r.validate_date(data.get("date"))
    start_time = r.validate_time(data.get("startTime"))
    table_ids = r.validate_table_ids(data.get("tableIds"))
    party_size = r.validate_party_size(data.get("partySize"))
    guest = r.validate_guest(data, require_contact=not staff)
    if not staff:
        # Which campaign / channel brought the guest (marketing statistics).
        # Never blocks a booking: anything malformed is just dropped.
        attribution = marketing.validate_attribution(data.get("attribution"))
        if attribution:
            guest["attribution"] = attribution
    if staff:
        source = data.get("source", "phone")
        if source not in r.SOURCES - {"online"}:
            raise ValueError("source must be phone, walk_in or staff")
        notify = r.notify_flag({"notifyGuest": data.get("notifyGuest", source != "walk_in")})
        mode = data.get("guarantee")
        if mode not in GUARANTEE_MODES:
            raise ValueError("guarantee must be link or sms")
        if mode and source != "phone":
            raise ValueError(f"guarantee {mode} is for phone bookings")
        if mode in ("link", "sms") and not guest.get("customerPhone"):
            raise ValueError(f"guarantee {mode} needs the guest's phone number")
        if source == "phone" and not guest.get("customerPhone") and not tenant.is_system_sub(ctx.sub):
            # Staff on the call always get the number (the card link and the
            # confirmation go to it); only the AI agent books a hidden caller ID.
            raise ValueError("phone is required for a phone booking")
    else:
        notify = True
        source = "online"
        if data.get("acceptTerms") is not True:
            raise ValueError("acceptTerms must be true")
        # Marketing consent: voluntary, never needed for the booking. The
        # subscriber list (marketing-sync) still reads marketingOptIn = e-mail.
        ticked = consent.parse_request(data.get("marketingConsent"), email=guest.get("customerEmail"),
                                       phone=guest.get("customerPhone"),
                                       legacy_opt_in=guest.get("marketingOptIn"), source="booking")
        guest["marketingOptIn"] = bool(ticked and consent.EMAIL in ticked[1])

    now = r.now_utc()
    booking = {
        "date_str": date_str, "start_time": start_time, "table_ids": table_ids,
        "party_size": party_size, "guest": guest, "source": source, "notify": notify,
    }
    if staff:
        return _book(ctx, location_id, now, staff=True, guarantee_mode=mode, lambda_context=lambda_context,
                     **booking)
    if data.get("holdId") is not None:
        # The tables are already the guest's (held); the bot check was done
        # when they were held - only the per-guest limits apply here. As in
        # _guard_online the IP address keeps its attempt; only the phone
        # and e-mail counters are given back when nothing is booked.
        request_guard.check([("ip", http.source_ip(event), *IP_LIMIT)], now)
        counted = request_guard.check([
            ("phone", guest.get("customerPhone"), *CONTACT_LIMIT),
            ("email", guest.get("customerEmail"), *CONTACT_LIMIT),
        ], now)
        try:
            response = _book_held(event, ctx, location_id, now, data.get("holdId"), **booking)
        except Exception:
            request_guard.refund(counted)
            raise
        return _with_consent(ctx, location_id, guest, ticked, response)
    counted = _guard_online(event, data, guest, now)
    try:
        response = _book(ctx, location_id, now, staff=False, **booking)
    except Exception:
        # Nothing was booked (taken table, closed day ...): the attempt
        # doesn't count against the guest's phone number and e-mail.
        request_guard.refund(counted)
        raise
    return _with_consent(ctx, location_id, guest, ticked, response)


def _with_consent(ctx, location_id, guest, ticked, response):
    """The consent log record, once the booking exists (shared/consent.py)."""
    consent.record_from_form(ctx.tenant, ctx.tenant_id, parsed=ticked, email=guest.get("customerEmail"),
                             phone=guest.get("customerPhone"), location_id=location_id, source="booking",
                             ref=consent.ref_from_response(response, "reservationId"))
    return response


def _checked_slot(ctx, location_id, now, *, staff, source, date_str, start_time, table_ids, party_size):
    """Validates the request against live availability. Returns
    (lock versions, day context, slot, seats)."""
    availability.set_tenant(ctx.tenant_id)
    # Lock versions first, then holds: a write in between bumps a version
    # and the transaction below fails instead of double-booking.
    versions = r.read_lock_versions(location_id, date_str, table_ids)
    walk_in = staff and source == "walk_in"
    context = availability.day_context(location_id, date_str, now, include_started=walk_in)
    if context is None:
        raise tenant.TenantError(404, "not found")
    if walk_in and date_str != now.astimezone(ZoneInfo(context["timezone"])).date().isoformat():
        # A walk-in is a guest standing in the restaurant: today only.
        raise ValueError("a walk-in is booked for today")
    if not staff and (context["day"] or {}).get("kind") == "no_online":
        raise ValueError("online booking is closed for this date")
    occupancies = availability.query_occupancies(location_id, date_str) if context["slots"] else []
    free = availability.free_tables(context, occupancies)
    slot, seats = r.check_tables(
        context, free, start_time, table_ids, party_size,
        None if staff else _max_online_party(ctx.location),
    )
    if not staff:
        offered = booking_rules.online_options(
            free[slot["startTime"]], party_size,
            context["tableRules"], context["tableCombinations"],
        )
        if table_ids not in [option["tableIds"] for option in offered]:
            raise ValueError("tableIds are not offered online for this party size")
    return versions, context, slot, seats


def _return_urls(ctx, item):
    """(success, cancel) of a phone guest's Stripe page: the booking's
    manage page (it confirms the booking and shows it booked) and the
    restaurant's public site (shared/site.py); booqy.se when unknown."""
    try:
        base, slug = site.base(ctx.tenant_id, ctx.tenant)
    except (BotoCoreError, ClientError, KeyError):
        base, slug = None, None
    if not base:
        return FALLBACK_RETURN_URL, FALLBACK_RETURN_URL
    home = site.page(base, slug, "/")
    try:
        return manage_link.url(base, item, slug=slug) or home, home
    except manage_link.LinkKeyUnavailable:
        return home, home


def phone_card_page():
    """PHONE_CARD_PAGE: where a phone guest adds the card - "site" (the
    booking's manage page on the restaurant's site) or "stripe" (a
    Stripe-hosted Checkout page, the default; also for any other value)."""
    return "site" if (os.environ.get("PHONE_CARD_PAGE") or "").strip().lower() == "site" else "stripe"


def _short_link_base(ctx):
    """Where the SMS card link points: <restaurant's own domain or slug
    address>/api/k when its site is live (restaurant sites send /api/* to this
    API), else SHORT_LINK_BASE (Booqy's admin host). None when short links
    are off (SHORT_LINK_BASE unset)."""
    default = short_link.base()
    if not default:
        return None
    try:
        host = site.canonical_host(ctx.tenant_id, ctx.tenant)
    except (BotoCoreError, ClientError, KeyError):
        host = None
    own = f"https://{host}/api/k" if host else None
    return own if own and short_link.PLAIN_URL.fullmatch(own) else default


def _wallet_domains(ctx, account):
    """Apple Pay and Google Pay in the site's card form need the site's
    hosts registered on the restaurant's Stripe account (best effort,
    shared/payment_domains.py); the card itself always works."""
    payment_domains.ensure(account, lambda: site.hosts(ctx.tenant_id, ctx.tenant))


def _site_card_page(ctx, item):
    """The booking's manage link on the restaurant's site, or None (then the
    Stripe page) when the tenant's live site has no card step yet (tenant
    row cardStepOnManagePage, set once its site runs site-kit 0.4.0 or
    later; an older manage page tells the guest the booking failed), or the
    site's address or the link key is unknown."""
    if (ctx.tenant or {}).get("cardStepOnManagePage") is not True:
        return None
    try:
        base, slug = site.base(ctx.tenant_id, ctx.tenant)
    except (BotoCoreError, ClientError, KeyError):
        return None
    try:
        return manage_link.url(base, item, slug=slug) if base else None
    except manage_link.LinkKeyUnavailable:
        return None


def _phone_card_skipped(tenant_id, location_id):
    """The location's phone agent setting phoneGuarantee "skip": phone
    bookings never need a card (the agent follows it as well). Read from the
    phone agent table when this function has it (PHONE_AGENT_TABLE_NAME);
    unset or unreadable: the card is asked."""
    if not os.environ.get("PHONE_AGENT_TABLE_NAME"):
        return False
    from shared.features import phone_agent as pa  # local: only staff phone bookings need it
    try:
        return pa.phone_guarantee(pa.get_loc(tenant_id, location_id)) == "skip"
    except (BotoCoreError, ClientError) as exc:
        logger.warning(json.dumps({"phoneGuarantee": "unreadable", "reason": type(exc).__name__}))
        return False


def _card_decision(ctx, location_id, now, starts_at, *, mode, party_size):
    """(card, skipped) of a staff booking: card = it waits pending for the
    card the guest adds from a phone link; skipped = why a card the policy
    asks for is not asked ("waived" by staff, "too_soon": the hold could
    lapse after the start, "phone_skip": the location's phoneGuarantee)."""
    if not mode or not guarantee.required(ctx.location, ctx.tenant, party_size):
        return False, None
    if mode == "waived":
        return False, "waived"
    if mode == "sms":
        if _phone_card_skipped(ctx.tenant_id, location_id):
            return False, "phone_skip"
        if not guarantee.phone_link_fits(now, starts_at):
            return False, "too_soon"
    return True, None


def _same_number(phone, other):
    try:
        return isinstance(other, str) and r.normalize_phone(other) == phone
    except ValueError:
        return False


def _own_mobile(ctx, phone):
    """A number the card link may go to: a Nordic mobile (sms.textable),
    not a filler like 070 000 00 00 and not the restaurant's own number."""
    return (sms.textable(phone) and not sms.filler(phone)
            and not _same_number(phone, (ctx.location or {}).get("phoneNumber")))


def _link_sms_limits(ctx, location_id):
    return [("link-sms-loc", location_id, *LINK_SMS_LOCATION_LIMIT),
            ("link-sms-sub", ctx.sub, *LINK_SMS_STAFF_LIMIT)]


def _claim_link_sms(ctx, location_id, item, now):
    """Before Stripe is asked or anything written: the number takes the card
    link SMS and the counters allow one more. Returns the counters (given
    back when nothing is sent)."""
    phone = item.get("customerPhone")
    if not sms.textable(phone):
        raise ValueError("guarantee sms needs a mobile number")
    if sms.filler(phone) or _same_number(phone, (ctx.location or {}).get("phoneNumber")):
        raise ValueError("guarantee sms needs the guest's own mobile number")
    if not os.environ.get("NOTIFICATION_FUNCTION_NAME"):
        raise SmsUnavailable()
    return request_guard.check(_link_sms_limits(ctx, location_id), now)


def _phone_card(ctx, item, now, ends_at, *, by):
    """A phone booking with the card guarantee: pending until the guest adds
    the card on the page the SMS links to. Updates the item (status, Stripe
    ids, guaranteeLink) and returns (the link for the answer, the short link
    row's Put or None)."""
    account = guarantee.stripe_account(ctx.tenant, ctx.location)
    expires = guarantee.phone_pending_expires(now)
    item.update({
        "status": r.PENDING,
        "pendingExpiresAt": r.iso(expires),
        "guarantee": guarantee.snapshot(ctx.location, item["partySize"]),
        "stripeAccountId": account,
        "history": [r.history_entry("created", item["createdBy"], now, status=r.PENDING, source=item["source"])],
    })
    page = _site_card_page(ctx, item) if phone_card_page() == "site" else None
    if page:
        # The restaurant's own card page: the booking's manage page shows
        # the card form, exactly as after an online booking (SetupIntent).
        _wallet_domains(ctx, account)
        customer_id, setup_intent_id, _secret = guarantee.start_setup(item, account)
        item.update({"stripeCustomerId": customer_id, "setupIntentId": setup_intent_id})
        url, done_url = page, page
    else:
        success_url, cancel_url = _return_urls(ctx, item)
        customer_id, session = guarantee.start_checkout(item, account, return_url=cancel_url,
                                                        success_url=success_url, lang=item.get("language"),
                                                        now=now)
        item.update({"stripeCustomerId": customer_id, "checkoutSessionId": session["id"]})
        url = session["url"]
        done_url = success_url if success_url != FALLBACK_RETURN_URL else None
    # The SMS carries a short plain link (shared/short_link.py), never the
    # long URL: phones cut a link at # or %, which broke the Stripe page.
    short_put = None
    link_base = _short_link_base(ctx)
    if link_base:
        code = short_link.new_code()
        short_put = short_link.put_op(code, table=r.reservation_table(), target=url, item=item,
                                      expires=expires, serialize=r.serialize, done_url=done_url,
                                      keep_until=ends_at)
        url = short_link.url(code, link_base)
    kind = "site" if page else "stripe"
    # Stored for resends and for staff to see who texted it (shared/guarantee.py
    # link_view). The url is a bearer link: never in a view, history or log.
    item["guaranteeLink"] = {"url": url, "page": kind, "by": by, "sends": 1}
    if by == "booqy":
        item["guaranteeLink"]["textedAt"] = r.iso(now)
    return {"url": url, "minutes": guarantee.PHONE_PENDING_MINUTES, "expiresAt": r.iso(expires),
            "page": kind}, short_put


def _book(ctx, location_id, now, *, staff, date_str, start_time, table_ids,
          party_size, guest, source, notify, guarantee_mode=None, lambda_context=None):
    versions, context, slot, seats = _checked_slot(
        ctx, location_id, now, staff=staff, source=source, date_str=date_str,
        start_time=start_time, table_ids=table_ids, party_size=party_size)

    reservation_id = r.new_id()
    starts_at, ends_at = slot["startUtc"], slot["endUtc"]
    actor = ctx.sub if staff else "guest"
    item = {
        **r.reservation_key(location_id, date_str, reservation_id),
        "reservationId": reservation_id,
        "tenantId": ctx.tenant_id,
        "locationId": location_id,
        "date": date_str,
        "startTime": slot["startTime"],
        "endTime": slot["endTime"],
        "bookedFor": r.iso(starts_at),
        "endsAt": r.iso(ends_at),
        "timezone": context["timezone"],
        "tableIds": table_ids,
        "seats": seats,
        "partySize": party_size,
        "layoutVersion": slot["layoutVersion"],
        "bookingRule": slot["rule"],
        "extendedMinutes": 0,
        **{k: v for k, v in guest.items() if v is not None},
        "source": source,
        "status": r.RESERVED,
        "linkVersion": 1,
        "termsAcceptedAt": r.iso(now) if source == "online" else None,
        "createdAt": r.iso(now),
        "createdBy": actor,
        "version": 1,
        "history": [r.history_entry("created", actor, now, status=r.RESERVED, source=source)],
        "ttl": r.retention_ttl(ends_at),
    }
    card, skipped = (_card_decision(ctx, location_id, now, starts_at, mode=guarantee_mode, party_size=party_size)
                     if staff else (False, None))
    if skipped == "too_soon":
        item["history"] = [r.history_entry("created", actor, now, status=r.RESERVED, source=source,
                                           cardSkipped="too_soon")]
    elif skipped == "waived":
        item["history"].append(r.history_entry("guarantee_waived", actor, now))
    texts = card and guarantee_mode == "sms"
    counted = _claim_link_sms(ctx, location_id, item, now) if texts else []
    try:
        setup = checkout = short_put = None
        if card:
            checkout, short_put = _phone_card(ctx, item, now, ends_at, by="booqy" if texts else "caller")
        elif not staff and guarantee.required(ctx.location, ctx.tenant, party_size):
            # Card guarantee: pending until the guest confirms the card; the
            # confirmation (notice) is sent then, not now.
            account = guarantee.stripe_account(ctx.tenant, ctx.location)
            expires = now + timedelta(minutes=guarantee.PENDING_MINUTES)
            item.update({
                "status": r.PENDING,
                "pendingExpiresAt": r.iso(expires),
                "guarantee": guarantee.snapshot(ctx.location, party_size),
                "stripeAccountId": account,
                "history": [r.history_entry("created", actor, now, status=r.PENDING, source=source)],
            })
            _wallet_domains(ctx, account)
            customer_id, setup_intent_id, client_secret = guarantee.start_setup(item, account)
            item.update({"stripeCustomerId": customer_id, "setupIntentId": setup_intent_id})
            setup = {"clientSecret": client_secret, "stripeAccount": account, "expiresAt": r.iso(expires)}
        elif notify and r.can_be_notified(item):
            item["notice"] = r.notice(r.NOTICE_CONFIRMED, now)
        # Built before the write: a missing signing key fails the request
        # instead of leaving a booking whose link can't be sent.
        manage_token = None if staff else manage_link.token_for(item)
        pointer = {
            **r.pointer_key(location_id, reservation_id),
            "reservationId": reservation_id,
            "date": date_str,
            "tenantId": ctx.tenant_id,
            "ttl": item["ttl"],
        }
        r.transact(
            r.lock_updates(location_id, date_str, versions)
            + r.hold_items(location_id, date_str, slot["startTime"], slot["endTime"],
                           table_ids, reservation_id, ends_at, now=now)
            + [
                {"Put": {"TableName": r.reservation_table(), "Item": r.serialize(item),
                         "ConditionExpression": "attribute_not_exists(PK)"}},
                {"Put": {"TableName": r.reservation_table(), "Item": r.serialize(pointer),
                         "ConditionExpression": "attribute_not_exists(PK)"}},
            ]
            + ([guarantee.marker_put(item)] if setup or checkout else [])
            + ([short_put] if short_put else [])
        )
    except Exception:
        # Nothing was booked or texted: the SMS counters are given back.
        request_guard.refund(counted)
        raise

    if texts:
        # Booqy texts the link now, while the staff are still on the call: a
        # failed SMS releases the booking at once (502) instead of a hold
        # nobody can complete.
        text = messages.guarantee_link_sms(item, ctx.tenant, ctx.location, item["guaranteeLink"]["url"],
                                           guarantee.PHONE_PENDING_MINUTES)
        result, maybe_sent = _send_link(ctx, item, text, lambda_context)
        if result != "sent":
            if not maybe_sent:
                request_guard.refund(counted)
            booked = _release_unsent(item, now, actor, lambda_context)
            if booked is None:
                raise LinkSmsFailed()
            return http.respond(201, r.staff_view(booked))   # the card came in that same instant

    if staff:
        body = r.staff_view(item)
        if texts:
            body["guaranteeLink"] = {**guarantee.link_view(item, now), "minutes": checkout["minutes"],
                                     "expiresAt": checkout["expiresAt"], "page": checkout["page"], "sms": "sent"}
        elif checkout:
            body["guaranteeLink"] = checkout     # the caller (AI agent) texts it: the url, as before
        if skipped == "too_soon":
            body["guaranteeSkipped"] = "too_soon"
        return http.respond(201, body)
    body = r.guest_view(item, ctx.location, now=now)
    if manage_token:
        body["manageToken"] = manage_token
    if setup:
        body["setup"] = setup
    return http.respond(201, body)


# --- the card link SMS (notification:live, RequestResponse) -----------------------------------

_lambda_clients = {}
# The invoke reached notification but no answer came back (a read timeout or
# a connection closed after the request went out): the SMS may have gone out.
_MAYBE_SENT = (ReadTimeoutError, ConnectionClosedError)


def _lambda_client(read_timeout=LINK_SMS_READ_SECONDS):
    """One cached client per read timeout; botocore never retries (a retry
    could text the guest twice)."""
    client = _lambda_clients.get(read_timeout)
    if client is None:
        client = boto3.client("lambda", config=Config(
            connect_timeout=LINK_SMS_CONNECT_SECONDS, read_timeout=read_timeout,
            retries={"total_max_attempts": 1}))
        _lambda_clients[read_timeout] = client
    return client


def _text_link(ctx, item, text, read_timeout=LINK_SMS_READ_SECONDS):
    """Texts the card link to the booking's own phone through notification
    (the feature notice, kind reservations.guarantee-link) and waits for the
    answer. Returns (result, reason, maybe_sent): ("sent", None, False) or
    ("failed", reason, maybe_sent). Any error, a FunctionError or a dropped
    notice is a failure; maybe_sent is True when the request reached
    notification but its answer was lost (the guest may have the SMS)."""
    notice = {"version": 1, "kind": "reservations.guarantee-link", "tenantId": ctx.tenant_id,
              "locationId": item["locationId"], "channel": "sms", "to": item["customerPhone"], "text": text,
              "replyTo": None}
    try:
        answer = _lambda_client(read_timeout).invoke(
            FunctionName=os.environ["NOTIFICATION_FUNCTION_NAME"], InvocationType="RequestResponse",
            Payload=json.dumps({"notice": notice}).encode("utf-8"))
        if answer.get("FunctionError"):
            return "failed", "function_error", False
        out = json.loads(answer["Payload"].read() or b"{}")
    except ClientError as exc:
        return "failed", exc.response.get("Error", {}).get("Code") or "ClientError", False
    except (BotoCoreError, ValueError, KeyError) as exc:
        return "failed", type(exc).__name__, isinstance(exc, _MAYBE_SENT)
    if not isinstance(out, dict):
        return "failed", "dropped", False
    if out.get("status") == "sent":
        return "sent", None, False
    return "failed", str(out.get("reason") or "dropped"), False


def _remaining_seconds(lambda_context):
    remaining = getattr(lambda_context, "get_remaining_time_in_millis", None)
    return remaining() / 1000 if callable(remaining) else None


def _send_link(ctx, item, text, lambda_context):
    """_text_link within the Lambda's time: with too little left to wait for
    the answer and still release the booking, nothing is sent. Logged
    without the phone, the text or the link. Returns (result, maybe_sent)."""
    seconds = _remaining_seconds(lambda_context)
    if seconds is not None and seconds < LINK_SMS_CONNECT_SECONDS + 1 + LINK_SMS_RELEASE_SECONDS:
        result, reason, maybe_sent = "failed", "no_time", False
    else:
        read_timeout = LINK_SMS_READ_SECONDS if seconds is None else \
            max(1, min(LINK_SMS_READ_SECONDS, int(seconds - 4)))
        result, reason, maybe_sent = _text_link(ctx, item, text, read_timeout)
    (logger.info if result == "sent" else logger.warning)(json.dumps({
        "guaranteeLinkSms": result, "reservationId": item["reservationId"], "reason": reason,
        "maybeSent": maybe_sent}))
    return result, maybe_sent


def _release_unsent(item, now, actor, lambda_context):
    """The card link SMS of a new booking did not go out: release it
    (expired, tables free, marker gone, history link_failed, no notice),
    then close its card page and delete its Stripe Customer (the guest's
    name and number, for a booking that never was). Returns the booking
    when the guest added the card in that same instant (reserved), else
    None. When it cannot be released (a DynamoDB error, or it kept changing
    while still pending) it is logged as an error and left to the expiry
    sweep with its card page open: a booking that still holds tables keeps
    a working link (the resend), never a dead one."""
    current, released, reason = item, False, None
    for _ in range(2):
        try:
            r.transact(guarantee.expire_items(current, now, action="link_failed", actor=actor))
            released = True
            break
        except r.Conflict:
            reason = "conflict"
            try:
                current = r.load(item["locationId"], item["reservationId"])
            except r.NotFound:
                reason = "not_found"
                break
            except (BotoCoreError, ClientError) as exc:
                reason = type(exc).__name__
                break
            if current.get("status") == r.RESERVED:
                return current
            if current.get("status") != r.PENDING:
                released = True     # cancelled or expired meanwhile: nothing holds its tables
                break
        except (BotoCoreError, ClientError) as exc:
            reason = type(exc).__name__
            break
    if not released:
        logger.error(json.dumps({"guaranteeLinkRelease": "failed", "reservationId": item["reservationId"],
                                 "reason": reason}))
        return None
    closed = _within_time(lambda_context, lambda timeout: guarantee.close_card_page(item, timeout=timeout))
    forgotten = _within_time(lambda_context, lambda timeout: guarantee.forget_customer(item, timeout=timeout))
    logger.info(json.dumps({"guaranteeLinkPage": closed, "guaranteeLinkCustomer": forgotten,
                            "reservationId": item["reservationId"]}))
    return None


def _within_time(lambda_context, call):
    """A best effort Stripe call bounded by CARD_PAGE_CLOSE_SECONDS and the
    Lambda's time left; "no_time" when under 2 seconds are left."""
    seconds = _remaining_seconds(lambda_context)
    if seconds is not None and seconds < 2:
        return "no_time"
    return call(CARD_PAGE_CLOSE_SECONDS if seconds is None else max(1, min(CARD_PAGE_CLOSE_SECONDS, seconds - 1)))


def _resend(event, lambda_context=None):
    """Staff send the same card link again to the booking's own phone (the
    guest says nothing arrived): only while it is pending, at most
    guarantee.MAX_LINK_SMS texts on the booking's counter and while RESEND_MIN_MINUTES of
    the window are left. The counter is claimed before the SMS goes, so a
    double click texts once (the other gets 409 changed_retry); a failed SMS
    keeps the booking and the counter."""
    location_id = http.path_id(event, "locationId")
    ctx = tenant.for_jwt(event, location_id=location_id, feature="reservations", area=("bokningar", "layout"))
    http.body(event, allowed=set(), required=False)
    item = r.load(location_id, http.path_id(event, "reservationId"))
    link = item.get("guaranteeLink") if isinstance(item.get("guaranteeLink"), dict) else {}
    if item.get("status") != r.PENDING or not link.get("url"):
        raise r.InvalidState("not_pending")
    now = r.now_utc()
    left = guarantee.link_minutes_left(item, now)
    if left < guarantee.RESEND_MIN_MINUTES:
        raise r.InvalidState("link_closed")
    sends = int(link.get("sends") or 1)
    if sends >= guarantee.MAX_LINK_SMS:
        raise r.InvalidState("resend_limit")
    if not _own_mobile(ctx, item.get("customerPhone")):
        raise r.InvalidState("not_textable")   # e.g. the phone was edited to the restaurant's own
    if not os.environ.get("NOTIFICATION_FUNCTION_NAME"):
        raise SmsUnavailable()
    counted = request_guard.check(_link_sms_limits(ctx, location_id), now)
    entry = r.history_entry("link_resent", ctx.sub, now, sends=sends + 1)
    try:
        r.transact([{"Update": {
            "TableName": r.reservation_table(),
            "Key": r.serialize(r.reservation_key(location_id, item["date"], item["reservationId"])),
            "UpdateExpression": ("SET guaranteeLink.sends = :next, guaranteeLink.textedAt = :now, "
                                 "updatedAt = :now, updatedBy = :by, "
                                 "history = list_append(if_not_exists(history, :empty), :entry), "
                                 "version = if_not_exists(version, :zero) + :one"),
            "ConditionExpression": "#s = :pending AND guaranteeLink.sends = :old AND pendingExpiresAt = :exp",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": r.serialize({
                ":next": sends + 1, ":now": r.iso(now), ":by": ctx.sub, ":entry": [entry], ":empty": [],
                ":zero": 0, ":one": 1, ":pending": r.PENDING, ":old": sends, ":exp": item["pendingExpiresAt"],
            }),
        }}])
    except Exception:
        request_guard.refund(counted)
        raise
    updated = {**item, "guaranteeLink": {**link, "sends": sends + 1, "textedAt": r.iso(now)},
               "updatedAt": r.iso(now), "updatedBy": ctx.sub, "version": int(item.get("version") or 0) + 1,
               "history": list(item.get("history") or []) + [entry]}
    text = messages.guarantee_link_sms(item, ctx.tenant, ctx.location, link["url"], left)
    result, maybe_sent = _send_link(ctx, item, text, lambda_context)
    if result != "sent":
        if not maybe_sent:
            request_guard.refund(counted)
        raise LinkSmsFailed()
    return http.respond(200, r.staff_view(updated))


def _hold(event):
    """The guest confirmed their table(s): claim them for HOLD_MINUTES."""
    location_id = http.path_id(event, "locationId")
    ctx = tenant.for_public(location_id, feature="reservations")
    data = http.body(event, allowed=_HOLD_FIELDS)
    date_str = r.validate_date(data.get("date"))
    start_time = r.validate_time(data.get("startTime"))
    table_ids = r.validate_table_ids(data.get("tableIds"))
    party_size = r.validate_party_size(data.get("partySize"))
    now = r.now_utc()
    ip = http.source_ip(event)
    request_guard.check([("hold-ip", ip, *HOLD_IP_LIMIT)], now)
    bot_check.verify(event, "hold")

    versions, context, slot, seats = _checked_slot(
        ctx, location_id, now, staff=False, source="online", date_str=date_str,
        start_time=start_time, table_ids=table_ids, party_size=party_size)
    hold_id = r.new_id()
    expires = now + timedelta(minutes=r.HOLD_MINUTES)
    starts_at, ends_at = slot["startUtc"], slot["endUtc"]
    item = {
        **r.reservation_key(location_id, date_str, hold_id),
        "reservationId": hold_id,
        "tenantId": ctx.tenant_id,
        "locationId": location_id,
        "date": date_str,
        "startTime": slot["startTime"],
        "endTime": slot["endTime"],
        "bookedFor": r.iso(starts_at),
        "endsAt": r.iso(ends_at),
        "timezone": context["timezone"],
        "tableIds": table_ids,
        "seats": seats,
        "partySize": party_size,
        "layoutVersion": slot["layoutVersion"],
        # The rule of the start time, kept by the booking made from the hold.
        "bookingRule": slot["rule"],
        "extendedMinutes": 0,
        "source": "online",
        "status": r.HELD,
        "holdOnly": True,
        "fromHold": True,
        "pendingExpiresAt": r.iso(expires),
        "linkVersion": 1,
        "createdAt": r.iso(now),
        "createdBy": "guest",
        "version": 1,
        "history": [r.history_entry("held", "guest", now, status=r.HELD, source="online")],
        # An abandoned hold is gone from the table two days after it ran out.
        "ttl": int(expires.timestamp()) + 2 * 86400,
    }
    token = manage_link.token_for(item)
    pointer = {
        **r.pointer_key(location_id, hold_id),
        "reservationId": hold_id, "date": date_str, "tenantId": ctx.tenant_id,
        "ttl": r.retention_ttl(ends_at),
    }
    r.transact(
        r.lock_updates(location_id, date_str, versions)
        + r.hold_items(location_id, date_str, slot["startTime"], slot["endTime"],
                       table_ids, hold_id, ends_at, now=now, hold_expires=expires)
        + [
            {"Put": {"TableName": r.reservation_table(), "Item": r.serialize(item),
                     "ConditionExpression": "attribute_not_exists(PK)"}},
            {"Put": {"TableName": r.reservation_table(), "Item": r.serialize(pointer),
                     "ConditionExpression": "attribute_not_exists(PK)"}},
            guarantee.marker_put(item),
        ]
    )
    return http.respond(201, {
        "holdId": hold_id,
        "manageToken": token,
        "expiresAt": r.iso(expires),
        "holdMinutes": r.HOLD_MINUTES,
        "date": date_str,
        "startTime": slot["startTime"],
        "endTime": slot["endTime"],
        "tableIds": table_ids,
        "partySize": party_size,
        "durationMinutes": slot["durationMinutes"],
        "extendable": slot["rule"]["extensionAllowed"],
        "ruleName": slot["rule"].get("name"),
        "cardRequired": guarantee.required(ctx.location, ctx.tenant, party_size),
    })


def _held(event, location_id, hold_id):
    item = r.load(location_id, hold_id)
    if not item.get("fromHold") or not manage_link.matches(item, http.header(event, "x-manage-token")):
        raise r.NotFound
    return item


def _release(event):
    """The guest let go of the tables (timer ran out, changed their mind).
    Idempotent: 200 also when it already ran out, was released or became a
    booking (a booking is never cancelled through this route)."""
    location_id = http.path_id(event, "locationId")
    tenant.for_public(location_id, feature="reservations")
    http.body(event, allowed=set(), required=False)
    item = _held(event, location_id, http.path_id(event, "holdId"))
    released = False
    if item["status"] == r.HELD:
        now = r.now_utc()
        try:
            r.transact(guarantee.expire_items(item, now, action="hold_released", actor="guest"))
            released = True
        except r.Conflict:
            pass  # turned into a booking or expired in the same instant - nothing to let go
    return http.respond(200, {"released": released})


def _book_held(event, ctx, location_id, now, hold_id, *, date_str, start_time, table_ids,
               party_size, guest, source, notify):
    """POST .../reservations with holdId: the held tables become the booking."""
    if not isinstance(hold_id, str) or not r.valid_reservation_id(hold_id):
        raise ValueError("holdId is invalid")
    item = _held(event, location_id, hold_id)
    if item["status"] != r.HELD or r.parse_iso(item["pendingExpiresAt"]) <= now:
        raise r.InvalidState("hold_expired")
    if (item["date"] != date_str or item["startTime"] != start_time
            or list(item["tableIds"]) != table_ids or int(item["partySize"]) != party_size):
        raise ValueError("the booking must be for the held date, time, tables and party size")

    booked = {k: v for k, v in item.items() if k not in ("holdOnly", "history")}
    booked.update({k: v for k, v in guest.items() if v is not None})
    booked.update({
        "termsAcceptedAt": r.iso(now),
        "updatedAt": r.iso(now),
        "updatedBy": "guest",
        "version": int(item.get("version") or 1) + 1,
        "ttl": r.retention_ttl(r.parse_iso(item["endsAt"])),
    })
    history = list(item.get("history") or [])
    setup = None
    extra = []
    if guarantee.required(ctx.location, ctx.tenant, party_size):
        # Card step next - it keeps the hold's clock (same pendingExpiresAt,
        # same marker, slot rows still expire with it).
        account = guarantee.stripe_account(ctx.tenant, ctx.location)
        booked.update({"status": r.PENDING, "guarantee": guarantee.snapshot(ctx.location, party_size),
                       "stripeAccountId": account})
        _wallet_domains(ctx, account)
        customer_id, setup_intent_id, client_secret = guarantee.start_setup(booked, account)
        booked.update({"stripeCustomerId": customer_id, "setupIntentId": setup_intent_id})
        setup = {"clientSecret": client_secret, "stripeAccount": account,
                 "expiresAt": item["pendingExpiresAt"]}
        history.append(r.history_entry("created", "guest", now, status=r.PENDING, source=source))
    else:
        booked["status"] = r.RESERVED
        booked.pop("pendingExpiresAt", None)
        if notify and r.can_be_notified(booked):
            booked["notice"] = r.notice(r.NOTICE_CONFIRMED, now)
        history.append(r.history_entry("created", "guest", now, status=r.RESERVED, source=source))
        extra = guarantee.marker_delete_ops(item) + r.keep_hold_items(item)
    booked["history"] = history

    try:
        r.transact([{"Put": {
            "TableName": r.reservation_table(), "Item": r.serialize(booked),
            "ConditionExpression": "#s = :held AND pendingExpiresAt > :now AND version = :v",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": r.serialize({":held": r.HELD, ":now": r.iso(now),
                                                       ":v": int(item.get("version") or 1)}),
        }}] + extra)
    except r.Conflict:
        raise r.InvalidState("hold_expired") from None
    body = r.guest_view(booked, ctx.location, now=now)
    body["manageToken"] = manage_link.token_for(booked)
    if setup:
        body["setup"] = setup
    return http.respond(201, body)


def _confirm(event):
    """The guest finished the card form: verify the SetupIntent with Stripe
    and turn the pending booking reserved. Idempotent."""
    location_id = http.path_id(event, "locationId")
    ctx = tenant.for_public(location_id, feature="reservations")
    http.body(event, allowed=set(), required=False)
    item = r.load(location_id, http.path_id(event, "reservationId"))
    if not manage_link.matches(item, http.header(event, "x-manage-token")):
        raise r.NotFound
    now = r.now_utc()
    item = guarantee.confirm_if_ready(item, now, "guest")
    return http.respond(200, r.guest_view(item, ctx.location, now=now))


def handler(event, context):
    method, path = http.route(event)
    if path not in (_ONLINE, _MANUAL, _CONFIRM, _HOLDS, _HOLD, _RESEND):
        return http.error(404, "not found")
    allowed = "DELETE" if path == _HOLD else "POST"
    if method != allowed:
        return http.respond(405, {"error": "method not allowed"}, {"Allow": allowed})
    try:
        if path == _HOLDS:
            return _hold(event)
        if path == _HOLD:
            return _release(event)
        if path == _CONFIRM:
            return _confirm(event)
        if path == _RESEND:
            return _resend(event, context)
        return _create(event, staff=path == _MANUAL, lambda_context=context)
    except tenant.TenantError as exc:
        return exc.response()
    except request_guard.Limited as exc:
        if exc.kind.startswith("link-sms"):
            # The body repeats Retry-After: the admin calls the API from
            # another origin, where the browser does not expose that header.
            return http.respond(429, {"error": "sms_limit", "retryAfter": exc.retry_after},
                                {"Retry-After": str(exc.retry_after)})
        return http.respond(429, {"error": "too_many_requests"}, {"Retry-After": str(exc.retry_after)})
    except bot_check.Unavailable:
        return http.error(503, "verification_unavailable")
    except SmsUnavailable:
        return http.error(503, "sms_unavailable")
    except LinkSmsFailed:
        return http.error(502, "sms_failed")
    except r.NotFound:
        return http.error(404, "not found")
    except r.InvalidState as exc:
        return http.error(409, exc.code)
    except r.Conflict as exc:
        return http.error(409, exc.code)
    except (stripe.StripeError, stripe.StripeUnavailable):
        return http.error(503, "payment_unavailable")
    except ValueError as exc:
        return http.error(400, str(exc))
    except availability.AvailabilityConflict:
        return http.error(409, "availability_changed")
    except (BotoCoreError, ClientError, availability.AvailabilityServiceFailure,
            manage_link.LinkKeyUnavailable):
        return http.error(503, "reservation service unavailable")

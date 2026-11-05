"""events-tickets

TRIGGER:
    API Gateway -- Auth: JWT (tenant pool), feature `events` (off -> 403)
    GET    /locations/{locationId}/events                                       K1 owner, staff
    POST   /locations/{locationId}/events                                       K2 owner (draft)
    GET    /locations/{locationId}/events/{eventId}                             K3 owner, staff (staff: no revenue)
    PUT    /locations/{locationId}/events/{eventId}                             K4 owner
    POST   /locations/{locationId}/events/{eventId}/publish                     K5 owner (series generated)
    PUT    /locations/{locationId}/events/{eventId}/ticket-types/{typeId}       K6 owner (edit: new price/VAT for
                                                                                later sales only; cap >= sold + held)
    DELETE /locations/{locationId}/events/{eventId}/ticket-types/{typeId}       K7 owner (only unsold)
    PUT    /locations/{locationId}/events/{eventId}/occurrences/{occurrenceId}  K8 owner (capacity >= sold + held)
    POST   .../occurrences/{occurrenceId}/cancel                                K9 owner: 202 mass refund job
    GET    .../occurrences/{occurrenceId}/guest-list                            K10 owner, staff (scanKey, offline)
    POST   .../occurrences/{occurrenceId}/check-ins                             K11 owner, staff (batch, clientId)
    GET    /locations/{locationId}/events/{eventId}/orders                      K12 owner
    POST   /locations/{locationId}/events/tickets/{ticketId}/refund             K13 owner
    GET    /locations/{locationId}/events/jobs/{jobId}                          K14 owner

    API Gateway -- Auth: NONE (feature off, unknown or foreign -> 404)
    GET    /public/locations/{locationId}/events                                K15
    GET    /public/locations/{locationId}/events/{eventId}                      K16
    POST   /public/locations/{locationId}/events/checkout                       K17 Turnstile + request guard;
                                                                                `lines` or `resaleTransferIds`
    GET    /public/tickets/{token}                                              K18
    GET    /public/tickets/{token}/pass                                         K19 Wallet pass (EVENTS_WALLET_MODE,
                                                                                off by default -> 404)
    POST   /public/tickets/{token}/transfers                                    K20 gift (request guard per
                                                                                ticket and IP) or resale listing
    POST   /public/ticket-transfers/{transferToken}/accept                      K21
    DELETE /public/tickets/{token}/transfers/{transferId}                       K22 (gift or listing)

    Lambda invoke (no requestContext): {"hook": ...} stripe.event, accounting.events,
    insights.events, privacy.export, privacy.erase, tenant.purge;
    EventBridge Scheduler {"job": "sweep"} every 5 minutes (hold expiry, owed
    Stripe refunds, mass refund jobs; stops before the Lambda's time runs out
    and continues next time) and {"job": "nightly"} (24-month contact clean-up).

PURPOSE:
    Evenemang och biljetter (ADR-K). Capacity is a conditional counter
    (`seatsLeft >= qty`) taken with the pending order in one transaction; tickets
    are issued only from Stripe's payment_intent.succeeded on the order's own
    connected account (free tickets at once). Money moves are append-only
    TX rows. VAT per ticket type from the accountant-approved list: NEEDS
    ACCOUNTANT REVIEW.

    Resale at face value (ADR-K 8, event setting `resaleAllowed`): the holder
    lists a ticket (K20 kind resale) at its own price; K16 shows the listings;
    a buyer's K17 checkout with `resaleTransferIds` claims them (the
    15-minute hold is the claim on the TRANSFER row, no capacity moves) and
    pays face value on the restaurant's account; the webhook voids the old
    ticket, issues the new one and books the seller's refund (same amount,
    Stripe key `ticket-resale-<transferId>`) in one transaction. Net zero in
    Bokföring; each ticket keeps its own price and VAT rate. The old QR is
    refused at the door (status void).

    Not built in this run (refused, never silently accepted): table pick
    (ADR-K 3).

    Event images (ADR-K 1, media.py): the menu image upload's key for this
    tenant and location plus required alt text; no S3 access here. Wallet
    passes (ADR-K 6, passes.py): off unless EVENTS_WALLET_MODE says
    `simulator` (never in prod) or `live` (secrets by ARN, a missing one hides
    that kind).

ENV_VARS:
    ENVIRONMENT, EVENTS_TICKETS_TABLE_NAME, TENANT_TABLE_NAME, LOCATION_TABLE_NAME,
    LOCATION_ID_INDEX_NAME, USER_TABLE_NAME, EVENTS_TICKETS_PEPPER_SECRET_ARN,
    STRIPE_SECRET_ARN, STRIPE_API_VERSION, TURNSTILE_REQUIRED, TURNSTILE_SECRET_ARN,
    REQUEST_GUARD_TABLE_NAME, NOTIFICATION_FUNCTION_NAME, EVENT_BUS_NAME,
    CLOUDFRONT_EDGE_SECRET, SITE_DOMAIN, CUSTOMER_SITE_URL;
    optional EVENTS_WALLET_MODE (off | simulator | live), EVENTS_TICKETS_APPLE_PASS_SECRET_ARN,
    EVENTS_TICKETS_GOOGLE_WALLET_SECRET_ARN
"""

import json
import logging
import os
from datetime import timedelta

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from shared import dynamo, events, http, orders, request_guard, site, tenant, turnstile
from shared import stripe_client as stripe
from shared.features import events_tickets as et
from shared.features.events_tickets import media, passes, qr, series

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CHECKOUT_IP_LIMIT = (10, 600)
TRANSFER_IP_LIMIT = (20, 3600)
TICKETS_SEND_GRACE_SECONDS = 120
JOB_BATCH = 200
INLINE_REFUNDS = 10  # K9 refunds this many tickets before answering 202; the sweep does the rest
RESALE_PER_ORDER = 4  # listed tickets one checkout may buy (4 transaction items each, 25 per transaction)
JOB_MARGIN_MS = 15_000  # the sweep stops starting new refunds this close to the Lambda timeout
_clients = {}
_invocation = {"time_left": None}  # this invocation's remaining-time getter (set by the handler)


def _time_left(context):
    """Milliseconds left for this invocation (a plain number outside Lambda)."""
    getter = getattr(context, "get_remaining_time_in_millis", None)
    return (lambda: int(getter())) if callable(getter) else (lambda: 10 ** 9)


def _in_time(time_left) -> bool:
    return time_left is None or time_left() > JOB_MARGIN_MS


def _client(name):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def _err(status, code, field=None):
    body = {"error": code, "code": code}
    if field:
        body["field"] = field
    return http.respond(status, body)


def _publish(detail_type, detail):
    try:
        events.publish(et.FEATURE_ID, detail_type, detail)
    except Exception as exc:
        logger.warning(json.dumps({"event": detail_type, "publish": "failed", "type": type(exc).__name__}))


def _kr(ore) -> str:
    ore = int(ore)
    return f"{ore // 100} kr" if ore % 100 == 0 else f"{ore / 100:.2f}".replace(".", ",") + " kr"


def _when(iso_value, location) -> str:
    at = et.parse_iso(iso_value)
    return at.astimezone(et.zone(location)).strftime("%Y-%m-%d %H:%M") if at else str(iso_value)


def _txn(items):
    dynamo.client().transact_write_items(TransactItems=items)


def _update(key, expr, values, condition=None, names=None):
    item = {"TableName": et.table_name(), "Key": et.ser(key), "UpdateExpression": expr}
    if values:
        item["ExpressionAttributeValues"] = et.ser(values)
    if condition:
        item["ConditionExpression"] = condition
    if names:
        item["ExpressionAttributeNames"] = names
    return {"Update": item}


def _put(item, condition="attribute_not_exists(PK)"):
    out = {"TableName": et.table_name(), "Item": et.ser(item)}
    if condition:
        out["ConditionExpression"] = condition
    return {"Put": out}


# --- notification (CONTRACTS.md 9.4) ----------------------------------------------------------

class NotificationUnavailable(RuntimeError):
    """notification:live did not take the email. A RuntimeError, so the Stripe
    path fails the hook and Stripe retries (the replay sends it)."""


def _notify(tenant_id, location_id, kind, to, subject, text):
    notice = {"version": 1, "kind": kind, "tenantId": tenant_id, "locationId": location_id, "channel": "email",
              "to": to, "subject": subject, "text": text, "replyTo": "location"}
    try:
        resp = _client("lambda").invoke(FunctionName=os.environ["NOTIFICATION_FUNCTION_NAME"],
                                        InvocationType="Event", Payload=json.dumps({"notice": notice}).encode("utf-8"))
    except (BotoCoreError, ClientError) as exc:
        raise NotificationUnavailable(f"notification invoke failed ({type(exc).__name__})") from exc
    if (resp or {}).get("StatusCode") != 202:
        raise NotificationUnavailable(f"notification invoke returned {(resp or {}).get('StatusCode')}")


def _site_link(tenant_id, path, token):
    tenant_row = tenant.get_tenant(tenant_id) or {}
    base, slug = site.base(tenant_id, tenant_row)
    return f"{site.page(base, slug, path)}#{token}" if base else None


def _send_tickets(tenant_id, order, event_row, occ, location):
    """One email with every ticket link (the QR is on the ticket page). Stamped
    only once notification took it, so a webhook replay sends it again."""
    if order.get("ticketsSentAt") or not order.get("buyerEmail"):
        return
    name = (location.get("name") or "restaurangen").strip()
    link = _site_link(tenant_id, "/biljett", qr.token("o", tenant_id, order["orderId"]))
    count = int(order["qty"])
    text = (f"Hej {order.get('buyerName') or ''}!\n\nTack för ditt köp. Här är "
            f"{'din biljett' if count == 1 else f'dina {count} biljetter'} till {event_row['title']} "
            f"på {name}, {_when(occ['startAt'], location)}.\n\n"
            + (f"Visa biljetterna i dörren: {link}\n\n" if link else "Visa det här mejlet i dörren.\n\n")
            + f"Totalt betalt: {_kr(order['amountOre'])} inklusive moms.\n"
            + (f"\nVillkor för återbetalning: {(event_row.get('settings') or {}).get('refundPolicy')}\n"
               if (event_row.get("settings") or {}).get("refundPolicy") else "")
            + "\nSpara mejlet: den som har länken kan visa biljetterna.\n")
    _notify(tenant_id, order["locationId"], "events-tickets.tickets", order["buyerEmail"],
            f"Dina biljetter till {event_row['title']}", text)
    et.table().update_item(Key=et.order_key(tenant_id, order["orderId"]), UpdateExpression="SET ticketsSentAt = :at",
                           ConditionExpression="attribute_exists(PK)",
                           ExpressionAttributeValues={":at": et.iso(et.now())})


# --- loading with 404s --------------------------------------------------------------------------

def _path(event, name):
    try:
        return http.path_id(event, name)
    except ValueError:
        raise et.EventsError(404, "not found")


def _event_or_404(ctx, event):
    ev = et.get_event(ctx.tenant_id, _path(event, "eventId"), ctx.location_id)
    if not ev:
        raise et.EventsError(404, "not found")
    return ev


def _occ_or_404(ctx, ev, event):
    start = et.split_occurrence(ev["eventId"], _path(event, "occurrenceId"))
    occ = et.get(et.occ_key(ctx.tenant_id, ev["eventId"], start)) if start else None
    if not occ:
        raise et.EventsError(404, "not found")
    return occ


def _owner(ctx):
    if not ctx.is_owner:
        raise et.EventsError(403, "owner_only")


def _version(body):
    version = body.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise et.EventsError(400, "version is required", "version")
    return version


# --- K1-K5 events ----------------------------------------------------------------------------

def list_events(event, ctx):
    params = http.query(event)
    if set(params) - {"status", "cursor"}:
        raise et.EventsError(400, "unsupported query parameters")
    status = params.get("status")
    if status and status not in et.EVENT_STATUSES:
        raise et.EventsError(400, "bad status", "status")
    rows = [e for e in et.events_of(ctx.tenant_id) if e.get("locationId") == ctx.location_id
            and (not status or e["status"] == status)]
    rows.sort(key=lambda e: e.get("createdAt") or "", reverse=True)
    start = 0
    if params.get("cursor"):
        key = et.decode_cursor(params["cursor"], ctx.tenant_id)
        start = next((i + 1 for i, e in enumerate(rows) if e["SK"] == key["SK"]), len(rows))
    page = rows[start:start + et.PAGE]
    at = et.iso(et.now())
    out = []
    for ev in page:
        occs, _ = et.event_parts(ctx.tenant_id, ev["eventId"])
        live = [o for o in occs if o["status"] == "scheduled"]
        upcoming = [o for o in live if o["endAt"] > at]
        out.append({"eventId": ev["eventId"], "title": ev["title"], "status": ev["status"],
                    "nextOccurrence": upcoming[0]["startAt"] if upcoming else None,
                    "sold": sum(int(o.get("sold") or 0) for o in live),
                    "capacity": sum(int(o["capacity"]) for o in live)})
    cursor = et.encode_cursor({"PK": page[-1]["PK"], "SK": page[-1]["SK"]}) \
        if page and start + et.PAGE < len(rows) else None
    return http.respond(200, {"events": out, "cursor": cursor})


def _event_fields(body, *, at, first_start=None):
    try:
        title = et.text(body.get("title"), et.TITLE_MAX, required=True)
    except ValueError:
        raise et.EventsError(400, "title is required (at most 120 characters)", "title")
    try:
        description = et.text(body.get("description"), et.DESCRIPTION_MAX) or ""
    except ValueError:
        raise et.EventsError(400, "description is at most 4000 characters", "description")
    out = {"title": title, "description": description, "settings": et.validate_settings(body.get("settings"))}
    if first_start is not None:
        out["series"] = et.validate_series(body.get("series"), first_start)
    return out


def _new_type(tenant_id, event_id, type_id, clean, at):
    item = {**et.type_key(tenant_id, event_id, type_id), "tenantId": tenant_id, "eventId": event_id,
            "typeId": type_id, **clean, "sold": 0, "held": 0, "taken": 0, "version": 1, "updatedAt": et.iso(at)}
    cap = _type_cap(item)
    if cap is not None:
        item["seatsLeft"] = cap
    return item


def _type_cap(t):
    caps = [int(c) for c in (t.get("capacity"), (t.get("tier") or {}).get("untilSold")) if c is not None]
    return min(caps) if caps else None


def _cap_guard(t) -> str:
    """The owner can add or remove a type's cap (K6) between our read of the
    type and a counter write: every write that moves `taken` is conditional
    on the type still being capped (or not) as read, so `seatsLeft` always
    equals cap - taken. A failed guard is retried with a fresh read."""
    return "attribute_exists(seatsLeft)" if "seatsLeft" in t else "attribute_not_exists(seatsLeft)"


def _new_occ(tenant_id, event_id, location_id, occ, at):
    start = et.compact(et.parse_iso(occ["startAt"]))
    return {**et.occ_key(tenant_id, event_id, start), "tenantId": tenant_id, "eventId": event_id,
            "locationId": location_id, "occurrenceId": et.occurrence_id(event_id, start), **occ,
            "sold": 0, "held": 0, "taken": 0, "seatsLeft": int(occ["capacity"]), "checkedIn": 0, "revenueOre": 0,
            "status": "scheduled", "version": 1, "createdAt": et.iso(at)}


def create_event(event, ctx):
    _owner(ctx)
    body = http.body(event, allowed=("eventId", "title", "description", "imageKey", "imageAlt", "occurrences",
                                     "series", "ticketTypes", "settings"))
    event_id = body.get("eventId")
    if not et.valid_id(event_id):
        raise et.EventsError(400, "eventId is invalid", "eventId")
    existing = et.get(et.event_key(ctx.tenant_id, event_id))
    if existing:  # a retry of the same create
        if existing.get("locationId") != ctx.location_id:
            raise et.EventsError(409, "changed_retry")
        occs, types = et.event_parts(ctx.tenant_id, event_id)
        return http.respond(201, et.event_view(existing, occs, types))
    at = et.now()
    raw_occs = body.get("occurrences")
    if not isinstance(raw_occs, list) or not 1 <= len(raw_occs) <= et.MAX_OCCURRENCES:
        raise et.EventsError(400, "invalid_window", "occurrences")
    occs = [et.validate_occurrence(o, at=at) for o in raw_occs]
    if len({o["startAt"] for o in occs}) != len(occs):
        raise et.EventsError(400, "invalid_window", "occurrences")
    fields = _event_fields(body, at=at, first_start=et.parse_iso(min(o["startAt"] for o in occs)))
    fields.update(media.clean_image(body, ctx.tenant_id, ctx.location_id) or {})
    if fields["series"] and len(occs) != 1:
        raise et.EventsError(400, "invalid_series", "series")
    raw_types = body.get("ticketTypes") or []
    if not isinstance(raw_types, list) or len(raw_types) > et.MAX_TYPES:
        raise et.EventsError(400, "unsupported ticketTypes", "ticketTypes")
    types = []
    for raw in raw_types:
        if not et.valid_id((raw or {}).get("typeId")):
            raise et.EventsError(400, "typeId is invalid", "typeId")
        types.append(_new_type(ctx.tenant_id, event_id, raw["typeId"], et.validate_type(raw), at))
    if len({t["typeId"] for t in types}) != len(types):
        raise et.EventsError(400, "typeId is repeated", "typeId")
    item = {**et.event_key(ctx.tenant_id, event_id), "tenantId": ctx.tenant_id, "eventId": event_id,
            "locationId": ctx.location_id, **{k: v for k, v in fields.items() if v is not None}, "status": "draft",
            "version": 1, "createdAt": et.iso(at), "updatedAt": et.iso(at), "updatedBy": ctx.sub}
    occ_items = [_new_occ(ctx.tenant_id, event_id, ctx.location_id, o, at) for o in occs]
    try:
        _txn([_put(item), *[_put(o) for o in occ_items], *[_put(t) for t in types]])
    except ClientError as exc:
        if not et.cancelled_txn(exc):
            raise
        raise et.EventsError(409, "changed_retry")
    return http.respond(201, et.event_view(item, sorted(occ_items, key=lambda o: o["startAt"]),
                                           sorted(types, key=lambda t: (t["sortOrder"], t["name"]))))


def get_event(event, ctx):
    ev = _event_or_404(ctx, event)
    occs, types = et.event_parts(ctx.tenant_id, ev["eventId"])
    view = et.event_view(ev, occs, types, money=ctx.is_owner)
    if (ev.get("settings") or {}).get("resaleAllowed"):
        # tickets other guests have put up for sale (the admin "view of resales"): one query for the event,
        # only occurrences that can still sell (a done or cancelled one has no live listing to count)
        open_ = [occ for occ in view["occurrences"] if occ["status"] == "scheduled"]
        listed = _listings(ctx.tenant_id, ev["eventId"], [occ["occurrenceId"] for occ in open_])
        for occ in open_:
            occ["resaleListed"] = len(listed[occ["occurrenceId"]])
    return http.respond(200, view)


def update_event(event, ctx):
    _owner(ctx)
    body = http.body(event, allowed=("title", "description", "imageKey", "imageAlt", "series", "settings", "version"))
    ev = _event_or_404(ctx, event)
    version = _version(body)
    if ev["status"] == "cancelled":
        raise et.EventsError(409, "invalid_state")
    occs, types = et.event_parts(ctx.tenant_id, ev["eventId"])
    first = et.parse_iso(occs[0]["startAt"]) if occs else et.now()
    fields = _event_fields(body, at=et.now(), first_start=first)
    image = media.clean_image(body, ctx.tenant_id, ctx.location_id)  # PUT: no imageKey removes the image
    if ev["status"] != "draft" and fields["series"] != ev.get("series"):
        raise et.EventsError(400, "invalid_series", "series")  # a published series is edited per occurrence
    if fields["series"] and len(occs) != 1:
        raise et.EventsError(400, "invalid_series", "series")
    sets = ["title = :t", "description = :d", "settings = :s", "version = version + :one", "updatedAt = :at",
            "updatedBy = :by"]
    values = {":t": fields["title"], ":d": fields["description"], ":s": fields["settings"], ":one": 1,
              ":at": et.iso(et.now()), ":by": ctx.sub, ":v": version}
    removes = []
    if fields["series"]:
        sets.insert(0, "series = :series")
        values[":series"] = fields["series"]
    else:
        removes.append("series")
    if image:
        sets += ["imageKey = :ik", "imageAlt = :ia"]
        values.update({":ik": image["imageKey"], ":ia": image["imageAlt"]})
    else:
        removes += ["imageKey", "imageAlt"]
    expr = "SET " + ", ".join(sets) + " REMOVE " + ", ".join(removes)
    try:
        et.table().update_item(Key=et.event_key(ctx.tenant_id, ev["eventId"]), UpdateExpression=expr,
                               ConditionExpression="version = :v", ExpressionAttributeValues=values)
    except ClientError as exc:
        if not et.conditional_failed(exc):
            raise
        raise et.EventsError(409, "changed_retry")
    ev = et.get(et.event_key(ctx.tenant_id, ev["eventId"]))
    return http.respond(200, et.event_view(ev, occs, types))


def _generate_series(tenant_id, ev, occs, location):
    """Writes the series' occurrences after the first; conditional Puts on the
    deterministic key, so a second run (or a crashed one) never duplicates."""
    if not ev.get("series") or not occs:
        return 0
    first = occs[0]
    start, end = et.parse_iso(first["startAt"]), et.parse_iso(first["endAt"])
    created = 0
    at = et.now()
    window = {}
    for field in ("salesOpenAt", "salesCloseAt", "doorsAt"):
        if first.get(field):
            window[field] = et.parse_iso(first[field]) - start
    for s, e in series.starts(start, end - start, ev["series"], et.zone(location))[1:]:
        occ = {"startAt": et.iso(s), "endAt": et.iso(e), "capacity": int(first["capacity"]),
               **{f: et.iso(s + delta) for f, delta in window.items()}}
        try:
            et.table().put_item(Item=_new_occ(tenant_id, ev["eventId"], ev["locationId"], occ, at),
                                ConditionExpression="attribute_not_exists(PK)")
            created += 1
        except ClientError as exc:
            if not et.conditional_failed(exc):
                raise
    return created


def publish_event(event, ctx):
    _owner(ctx)
    body = http.body(event, allowed=("version",))
    ev = _event_or_404(ctx, event)
    version = _version(body)
    occs, types = et.event_parts(ctx.tenant_id, ev["eventId"])
    if ev["status"] == "cancelled" or not types or not occs:
        raise et.EventsError(409, "invalid_state")
    _generate_series(ctx.tenant_id, ev, occs, ctx.location)
    if ev["status"] == "draft":
        try:
            et.table().update_item(
                Key=et.event_key(ctx.tenant_id, ev["eventId"]),
                UpdateExpression="SET #s = :p, version = version + :one, publishedAt = :at, updatedAt = :at",
                ConditionExpression="version = :v AND #s = :draft", ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":p": "published", ":one": 1, ":at": et.iso(et.now()), ":v": version,
                                           ":draft": "draft"})
        except ClientError as exc:
            if not et.conditional_failed(exc):
                raise
            again = et.get(et.event_key(ctx.tenant_id, ev["eventId"])) or {}
            if again.get("status") != "published":
                raise et.EventsError(409, "changed_retry")
        else:
            occs, types = et.event_parts(ctx.tenant_id, ev["eventId"])
            _publish("Event.Published", {"tenantId": ctx.tenant_id, "locationId": ev["locationId"],
                                         "eventId": ev["eventId"], "occurrences": len(occs)})
    ev = et.get(et.event_key(ctx.tenant_id, ev["eventId"]))
    occs, types = et.event_parts(ctx.tenant_id, ev["eventId"])
    return http.respond(200, et.event_view(ev, occs, types))


# --- K6-K8 types and occurrences -------------------------------------------------------------------

def put_type(event, ctx):
    _owner(ctx)
    body = http.body(event, allowed=("name", "priceOre", "vatRate", "capacity", "tier", "includes", "perOrderMax",
                                     "sortOrder", "version"))
    ev = _event_or_404(ctx, event)
    if ev["status"] == "cancelled":
        raise et.EventsError(409, "invalid_state")
    type_id = _path(event, "typeId")
    if not et.valid_id(type_id):
        raise et.EventsError(400, "typeId is invalid", "typeId")
    clean = et.validate_type(body, field="body")
    key = et.type_key(ctx.tenant_id, ev["eventId"], type_id)
    existing = et.get(key)
    at = et.now()
    if not existing:
        if body.get("version") is not None:
            raise et.EventsError(409, "changed_retry")
        count = len(et.event_parts(ctx.tenant_id, ev["eventId"])[1])
        if count >= et.MAX_TYPES:
            raise et.EventsError(400, "too many ticket types", "typeId")
        item = _new_type(ctx.tenant_id, ev["eventId"], type_id, clean, at)
        try:
            et.table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
        except ClientError as exc:
            if not et.conditional_failed(exc):
                raise
            item = et.get(key)
        return http.respond(200, et.type_view(item))
    if body.get("version") is None:  # a retry of the create
        return http.respond(200, et.type_view(existing))
    return _edit_type(ctx, ev, existing, clean, _version(body), at)


def _edit_type(ctx, ev, existing, clean, version, at):
    """Edit a ticket type after creation. Sold and held tickets are never
    touched: an order keeps the price and VAT rate on its lines and tickets,
    so a new price or rate applies to later checkouts only (a checkout that
    read the old price fails its hold with changed_retry). The cap may not go
    below `taken` (sold + held), checked in the same conditional write that
    sets `seatsLeft = cap - taken`, so a sale during the edit can never be
    oversold. A VAT change is allowed but flagged for the accountant
    (`vatReview`, append-only `vatChanges`). Once no occurrence can sell any
    more (all over or cancelled) a published event's types are frozen."""
    key = {"PK": existing["PK"], "SK": existing["SK"]}
    occs, _ = et.event_parts(ctx.tenant_id, ev["eventId"])
    if ev["status"] != "draft" and et.ended(occs, at):
        raise et.EventsError(409, "invalid_state")
    taken = int(existing.get("taken") or 0)
    cap = _type_cap(clean)
    cap_field = "capacity" if clean.get("capacity") is not None and cap == clean["capacity"] else "tier"
    if cap is not None and cap < taken:
        raise et.EventsError(400, "invalid_capacity", cap_field)
    sets = ["#n = :name", "priceOre = :p", "vatRate = :r", "perOrderMax = :pom", "sortOrder = :so",
            "version = version + :one", "updatedAt = :at", "updatedBy = :by"]
    values = {":name": clean["name"], ":p": clean["priceOre"], ":r": clean["vatRate"], ":pom": clean["perOrderMax"],
              ":so": clean["sortOrder"], ":one": 1, ":at": et.iso(at), ":v": version, ":by": ctx.sub or "owner"}
    old_rate = int(existing["vatRate"])
    if clean["vatRate"] != old_rate:
        sets += ["vatReview = :review", "vatChanges = list_append(if_not_exists(vatChanges, :none), :change)"]
        values.update({":review": et.VAT_REVIEW, ":none": [],
                       ":change": [{"fromRate": old_rate, "toRate": clean["vatRate"], "at": et.iso(at),
                                    "by": ctx.sub or "owner", "soldBefore": int(existing.get("sold") or 0)}]})
    removes = []
    for field in ("capacity", "tier", "includes"):
        name = "#cap" if field == "capacity" else field  # capacity is a DynamoDB reserved word
        if field in clean:
            sets.append(f"{name} = :{field}")
            values[f":{field}"] = clean[field]
        else:
            removes.append(name)
    condition = "version = :v"
    if cap is not None:
        sets.append("seatsLeft = :cap - taken")
        values[":cap"] = cap
        condition += " AND taken <= :cap"
    else:
        removes.append("seatsLeft")
    expr = "SET " + ", ".join(sets) + (" REMOVE " + ", ".join(removes) if removes else "")
    try:
        names = {"#n": "name", **({"#cap": "capacity"} if "#cap" in expr else {})}
        et.table().update_item(Key=key, UpdateExpression=expr, ConditionExpression=condition,
                               ExpressionAttributeNames=names, ExpressionAttributeValues=values)
    except ClientError as exc:
        if not et.conditional_failed(exc):
            raise
        again = et.get(key) or {}
        if int(again.get("version") or 0) == version and cap is not None and int(again.get("taken") or 0) > cap:
            raise et.EventsError(400, "invalid_capacity", cap_field)  # seats were sold during the edit
        raise et.EventsError(409, "changed_retry")
    if clean["vatRate"] != old_rate:
        logger.warning(json.dumps({"event": "ticket_type.vat_changed", "review": et.VAT_REVIEW,
                                   "tenantId": ctx.tenant_id, "eventId": ev["eventId"], "typeId": existing["typeId"],
                                   "fromRate": old_rate, "toRate": clean["vatRate"]}))
    return http.respond(200, et.type_view(et.get(key)))


def delete_type(event, ctx):
    _owner(ctx)
    ev = _event_or_404(ctx, event)
    key = et.type_key(ctx.tenant_id, ev["eventId"], _path(event, "typeId"))
    try:
        et.table().delete_item(Key=key, ConditionExpression="attribute_not_exists(PK) OR taken = :zero",
                               ExpressionAttributeValues={":zero": 0})
    except ClientError as exc:
        if not et.conditional_failed(exc):
            raise
        raise et.EventsError(409, "has_sales")
    return {"statusCode": 204, "headers": {"Cache-Control": "no-store"}, "body": ""}


def put_occurrence(event, ctx):
    _owner(ctx)
    body = http.body(event, allowed=("capacity", "salesOpenAt", "salesCloseAt", "doorsAt", "version"))
    ev = _event_or_404(ctx, event)
    occ = _occ_or_404(ctx, ev, event)
    version = _version(body)
    at = et.now()
    if ev["status"] == "cancelled" or occ["status"] != "scheduled" or occ["endAt"] <= et.iso(at):
        raise et.EventsError(409, "invalid_state")  # cancelled, done or already over
    capacity = int(occ["capacity"])
    if body.get("capacity") is not None:
        capacity = et.whole(body["capacity"], 1, et.MAX_CAPACITY)
        if capacity is None or capacity < int(occ.get("taken") or 0):
            raise et.EventsError(400, "invalid_capacity", "capacity")
    merged = {f: occ.get(f) for f in ("salesOpenAt", "salesCloseAt", "doorsAt")}
    merged.update({f: body[f] for f in ("salesOpenAt", "salesCloseAt", "doorsAt") if f in body})
    window = et._window(merged, start=et.parse_iso(occ["startAt"]), end=et.parse_iso(occ["endAt"]))
    sets = ["#cap = :c", "seatsLeft = :c - taken", "version = version + :one"]
    values = {":c": capacity, ":one": 1, ":v": version, ":sched": "scheduled"}
    removes = []
    for field in ("salesOpenAt", "salesCloseAt", "doorsAt"):
        if window.get(field):
            sets.append(f"{field} = :{field}")
            values[f":{field}"] = window[field]
        else:
            removes.append(field)
    expr = "SET " + ", ".join(sets) + (" REMOVE " + ", ".join(removes) if removes else "")
    try:
        et.table().update_item(Key={"PK": occ["PK"], "SK": occ["SK"]}, UpdateExpression=expr,
                               ConditionExpression="version = :v AND taken <= :c AND #s = :sched",
                               ExpressionAttributeNames={"#s": "status", "#cap": "capacity"},
                               ExpressionAttributeValues=values)
    except ClientError as exc:
        if not et.conditional_failed(exc):
            raise
        # The write is conditional on taken (sold + held) itself, so a sale between the read and the write
        # is never oversold: tell which condition failed.
        again = et.get({"PK": occ["PK"], "SK": occ["SK"]}) or {}
        if again.get("status") != "scheduled":
            raise et.EventsError(409, "invalid_state")
        if int(again.get("version") or 0) == version and int(again.get("taken") or 0) > capacity:
            raise et.EventsError(400, "invalid_capacity", "capacity")
        raise et.EventsError(409, "changed_retry")
    return http.respond(200, et.occurrence_view(et.get({"PK": occ["PK"], "SK": occ["SK"]}), money=True))


# --- K9, K14 cancel = mass refund -------------------------------------------------------------------

def _paid_tickets(tenant_id, occ_id):
    return [t for t in et.tickets_of_occurrence(tenant_id, occ_id) if t["status"] in ("valid", "checked_in")]


def cancel_occurrence(event, ctx):
    _owner(ctx)
    body = http.body(event, allowed=("jobId", "version", "message"))
    ev = _event_or_404(ctx, event)
    occ = _occ_or_404(ctx, ev, event)
    job_id = body.get("jobId")
    if not et.valid_id(job_id):
        raise et.EventsError(400, "jobId is invalid", "jobId")
    try:
        message = et.text(body.get("message"), et.MESSAGE_MAX)
    except ValueError:
        raise et.EventsError(400, "message is at most 500 characters", "message")
    job = et.get(et.job_key(ctx.tenant_id, job_id))
    if job:
        if job.get("occurrenceId") != occ["occurrenceId"]:
            raise et.EventsError(409, "changed_retry")
        return http.respond(202, {"jobId": job_id, "toRefund": int(job["toRefund"])})
    version = _version(body)
    if occ["status"] != "scheduled":
        raise et.EventsError(409, "invalid_state")
    to_refund = len(_paid_tickets(ctx.tenant_id, occ["occurrenceId"]))
    at = et.now()
    job = {**et.job_key(ctx.tenant_id, job_id), "tenantId": ctx.tenant_id, "jobId": job_id, "kind": "mass_refund",
           "eventId": ev["eventId"], "locationId": ev["locationId"], "occurrenceId": occ["occurrenceId"],
           "toRefund": to_refund, "done": False, "counts": {"refunded": 0, "failed": 0, "notified": 0},
           "createdAt": et.iso(at), "by": ctx.sub, "GSI1PK": "JOBS", "GSI1SK": f"{et.iso(at)}#{ctx.tenant_id}#{job_id}"}
    if message:
        job["message"] = message
    try:
        _txn([_update({"PK": occ["PK"], "SK": occ["SK"]}, "SET #s = :c, version = version + :one, cancelledAt = :at",
                      {":c": "cancelled", ":one": 1, ":at": et.iso(at), ":v": version, ":sched": "scheduled"},
                      "version = :v AND #s = :sched", {"#s": "status"}),
              _put(job)])
    except ClientError as exc:
        if not et.cancelled_txn(exc):
            raise
        raise et.EventsError(409, "changed_retry")
    _publish("EventOccurrence.Cancelled", {"tenantId": ctx.tenant_id, "locationId": ev["locationId"],
                                           "eventId": ev["eventId"], "occurrenceId": occ["occurrenceId"],
                                           "ticketsToRefund": to_refund})
    try:  # a few refunds now (API Gateway allows 30 s); the sweep finishes the rest within minutes
        _run_job(ctx.tenant_id, job_id, limit=INLINE_REFUNDS, time_left=_invocation["time_left"])
    except Exception as exc:  # never fail the 202: the job is stored and resumable
        logger.warning(json.dumps({"job": job_id, "firstRun": "failed", "type": type(exc).__name__}))
    return http.respond(202, {"jobId": job_id, "toRefund": to_refund})


def get_job(event, ctx):
    _owner(ctx)
    job = et.get(et.job_key(ctx.tenant_id, _path(event, "jobId")))
    if not job or job.get("locationId") != ctx.location_id:
        raise et.EventsError(404, "not found")
    return http.respond(200, {"jobId": job["jobId"], "kind": job["kind"], "done": bool(job["done"]),
                              "counts": et.plain(job["counts"]), "toRefund": int(job["toRefund"])})


def _owed_refunds(tenant_id, occ_id):
    return [t for t in et.pending_refunds() if t.get("tenantId") == tenant_id and t.get("occurrenceId") == occ_id]


def _run_job(tenant_id, job_id, *, limit=JOB_BATCH, time_left=None):
    """One step of a mass refund: tickets are refunded in our books one by
    one (`counts.refunded` written per ticket, so K14 shows live progress),
    then the owed Stripe refunds are sent; `counts.failed` is how many
    Stripe still owes. Stops at `limit` tickets or when `time_left` (ms)
    gets short, and the next sweep continues. Done when every ticket is
    refunded, Stripe has every refund and every buyer is emailed."""
    job = et.get(et.job_key(tenant_id, job_id))
    if not job or job.get("done"):
        return job
    key = et.job_key(tenant_id, job_id)
    tickets = _paid_tickets(tenant_id, job["occurrenceId"])
    stopped = False
    for ticket in tickets[:limit]:
        if not _in_time(time_left):
            stopped = True
            break
        try:
            _refund_ticket(tenant_id, ticket, f"cancel-{ticket['ticketId']}", by="cancellation",
                           reason="Evenemanget är inställt", stripe="later")
        except (et.EventsError, ClientError, BotoCoreError) as exc:
            logger.warning(json.dumps({"job": job_id, "ticket": "refund_failed", "type": type(exc).__name__}))
            continue
        et.table().update_item(Key=key, UpdateExpression="SET counts.refunded = counts.refunded + :one",
                               ExpressionAttributeValues={":one": 1})
    for tx in _owed_refunds(tenant_id, job["occurrenceId"]):
        if not _in_time(time_left):
            stopped = True
            break
        try:
            _stripe_refund(tenant_id, tx)
        except et.EventsError:
            continue  # still owed: counted below and retried by the sweep
    remaining = len(_paid_tickets(tenant_id, job["occurrenceId"]))
    unfinished = len(_owed_refunds(tenant_id, job["occurrenceId"]))
    notified = 0
    if not remaining and not unfinished and not stopped:
        notified = _notify_cancelled(tenant_id, job)
    done = not remaining and not unfinished and not stopped and not _unnotified(tenant_id, job)
    expr = "SET counts.failed = :f, counts.notified = counts.notified + :n, done = :d"
    values = {":f": unfinished, ":n": notified, ":d": done}  # failed = Stripe refunds still owed after this run
    if done:
        expr += ", #ttl = :ttl REMOVE GSI1PK, GSI1SK"
        values[":ttl"] = int((et.now() + timedelta(days=et.JOB_TTL_DAYS)).timestamp())
    et.table().update_item(Key=key, UpdateExpression=expr, ExpressionAttributeValues=values,
                           **({"ExpressionAttributeNames": {"#ttl": "ttl"}} if done else {}))
    return et.get(key)


def _orders_of_occurrence(tenant_id, occ_id):
    event_id = occ_id.rsplit("-", 1)[0]
    return [o for o in et.orders_of_event(tenant_id, event_id) if o.get("occurrenceId") == occ_id
            and o.get("status") in ("paid", "partly_refunded", "refunded")]


def _unnotified(tenant_id, job):
    return [o for o in _orders_of_occurrence(tenant_id, job["occurrenceId"])
            if o.get("buyerEmail") and o.get("cancelNotifiedJob") != job["jobId"] and int(o["amountOre"]) >= 0]


def _notify_cancelled(tenant_id, job):
    ev = et.get(et.event_key(tenant_id, job["eventId"])) or {}
    location = tenant.get_location(tenant_id, job["locationId"]) or {}
    occ_start = et.from_compact(job["occurrenceId"][len(job["eventId"]) + 1:])
    sent = 0
    for order in _unnotified(tenant_id, job):
        # what this cancellation gave back: the order's tickets refunded by the job (a ticket the owner refunded
        # or a resale paid back earlier is not counted again)
        refunded_now = sum(int(t.get("priceOre") or 0) for t in et.tickets_of_order(tenant_id, order)
                           if t.get("status") == "refunded" and (t.get("refundTxId") or "").startswith("cancel-"))
        text = (f"Hej {order.get('buyerName') or ''}!\n\n{ev.get('title', 'Evenemanget')} på "
                f"{(location.get('name') or 'restaurangen').strip()} "
                f"{_when(et.iso(occ_start), location) if occ_start else ''} är tyvärr inställt.\n\n"
                + (f"{job['message']}\n\n" if job.get("message") else "")
                + (f"Vi har återbetalat {_kr(refunded_now)} till kortet du betalade med. Det kan ta "
                   "några bankdagar innan pengarna syns.\n" if refunded_now else ""))
        try:
            _notify(tenant_id, order["locationId"], "events-tickets.cancelled", order["buyerEmail"],
                    f"Inställt: {ev.get('title', 'evenemang')}", text)
        except NotificationUnavailable:
            continue
        et.table().update_item(Key=et.order_key(tenant_id, order["orderId"]),
                               UpdateExpression="SET cancelNotifiedJob = :j",
                               ExpressionAttributeValues={":j": job["jobId"]})
        sent += 1
    return sent


# --- refunds (K13 and the mass refund) ---------------------------------------------------------------

def _vat_lines(amount, rate):
    vat = et.vat_of(abs(amount), rate)
    sign = 1 if amount >= 0 else -1
    return [{"rate": int(rate), "netOre": sign * (abs(amount) - vat), "vatOre": sign * vat}]


def _tx(tenant_id, tx_id, kind, order, ticket_ids, amount, rates, at, **extra):
    """rates: [(amount, rate)] for the VAT lines (one per ticket type)."""
    lines = []
    for part, rate in rates:
        lines += _vat_lines(part, rate)
    tz = et.zone(tenant.get_location(tenant_id, order["locationId"]))
    day = et.local_day(at, tz)
    return {**et.tx_key(tenant_id, tx_id), "tenantId": tenant_id, "txId": tx_id, "kind": kind,
            "orderId": order["orderId"], "ticketIds": list(ticket_ids), "amountOre": int(amount), "vatLines": lines,
            "locationId": order["locationId"], "occurrenceId": order["occurrenceId"], "at": et.iso(at),
            "GSI2PK": f"TENANT#{tenant_id}#LOC#{order['locationId']}#{day}", "GSI2SK": f"{et.iso(at)}#{tx_id}",
            **extra}


def _refund_ticket(tenant_id, ticket, tx_id, *, by, reason=None, stripe="now", retry=True):
    """The ticket first (one transaction: ticket refunded, TX row, counters),
    then Stripe for exactly the ticket's price (key ticket-refund-<ticketId>).
    `stripe`: "now" calls Stripe here, "later" leaves it to the caller or the
    sweep, "external" means Stripe already refunded it. Until Stripe has the
    refund the TX carries a sparse REFUNDS index row, so a failed call is
    retried by the sweep whatever txId the client sends next. Returns
    (ticket, created)."""
    done = et.get(et.tx_key(tenant_id, tx_id))
    if done:
        if done.get("ticketIds") != [ticket["ticketId"]]:
            raise et.EventsError(409, "changed_retry")
        if stripe == "now":
            _stripe_refund(tenant_id, done)
        return et.get(et.ticket_key(tenant_id, ticket["ticketId"])), False
    if ticket["status"] not in ("valid", "checked_in"):
        raise et.EventsError(409, "invalid_state")
    order = et.get(et.order_key(tenant_id, ticket["orderId"]))
    occ_start = et.split_occurrence(ticket["eventId"], ticket["occurrenceId"])
    if not order or not occ_start:
        raise et.EventsError(409, "invalid_state")
    price, rate = int(ticket.get("priceOre") or 0), int(ticket.get("vatRate") or 12)
    at = et.now()
    refunded = int(order.get("refundedCount") or 0)
    status = "refunded" if refunded + 1 >= int(order["qty"]) else "partly_refunded"
    tx = _tx(tenant_id, tx_id, "refund", order, [ticket["ticketId"]], -price, [(-price, rate)], at, by=by,
             paymentIntentId=order.get("paymentIntentId"), stripeAccountId=order.get("stripeAccountId"),
             **({"reason": reason} if reason else {}))
    if price == 0 or stripe == "external":
        tx["stripeRefundId"] = "none" if price == 0 else "external"
    else:
        tx.update(et.refund_gsi(tenant_id, tx_id, et.iso(at)))
    type_row = et.get(et.type_key(tenant_id, ticket["eventId"], ticket["typeId"]))
    items = [
        _update(et.ticket_key(tenant_id, ticket["ticketId"]),
                "SET #s = :r, refundedAt = :at, refundTxId = :tx, version = version + :one"
                " REMOVE pendingTransferId, pendingTransferKind, resaleClaimOrderId",
                {":r": "refunded", ":at": et.iso(at), ":tx": tx_id, ":one": 1, ":v": int(ticket["version"])},
                "version = :v", {"#s": "status"}),
        _put(tx),
        _update(et.occ_key(tenant_id, ticket["eventId"], occ_start),
                "SET sold = sold - :one, taken = taken - :one, seatsLeft = seatsLeft + :one, "
                "revenueOre = revenueOre - :p",
                {":one": 1, ":p": price}),
        _update(et.order_key(tenant_id, order["orderId"]),
                "SET refundedCount = refundedCount + :one, refundedOre = refundedOre + :p, #s = :st",
                {":one": 1, ":p": price, ":st": status, ":rc": refunded}, "refundedCount = :rc", {"#s": "status"}),
    ]
    if type_row:
        items.append(_update({"PK": type_row["PK"], "SK": type_row["SK"]},
                             "SET sold = sold - :one, taken = taken - :one"
                             + (", seatsLeft = seatsLeft + :one" if "seatsLeft" in type_row else ""), {":one": 1},
                             _cap_guard(type_row)))
    if ticket.get("pendingTransferId") and stripe == "external":
        # Stripe already gave the seller the money back (dashboard refund, lost dispute): the listing ends
        # whatever its state, a buyer's `claimed` hold included. That buyer's payment then finds the ticket
        # no longer valid (`_issue_resale` -> `_resale_failed`) and goes back in full.
        items.append(_update(et.transfer_key(tenant_id, ticket["pendingTransferId"]),
                             "SET #s = :c, cancelledAt = :at REMOVE GSI2PK, GSI2SK, claimOrderId, claimedAt",
                             {":c": "cancelled", ":at": et.iso(at), ":p": "pending", ":cl": "claimed"},
                             "#s IN (:p, :cl)", {"#s": "status"}))
    elif ticket.get("pendingTransferId"):
        # A gift on its way or a resale listing ends with the ticket. A listing a buyer is paying for
        # (`claimed`) fails the condition: the refund waits until that hold is paid or expires.
        items.append(_update(et.transfer_key(tenant_id, ticket["pendingTransferId"]),
                             "SET #s = :c, cancelledAt = :at REMOVE GSI2PK, GSI2SK",
                             {":c": "cancelled", ":at": et.iso(at), ":p": "pending"}, "#s = :p", {"#s": "status"}))
    try:
        _txn(items)
    except ClientError as exc:
        if not et.cancelled_txn(exc):
            raise
        fresh = et.get(et.ticket_key(tenant_id, ticket["ticketId"]))
        if retry and fresh:  # e.g. the owner changed the type's cap meanwhile: once more from a fresh read
            return _refund_ticket(tenant_id, fresh, tx_id, by=by, reason=reason, stripe=stripe, retry=False)
        if fresh and fresh.get("pendingTransferId") and (
                et.get(et.transfer_key(tenant_id, fresh["pendingTransferId"])) or {}).get("status") == "claimed":
            raise et.EventsError(409, "invalid_state")  # a buyer is paying for the listed ticket right now
        raise et.EventsError(409, "changed_retry")
    _publish("Ticket.Refunded", {"tenantId": tenant_id, "locationId": order["locationId"],
                                 "occurrenceId": ticket["occurrenceId"], "ticketId": ticket["ticketId"],
                                 "amountOre": price, "by": by})
    if price and stripe == "now":
        _stripe_refund(tenant_id, tx)
    return et.get(et.ticket_key(tenant_id, ticket["ticketId"])), True


def _existing_stripe_refund(tenant_id, tx, ticket_id, amount):
    """A refund Stripe already holds for this ticket (metadata), or None.
    Used before retrying a TX older than REFUND_RETRY_HOURS: Stripe may
    have forgotten the idempotency key by then, so a blind retry could
    refund twice. Failed and canceled refunds moved no money and do not count."""
    listing = stripe.request("GET", "/v1/refunds", {"payment_intent": tx["paymentIntentId"], "limit": 100},
                             account=tx["stripeAccountId"])
    for refund in (listing or {}).get("data") or []:
        meta = refund.get("metadata") or {}
        if meta.get("ticketId") == ticket_id and meta.get("tenantId") == tenant_id \
                and int(refund.get("amount") or 0) == amount and refund.get("status") not in ("failed", "canceled"):
            return refund
    return None


def _stripe_refund(tenant_id, tx):
    amount = -int(tx["amountOre"])
    if amount <= 0 or tx.get("stripeRefundId"):
        return
    ticket_id = tx["ticketIds"][0]
    created = et.parse_iso(tx.get("at"))
    try:
        result = None
        if created and et.now() - created > timedelta(hours=et.REFUND_RETRY_HOURS):
            result = _existing_stripe_refund(tenant_id, tx, ticket_id, amount)
        if result is None:
            result = stripe.request("POST", "/v1/refunds", {
                "payment_intent": tx["paymentIntentId"], "amount": amount,
                "metadata": {"kind": "ticket", "tenantId": tenant_id, "orderId": tx["orderId"],
                             "ticketId": ticket_id,
                             **({"transferId": tx["transferId"]} if tx.get("transferId") else {})}},
                account=tx["stripeAccountId"],
                # a resale refund is keyed by its listing (ADR-K 8): the same ticket may be refunded
                # once by the owner and once by a resale, never twice for the same reason
                idempotency_key=tx.get("stripeKey") or f"ticket-refund-{ticket_id}")
    except (stripe.StripeError, stripe.StripeUnavailable):
        logger.warning(json.dumps({"event": "ticket.refund", "stripe": "failed", "txId": tx["txId"]}))
        raise et.EventsError(502, "stripe_unavailable")
    et.table().update_item(Key=et.tx_key(tenant_id, tx["txId"]),
                           UpdateExpression="SET stripeRefundId = :r REMOVE GSI1PK, GSI1SK",
                           ExpressionAttributeValues={":r": (result or {}).get("id") or "unknown"})


def refund_ticket(event, ctx):
    _owner(ctx)
    body = http.body(event, allowed=("txId", "reason"))
    ticket = et.get(et.ticket_key(ctx.tenant_id, _path(event, "ticketId")))
    if not ticket or ticket.get("locationId") != ctx.location_id:
        raise et.EventsError(404, "not found")
    tx_id = body.get("txId")
    if not et.valid_id(tx_id):
        raise et.EventsError(400, "txId is invalid", "txId")
    try:
        reason = et.text(body.get("reason"), 200, required=True)
    except ValueError:
        raise et.EventsError(400, "reason is required", "reason")
    ticket, created = _refund_ticket(ctx.tenant_id, ticket, tx_id, by="owner", reason=reason)
    if created and int(ticket.get("priceOre") or 0):
        order = et.get(et.order_key(ctx.tenant_id, ticket["orderId"])) or {}
        if order.get("buyerEmail"):
            try:
                _notify(ctx.tenant_id, ticket["locationId"], "events-tickets.refunded", order["buyerEmail"],
                        "Återbetald biljett",
                        f"Hej {order.get('buyerName') or ''}!\n\nEn biljett ({ticket.get('typeName', '')}) har "
                        f"återbetalats med {_kr(ticket['priceOre'])}. Biljetten gäller inte längre.\n")
            except NotificationUnavailable as exc:
                logger.warning(json.dumps({"route": "refund", "notification": str(exc)}))
    return http.respond(200, et.ticket_view(ticket))


# --- K10-K12 guest list, check-ins, orders ---------------------------------------------------------

def guest_list(event, ctx):
    ev = _event_or_404(ctx, event)
    occ = _occ_or_404(ctx, ev, event)
    tickets = sorted(et.tickets_of_occurrence(ctx.tenant_id, occ["occurrenceId"]),
                     key=lambda t: ((t.get("holderName") or "").lower(), t["ticketId"]))
    shown = [et.ticket_view(t) for t in tickets if t["status"] in ("valid", "checked_in", "refunded", "void")]
    return http.respond(200, {"scanKey": qr.scan_key(ctx.tenant_id, occ["occurrenceId"]), "tickets": shown,
                              "counters": {"sold": int(occ.get("sold") or 0),
                                           "checkedIn": int(occ.get("checkedIn") or 0),
                                           "capacity": int(occ["capacity"])}})


def _by_name(sub, tenant_id):
    """The staff member's first name for "redan insläppt 19:02 av Ali" (never an email)."""
    table_name = os.environ.get("USER_TABLE_NAME")
    if not table_name or not sub:
        return None
    try:
        item = dynamo.table(table_name).get_item(Key={"PK": f"USER#{sub}", "SK": "PROFILE"}).get("Item") or {}
    except ClientError:
        return None
    if item.get("tenantId") != tenant_id:
        return None
    name = (item.get("name") or item.get("givenName") or "").strip()
    return name.split()[0][:40] if name else None


def check_ins(event, ctx):
    body = http.body(event, allowed=("checkIns",))
    ev = _event_or_404(ctx, event)
    occ = _occ_or_404(ctx, ev, event)
    raw = body.get("checkIns")
    if not isinstance(raw, list) or not raw:
        raise et.EventsError(400, "checkIns is required", "checkIns")
    if len(raw) > 200:
        raise et.EventsError(400, "too_many", "checkIns")
    by = _by_name(ctx.sub, ctx.tenant_id)
    results = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("clientId"), str) \
                or not 1 <= len(item["clientId"]) <= 80:
            raise et.EventsError(400, "clientId is required", "checkIns")
        results.append(_check_in(ctx, occ, item, by))
    return http.respond(200, {"results": results})


def _check_in(ctx, occ, item, by):
    client_id, ticket_id = item["clientId"], item.get("ticketId")
    if not qr.verify(ctx.tenant_id, occ["occurrenceId"], ticket_id, item.get("sig")):
        return {"clientId": client_id, "status": "invalid"}
    ticket = et.get(et.ticket_key(ctx.tenant_id, ticket_id))
    if not ticket or ticket.get("occurrenceId") != occ["occurrenceId"]:
        return {"clientId": client_id, "status": "invalid"}
    if ticket["status"] == "checked_in":
        return {"clientId": client_id, "status": "already", "at": ticket.get("checkedInAt"),
                **({"by": ticket["checkedInBy"]} if ticket.get("checkedInBy") else {})}
    if ticket["status"] != "valid":
        return {"clientId": client_id, "status": "void"}
    at = et.parse_iso(item.get("at")) or et.now()
    if at > et.now() + timedelta(minutes=5):
        at = et.now()
    values = {":c": "checked_in", ":at": et.iso(at), ":cid": client_id, ":one": 1, ":valid": "valid"}
    sets = "SET #s = :c, checkedInAt = :at, checkedInClientId = :cid, version = version + :one"
    if by:
        sets += ", checkedInBy = :by"
        values[":by"] = by
    try:
        _txn([_update(et.ticket_key(ctx.tenant_id, ticket_id), sets, values, "#s = :valid", {"#s": "status"}),
              _update({"PK": occ["PK"], "SK": occ["SK"]}, "SET checkedIn = checkedIn + :one", {":one": 1})])
    except ClientError as exc:
        if not et.cancelled_txn(exc):
            raise
        again = et.get(et.ticket_key(ctx.tenant_id, ticket_id)) or {}
        if again.get("status") == "checked_in":
            return {"clientId": client_id, "status": "already", "at": again.get("checkedInAt"),
                    **({"by": again["checkedInBy"]} if again.get("checkedInBy") else {})}
        return {"clientId": client_id, "status": "void"}
    _publish("Ticket.CheckedIn", {"tenantId": ctx.tenant_id, "locationId": occ["locationId"],
                                  "occurrenceId": occ["occurrenceId"], "ticketId": ticket_id})
    return {"clientId": client_id, "status": "ok", "at": et.iso(at), **({"by": by} if by else {})}


def list_orders(event, ctx):
    _owner(ctx)
    params = http.query(event)
    if set(params) - {"cursor"}:
        raise et.EventsError(400, "unsupported query parameters")
    ev = _event_or_404(ctx, event)
    rows = et.orders_of_event(ctx.tenant_id, ev["eventId"])  # paid orders, newest first (GSI1)
    start = 0
    if params.get("cursor"):
        key = et.decode_cursor(params["cursor"], ctx.tenant_id)
        start = next((i + 1 for i, o in enumerate(rows) if o["SK"] == key["SK"]), len(rows))
    page = rows[start:start + et.PAGE]
    out = []
    for o in page:
        view = {k: et.plain(o[k]) for k in ("orderId", "occurrenceId", "qty", "amountOre", "refundedOre", "status",
                                             "buyerName", "buyerEmail", "createdAt", "paidAt") if o.get(k) is not None}
        view["resale"] = bool(o.get("resale"))
        view["tickets"] = [et.ticket_view(t) for t in et.tickets_of_order(ctx.tenant_id, o)]
        out.append(view)
    cursor = et.encode_cursor({"PK": page[-1]["PK"], "SK": page[-1]["SK"]}) \
        if page and start + et.PAGE < len(rows) else None
    return http.respond(200, {"orders": out, "cursor": cursor})


# --- K15-K17 public -----------------------------------------------------------------------------

def _published(ctx, event_id):
    ev = et.get_event(ctx.tenant_id, event_id, ctx.location_id)
    if not ev or ev["status"] != "published":
        raise et.EventsError(404, "not found")
    return ev


def _type_available(t, at_iso):
    tier = t.get("tier") or {}
    if tier.get("availableFrom") and at_iso < tier["availableFrom"]:
        return False
    if tier.get("availableUntil") and at_iso >= tier["availableUntil"]:
        return False
    return "seatsLeft" not in t or int(t["seatsLeft"]) > 0


def _sales_open(occ, at_iso):
    if occ["status"] != "scheduled":
        return False
    if occ.get("salesOpenAt") and at_iso < occ["salesOpenAt"]:
        return False
    return at_iso < (occ.get("salesCloseAt") or occ["startAt"])


def public_list(event):
    ctx = tenant.for_public(http.path_id(event, "locationId"), feature=et.FEATURE_KEY)
    at = et.iso(et.now())
    out = []
    for ev in et.events_of(ctx.tenant_id):
        if ev.get("locationId") != ctx.location_id or ev["status"] != "published":
            continue
        occs, types = et.event_parts(ctx.tenant_id, ev["eventId"])
        upcoming = [o for o in occs if o["status"] == "scheduled" and o["startAt"] > at]
        if not upcoming or not types:
            continue
        out.append({"eventId": ev["eventId"], "title": ev["title"], **media.image_view(ev),
                    "nextOccurrence": upcoming[0]["startAt"],
                    "fromPriceSek": min(int(t["priceOre"]) for t in types) / 100,
                    "soldOut": all(et.left(o) == 0 for o in upcoming)})
    out.sort(key=lambda e: e["nextOccurrence"])
    return http.respond(200, {"events": out})


def public_detail(event):
    ctx = tenant.for_public(http.path_id(event, "locationId"), feature=et.FEATURE_KEY)
    ev = _published(ctx, _path(event, "eventId"))
    occs, types = et.event_parts(ctx.tenant_id, ev["eventId"])
    at = et.iso(et.now())
    upcoming = [o for o in occs if o["status"] == "scheduled" and o["startAt"] > at]
    settings = ev.get("settings") or {}
    resale = []
    # tickets other guests have put up for sale at face value (ADR-K 8): one query for the event
    offering = [o for o in upcoming if _resale_open(ev, o) and _sales_open(o, at)]
    listed = _listings(ctx.tenant_id, ev["eventId"], [o["occurrenceId"] for o in offering])
    for o in offering:
        resale += [_listing_view(t) for t in listed[o["occurrenceId"]] if t["status"] == "pending"]
    return http.respond(200, {
        "eventId": ev["eventId"], "title": ev["title"], "description": ev.get("description") or "",
        **media.image_view(ev),
        "refundPolicy": settings.get("refundPolicy"), "perOrderMax": int(settings.get("perOrderMax") or 10),
        "occurrences": [{"occurrenceId": o["occurrenceId"], "startAt": o["startAt"], "endAt": o["endAt"],
                         "left": et.left(o), "soldOut": et.left(o) == 0, "salesOpen": _sales_open(o, at)}
                        for o in upcoming],
        "ticketTypes": [{"typeId": t["typeId"], "name": t["name"], "priceSek": int(t["priceOre"]) / 100,
                         "priceOre": int(t["priceOre"]), "perOrderMax": int(t["perOrderMax"]),
                         **({"left": max(0, int(t["seatsLeft"]))} if "seatsLeft" in t else {}),
                         **({"includes": t["includes"]} if t.get("includes") else {}),
                         "available": _type_available(t, at)} for t in types],
        "resaleAllowed": bool(settings.get("resaleAllowed")), "resale": resale,
    })


def _listings(tenant_id, event_id, occ_ids):
    """The event's live resale listings (`pending` or `claimed`) whose ticket
    is still valid and still points at the listing, per occurrence (only the
    `occ_ids` asked for): a ticket the owner refunded or the door let in
    meanwhile is never offered. One GSI query for the whole event, then a key
    lookup per listing."""
    out = {occ_id: [] for occ_id in occ_ids}
    for transfer in et.resales_of_event(tenant_id, event_id) if out else []:
        if transfer.get("occurrenceId") not in out or transfer.get("kind") != "resale" \
                or transfer.get("status") not in ("pending", "claimed"):
            continue
        ticket = et.get(et.ticket_key(tenant_id, transfer["ticketId"])) or {}
        if ticket.get("status") == "valid" and ticket.get("pendingTransferId") == transfer["transferId"]:
            out[transfer["occurrenceId"]].append(transfer)
    return out


def _listing_view(t):
    return {"transferId": t["transferId"], "occurrenceId": t["occurrenceId"], "typeId": t["typeId"],
            "typeName": t.get("typeName") or "", "priceOre": int(t["priceOre"]), "priceSek": int(t["priceOre"]) / 100}


def _guard(event):
    """Checkout attempts per IP; request_guard.Limited becomes a 429 with Retry-After in the handler."""
    return request_guard.check([("ticket-ip", http.source_ip(event), *CHECKOUT_IP_LIMIT)], et.now())


def _checkout_lines(body, types, settings, at_iso):
    raw = body.get("lines")
    if not isinstance(raw, list) or not raw or len(raw) > et.MAX_TYPES:
        raise et.EventsError(400, "lines is required", "lines")
    by_id = {t["typeId"]: t for t in types}
    lines, total = [], 0
    for line in raw:
        if not isinstance(line, dict) or set(line) - {"typeId", "qty"}:
            raise et.EventsError(400, "lines is invalid", "lines")
        t = by_id.get(line.get("typeId"))
        if not t:
            raise et.EventsError(404, "not found")
        qty = et.whole(line.get("qty"), 1, et.MAX_PER_ORDER)
        if qty is None or qty > int(t["perOrderMax"]):
            raise et.EventsError(400, "too_many_tickets", "lines")
        if not _type_available(t, at_iso):
            raise et.EventsError(409, "type_sold_out")
        lines.append({"typeId": t["typeId"], "qty": qty, "priceOre": int(t["priceOre"]), "vatRate": int(t["vatRate"]),
                      "name": t["name"]})
        total += qty
    if len({ln["typeId"] for ln in lines}) != len(lines):
        raise et.EventsError(400, "lines is invalid", "lines")
    if total > int(settings.get("perOrderMax") or 10):
        raise et.EventsError(400, "too_many_tickets", "lines")
    return lines, total


def _hold_items(tenant_id, occ, types_by_id, lines, qty):
    """The hold is conditional on the seats (and the price at checkout),
    not on the type's version: the owner renaming a type must not turn a
    checkout into type_sold_out."""
    items = [_update({"PK": occ["PK"], "SK": occ["SK"]},
                     "SET held = held + :q, taken = taken + :q, seatsLeft = seatsLeft - :q",
                     {":q": qty, ":sched": "scheduled"}, "seatsLeft >= :q AND #s = :sched", {"#s": "status"})]
    for line in lines:
        t = types_by_id[line["typeId"]]
        capped = "seatsLeft" in t
        items.append(_update({"PK": t["PK"], "SK": t["SK"]},
                             "SET held = held + :q, taken = taken + :q"
                             + (", seatsLeft = seatsLeft - :q" if capped else ""),
                             {":q": line["qty"], ":p": line["priceOre"], ":r": line["vatRate"]},
                             "attribute_exists(PK) AND priceOre = :p AND vatRate = :r AND "
                             + ("seatsLeft >= :q" if capped else _cap_guard(t))))
    return items


def _release_items(tenant_id, order, *, held=True):
    """Undo a hold (held) or a sale (held=False) of an order's lines. A resale
    order holds no seats: its hold is the claim on each listing, given back
    (`claimed` -> `pending`) only while it is this order's."""
    if order.get("resale"):
        items = []
        for line in order["lines"]:
            # A listing Stripe's refund of the seller already cancelled (or one the hold lost long ago) has
            # nothing to give back; skipping it keeps the release of the rest from failing on its condition.
            transfer = et.get(et.transfer_key(tenant_id, line["transferId"])) or {}
            if transfer.get("status") == "claimed" and transfer.get("claimOrderId") == order["orderId"]:
                items.append(_update(et.transfer_key(tenant_id, line["transferId"]),
                                     "SET #s = :p REMOVE claimOrderId, claimedAt",
                                     {":p": "pending", ":c": "claimed", ":o": order["orderId"]},
                                     "#s = :c AND claimOrderId = :o", {"#s": "status"}))
            # the ticket's claim marker goes too (unconditional: the ticket may have been refunded meanwhile)
            items.append(_update(et.ticket_key(tenant_id, line["ticketId"]), "REMOVE resaleClaimOrderId", {}))
        return items
    field = "held" if held else "sold"
    occ_start = et.split_occurrence(order["eventId"], order["occurrenceId"])
    items = [_update(et.occ_key(tenant_id, order["eventId"], occ_start),
                     f"SET {field} = {field} - :q, taken = taken - :q, seatsLeft = seatsLeft + :q",
                     {":q": int(order["qty"])})]
    for line in order["lines"]:
        t = et.get(et.type_key(tenant_id, order["eventId"], line["typeId"]))
        if t:
            items.append(_update({"PK": t["PK"], "SK": t["SK"]},
                                 f"SET {field} = {field} - :q, taken = taken - :q"
                                 + (", seatsLeft = seatsLeft + :q" if "seatsLeft" in t else ""),
                                 {":q": int(line["qty"])}, _cap_guard(t)))
    return items


def checkout(event):
    location_id = http.path_id(event, "locationId")
    ctx = tenant.for_public(location_id, feature=et.FEATURE_KEY)
    body = http.body(event, allowed=("orderId", "occurrenceId", "lines", "resaleTransferIds", "buyerName",
                                     "buyerEmail", "buyerPhone", "tableIds", "turnstileToken"))
    order_id = body.get("orderId")
    if not et.valid_id(order_id):
        raise et.EventsError(400, "orderId is invalid", "orderId")
    if body.get("tableIds"):
        raise et.EventsError(400, "table_pick_not_available", "tableIds")
    try:
        buyer_name = et.text(body.get("buyerName"), et.NAME_MAX, required=True)
    except ValueError:
        raise et.EventsError(400, "buyerName is required", "buyerName")
    if not et.valid_email(body.get("buyerEmail")):
        raise et.EventsError(400, "invalid_email", "buyerEmail")
    phone = body.get("buyerPhone")
    if phone not in (None, "") and (not isinstance(phone, str) or not 6 <= len(phone.strip()) <= 20):
        raise et.EventsError(400, "buyerPhone is invalid", "buyerPhone")
    existing = et.get(et.order_key(ctx.tenant_id, order_id))
    if existing:
        return _checkout_retry(ctx, existing, body)
    occ_id = body.get("occurrenceId")
    event_id = occ_id.split("-", 1)[0] if isinstance(occ_id, str) and "-" in occ_id else None
    ev = et.get_event(ctx.tenant_id, event_id, location_id) if event_id else None
    start = et.split_occurrence(event_id, occ_id) if ev else None
    occ = et.get(et.occ_key(ctx.tenant_id, event_id, start)) if start else None
    if not ev or ev["status"] != "published" or not occ:
        raise et.EventsError(404, "not found")
    _, types = et.event_parts(ctx.tenant_id, event_id)
    at = et.now()
    resale_ids = body.get("resaleTransferIds")
    if resale_ids not in (None, []):
        if body.get("lines"):
            raise et.EventsError(400, "lines is invalid", "lines")  # a resale order holds no new seats
        lines, qty, hold, claimed = _resale_lines(ctx, ev, occ, resale_ids, at, order_id,
                                                  et.email_hash(ctx.tenant_id, body["buyerEmail"]))
    else:
        lines, qty = _checkout_lines(body, types, ev.get("settings") or {}, et.iso(at))
        hold, claimed = None, None
    if not _sales_open(occ, et.iso(at)):
        raise et.EventsError(409, "sales_closed")
    if claimed is None and et.left(occ) < qty:
        raise et.EventsError(409, "sold_out")
    _guard(event)
    if turnstile.required():
        try:
            turnstile.verify(body.get("turnstileToken"), http.source_ip(event))
        except ValueError:
            raise et.EventsError(400, "turnstile_failed", "turnstileToken")
    amount = sum(ln["qty"] * ln["priceOre"] for ln in lines)
    account = orders.stripe_account(ctx.tenant, ctx.location) if amount else None
    if amount and not account:
        raise et.EventsError(409, "no_stripe_account")
    expires = at + timedelta(minutes=et.HOLD_MINUTES)
    buyer = body["buyerEmail"].strip().lower()
    order = {**et.order_key(ctx.tenant_id, order_id), "tenantId": ctx.tenant_id, "orderId": order_id,
             "eventId": event_id, "occurrenceId": occ_id, "locationId": location_id,
             "lines": [{k: ln[k] for k in ("typeId", "qty", "priceOre", "vatRate", "name", "transferId", "ticketId")
                        if k in ln} for ln in lines],
             "qty": qty, "amountOre": amount, "refundedOre": 0, "refundedCount": 0, "buyerName": buyer_name,
             "buyerEmail": buyer, "buyerEmailHash": et.email_hash(ctx.tenant_id, buyer), "status": "pending",
             "createdAt": et.iso(at), "expiresAt": et.iso(expires),
             "ttl": int((expires + timedelta(days=1)).timestamp()),
             "GSI1PK": "HOLD", "GSI1SK": f"{et.iso(expires)}#{ctx.tenant_id}#{order_id}"}
    if claimed is not None:
        order.update({"resale": True, "resaleTransferIds": claimed})
    if account:
        order["stripeAccountId"] = account
    if phone:
        order["buyerPhone"] = phone.strip()
    types_by_id = {t["typeId"]: t for t in types}
    try:
        _txn([*(hold if hold is not None else _hold_items(ctx.tenant_id, occ, types_by_id, lines, qty)), _put(order)])
    except ClientError as exc:
        if not et.cancelled_txn(exc):
            raise
        again = et.get(et.order_key(ctx.tenant_id, order_id))
        if again:
            return _checkout_retry(ctx, again, body)
        if claimed is not None:
            raise et.EventsError(409, "resale_taken")  # another buyer claimed a listing, or it was withdrawn
        occ = et.get({"PK": occ["PK"], "SK": occ["SK"]}) or occ
        if et.left(occ) < qty:
            raise et.EventsError(409, "sold_out")
        _, fresh = et.event_parts(ctx.tenant_id, event_id)
        fresh_by_id = {t["typeId"]: t for t in fresh}
        for ln in lines:
            t = fresh_by_id.get(ln["typeId"])
            if not t or int(t["priceOre"]) != ln["priceOre"] or int(t["vatRate"]) != ln["vatRate"] \
                    or ("seatsLeft" in t) != ("seatsLeft" in types_by_id[ln["typeId"]]):
                raise et.EventsError(409, "changed_retry")  # the price or the cap changed under the guest: reload
        raise et.EventsError(409, "type_sold_out")
    if not amount:
        _issue(ctx.tenant_id, order, None)
        return http.respond(200, {"amountSek": 0, "expiresAt": order["expiresAt"]})
    try:
        intent = stripe.request("POST", "/v1/payment_intents", {
            "amount": amount, "currency": "sek", "automatic_payment_methods": {"enabled": "true"},
            "description": f"Biljetter {ev['title'][:60]}", "receipt_email": buyer,
            "metadata": {"kind": "ticket", "tenantId": ctx.tenant_id, "locationId": location_id, "orderId": order_id},
        }, account=account, idempotency_key=f"ticket-{order_id}")
    except (stripe.StripeError, stripe.StripeUnavailable) as exc:
        logger.warning(json.dumps({"ticket": "stripe_failed", "type": type(exc).__name__}))
        _expire_order(ctx.tenant_id, order)
        raise et.EventsError(502, "stripe_unavailable")
    et.table().update_item(Key=et.order_key(ctx.tenant_id, order_id), UpdateExpression="SET paymentIntentId = :p",
                           ExpressionAttributeValues={":p": intent["id"]})
    return http.respond(200, {"clientSecret": intent["client_secret"], "amountSek": amount / 100,
                              "expiresAt": order["expiresAt"], "stripeAccount": account})


def _resale_lines(ctx, ev, occ, raw_ids, at, order_id, buyer_hash):
    """A buyer's pick of listed tickets (ADR-K 8): one line per listing at the
    listed ticket's own price and VAT rate (never the type's current price).
    Returns (lines, qty, hold items, transferIds). The hold claims each
    listing (`pending` -> `claimed` by this order) and checks that its ticket
    is still valid and still listed, so two buyers can never pay for the same
    ticket and a ticket the owner refunded meanwhile is never sold. At most
    RESALE_PER_ORDER listings: the issue is one DynamoDB transaction (25
    items) and each listing takes four of them."""
    settings = ev.get("settings") or {}
    per_order = min(int(settings.get("perOrderMax") or 10), RESALE_PER_ORDER)
    if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= per_order or len(set(raw_ids)) != len(raw_ids) \
            or not all(et.valid_id(i) for i in raw_ids):
        raise et.EventsError(400, "too_many_tickets", "resaleTransferIds")
    if not _resale_open(ev, occ):
        raise et.EventsError(409, "sales_closed")  # resale off, or within two hours of the start
    lines, hold = [], []
    for transfer_id in raw_ids:
        transfer = et.get(et.transfer_key(ctx.tenant_id, transfer_id))
        if not transfer or transfer.get("kind") != "resale" or transfer.get("occurrenceId") != occ["occurrenceId"] \
                or transfer.get("locationId") != ctx.location_id:
            raise et.EventsError(404, "not found")
        ticket = et.get(et.ticket_key(ctx.tenant_id, transfer["ticketId"])) or {}
        if transfer["status"] != "pending" or ticket.get("status") != "valid" \
                or ticket.get("pendingTransferId") != transfer_id:
            raise et.EventsError(409, "resale_taken")
        seller = et.get(et.order_key(ctx.tenant_id, ticket["orderId"])) or {}
        # the seller is the order's buyer or, for a gifted-on ticket, its holder: withdraw it on the ticket page instead
        if buyer_hash in (seller.get("buyerEmailHash"), ticket.get("holderEmailHash")):
            raise et.EventsError(400, "own_listing", "resaleTransferIds")
        lines.append({"typeId": transfer["typeId"], "qty": 1, "priceOre": int(ticket["priceOre"]),
                      "vatRate": int(ticket["vatRate"]), "name": ticket.get("typeName") or transfer.get("typeName") or "",
                      "transferId": transfer_id, "ticketId": transfer["ticketId"]})
        hold.append(_update(et.transfer_key(ctx.tenant_id, transfer_id),
                            "SET #s = :c, claimOrderId = :o, claimedAt = :at",
                            {":c": "claimed", ":o": order_id, ":at": et.iso(at), ":p": "pending"}, "#s = :p",
                            {"#s": "status"}))
        # The ticket itself is claimed too (an Update, not a ConditionCheck: the role has no
        # dynamodb:ConditionCheckItem), conditional on it still being valid and still listed.
        hold.append(_update(et.ticket_key(ctx.tenant_id, transfer["ticketId"]), "SET resaleClaimOrderId = :o",
                            {":o": order_id, ":valid": "valid", ":t": transfer_id},
                            "#s = :valid AND pendingTransferId = :t", {"#s": "status"}))
    return lines, len(lines), hold, list(raw_ids)


def _checkout_retry(ctx, order, body):
    if order.get("resale"):
        same = (order.get("occurrenceId") == body.get("occurrenceId") and not body.get("lines")
                and sorted(order["resaleTransferIds"]) == sorted(body.get("resaleTransferIds") or [])
                and order.get("locationId") == ctx.location_id)
    else:
        same = (order.get("occurrenceId") == body.get("occurrenceId") and not body.get("resaleTransferIds")
                and sorted((ln["typeId"], int(ln["qty"])) for ln in order["lines"])
                == sorted((ln.get("typeId"), ln.get("qty")) for ln in (body.get("lines") or []) if isinstance(ln, dict))
                and order.get("locationId") == ctx.location_id)
    if not same:
        raise et.EventsError(409, "changed_retry")
    if order["status"] == "expired":
        raise et.EventsError(409, "sales_closed")
    out = {"amountSek": int(order["amountOre"]) / 100, "expiresAt": order["expiresAt"]}
    if order["status"] == "pending" and order.get("paymentIntentId"):
        try:
            intent = stripe.request("GET", f"/v1/payment_intents/{order['paymentIntentId']}",
                                    account=order["stripeAccountId"])
        except (stripe.StripeError, stripe.StripeUnavailable):
            raise et.EventsError(502, "stripe_unavailable")
        out.update({"clientSecret": intent["client_secret"], "stripeAccount": order["stripeAccountId"]})
    return http.respond(200, out)


def _expire_order(tenant_id, order, attempts=3):
    """Releases a pending order's hold once (condition status = pending). A
    type whose cap the owner added or removed meanwhile fails the cap guard:
    the release is rebuilt from a fresh read (the sweep retries what is left)."""
    for _ in range(attempts):
        try:
            _txn([_update(et.order_key(tenant_id, order["orderId"]), "SET #s = :e REMOVE GSI1PK, GSI1SK",
                          {":e": "expired", ":p": "pending"}, "#s = :p", {"#s": "status"}),
                  *_release_items(tenant_id, order)])
        except ClientError as exc:
            if not et.cancelled_txn(exc):
                raise
            again = et.get(et.order_key(tenant_id, order["orderId"]))
            if not again or again["status"] != "pending":
                return False
            continue
        return True
    return False


# --- issue (webhook or free order) ----------------------------------------------------------------

def _issue(tenant_id, order, intent, retry=True):
    """Tickets, codes, TX and counters in one transaction; a pending order
    moves its hold to sold, an order whose hold already expired takes the
    seats again only if they are still free (else the payment is refunded)."""
    if order["status"] in ("paid", "partly_refunded", "refunded"):
        _resend_unsent(tenant_id, order)
        return "already_issued"
    if order.get("lateRefundAt"):
        return "ignored:late_refunded"  # the payment already went back in full: a replay issues nothing
    late = order["status"] == "expired"
    at = et.now()
    occ_start = et.split_occurrence(order["eventId"], order["occurrenceId"])
    ev = et.get(et.event_key(tenant_id, order["eventId"])) or {}
    occ = et.get(et.occ_key(tenant_id, order["eventId"], occ_start)) or {}
    if occ.get("status") != "scheduled":
        # Paid for an occurrence that was cancelled meanwhile: no tickets, the payment goes back.
        if not late:
            _expire_order(tenant_id, order)
        return _refund_late(tenant_id, order, intent, "cancelled") if intent else "ignored:cancelled"
    if order.get("resale"):
        return _issue_resale(tenant_id, order, intent, ev, occ, late, at, retry=retry)
    tickets, n = [], 0
    for line in order["lines"]:
        for _ in range(int(line["qty"])):
            ticket_id = et.ticket_id_for(order["orderId"], n)
            n += 1
            tickets.append({**et.ticket_key(tenant_id, ticket_id), "tenantId": tenant_id, "ticketId": ticket_id,
                            "orderId": order["orderId"], "eventId": order["eventId"],
                            "occurrenceId": order["occurrenceId"], "locationId": order["locationId"],
                            "typeId": line["typeId"], "typeName": line["name"], "priceOre": int(line["priceOre"]),
                            "vatRate": int(line["vatRate"]), "status": "valid",
                            "holderName": order.get("buyerName"), "holderEmailHash": order.get("buyerEmailHash"),
                            "version": 1, "issuedAt": et.iso(at),
                            **et.occurrence_gsi(tenant_id, order["occurrenceId"], order.get("buyerName"), ticket_id)})
    amount = int(order["amountOre"])
    tx = _tx(tenant_id, f"sale-{order['orderId']}", "sale", order, [t["ticketId"] for t in tickets], amount,
             [(int(ln["qty"]) * int(ln["priceOre"]), int(ln["vatRate"])) for ln in order["lines"]], at,
             **({"paymentIntentId": intent["id"]} if intent else {}))
    cleanup_month = et.add_months(et.parse_iso(occ.get("startAt") or et.iso(at)), et.CONTACT_MONTHS).strftime("%Y-%m")
    paid_gsi = et.paid_order_gsi(tenant_id, order["eventId"], et.iso(at), order["orderId"])
    order_update = _update(
        et.order_key(tenant_id, order["orderId"]),
        "SET #s = :paid, paidAt = :at, GSI1PK = :g1, GSI1SK = :g1s, GSI2PK = :g2, GSI2SK = :g2s REMOVE #ttl",
        {":paid": "paid", ":at": et.iso(at), ":g1": paid_gsi["GSI1PK"], ":g1s": paid_gsi["GSI1SK"],
         ":g2": f"CLEANUP#{cleanup_month}", ":g2s": f"{tenant_id}#{order['orderId']}", ":st": order["status"]},
        "#s = :st", {"#s": "status", "#ttl": "ttl"})
    q = int(order["qty"])
    if late:
        counters = [_update({"PK": occ["PK"], "SK": occ["SK"]},
                            "SET sold = sold + :q, taken = taken + :q, seatsLeft = seatsLeft - :q, "
                            "revenueOre = revenueOre + :a",
                            {":q": q, ":a": amount}, "seatsLeft >= :q")]
        for line in order["lines"]:
            t = et.get(et.type_key(tenant_id, order["eventId"], line["typeId"]))
            if t:
                capped = "seatsLeft" in t
                counters.append(_update({"PK": t["PK"], "SK": t["SK"]},
                                        "SET sold = sold + :q, taken = taken + :q"
                                        + (", seatsLeft = seatsLeft - :q" if capped else ""),
                                        {":q": int(line["qty"])}, "seatsLeft >= :q" if capped else _cap_guard(t)))
    else:
        counters = [_update({"PK": occ["PK"], "SK": occ["SK"]},
                            "SET held = held - :q, sold = sold + :q, revenueOre = revenueOre + :a",
                            {":q": q, ":a": amount})]
        for line in order["lines"]:
            t = et.get(et.type_key(tenant_id, order["eventId"], line["typeId"]))
            if t:
                counters.append(_update({"PK": t["PK"], "SK": t["SK"]}, "SET held = held - :q, sold = sold + :q",
                                        {":q": int(line["qty"])}))
    try:
        _txn([order_update, *counters, _put(tx), *[_put(t) for t in tickets]])
    except ClientError as exc:
        if not et.cancelled_txn(exc):
            raise
        again = et.get(et.order_key(tenant_id, order["orderId"])) or {}
        if again.get("status") in ("paid", "partly_refunded", "refunded"):
            return "already_issued"
        if late and retry:
            return _issue(tenant_id, again or order, intent, retry=False)  # a cap changed under us: read again
        if late and intent:
            return _refund_late(tenant_id, order, intent)
        raise
    paid = et.get(et.order_key(tenant_id, order["orderId"]))
    _publish("Ticket.Sold", {"tenantId": tenant_id, "locationId": order["locationId"], "eventId": order["eventId"],
                             "occurrenceId": order["occurrenceId"], "orderId": order["orderId"], "qty": q,
                             "amountOre": amount})
    _send_tickets(tenant_id, paid, ev, occ, tenant.get_location(tenant_id, order["locationId"]) or {})
    return "issued"


def _issue_resale(tenant_id, order, intent, ev, occ, late, at, retry=True):
    """The buyer paid for listed tickets: in ONE transaction every listed
    ticket is voided (its QR is refused at the door from now on), a new ticket
    with the same type, price and VAT rate is issued to the buyer, each
    listing is `sold`, the buyer's order is paid, a `resale_sale` TX books the
    payment and one `resale_refund` TX per old ticket books the seller's
    money back (Stripe refund on the seller's PaymentIntent, key
    `ticket-resale-<transferId>`, sent after the transaction and retried by
    the sweep until Stripe has it). Capacity and revenue do not move: one
    ticket out, one in. If any listed ticket can no longer be sold (the
    seller was let in, the owner refunded it, or after an expired hold
    another buyer took it) the whole payment goes back; nothing is issued."""
    transfers, old = [], []
    for transfer_id in order["resaleTransferIds"]:
        transfer = et.get(et.transfer_key(tenant_id, transfer_id)) or {}
        ticket = et.get(et.ticket_key(tenant_id, transfer.get("ticketId") or "-")) or {}
        wanted = "pending" if late else "claimed"
        if transfer.get("status") != wanted or (not late and transfer.get("claimOrderId") != order["orderId"]) \
                or ticket.get("status") != "valid" or ticket.get("pendingTransferId") != transfer_id:
            return _resale_failed(tenant_id, order, intent, late)
        transfers.append(transfer)
        old.append(ticket)
    new_tickets, items, refunds, per_seller = [], [], [], {}
    sale_lines = []
    for n, (transfer, ticket) in enumerate(zip(transfers, old)):
        new_id = et.ticket_id_for(order["orderId"], n)
        price, rate = int(ticket["priceOre"]), int(ticket["vatRate"])
        new = {**et.ticket_key(tenant_id, new_id), "tenantId": tenant_id, "ticketId": new_id,
               "orderId": order["orderId"], "eventId": ticket["eventId"], "occurrenceId": ticket["occurrenceId"],
               "locationId": ticket["locationId"], "typeId": ticket["typeId"], "typeName": ticket.get("typeName") or "",
               "priceOre": price, "vatRate": rate, "status": "valid", "holderName": order.get("buyerName"),
               "holderEmailHash": order.get("buyerEmailHash"), "resaleOf": ticket["ticketId"],
               "resaleTransferId": transfer["transferId"], "version": 1, "issuedAt": et.iso(at),
               **et.occurrence_gsi(tenant_id, ticket["occurrenceId"], order.get("buyerName"), new_id)}
        new_tickets.append(new)
        sale_lines.append((price, rate))
        items.append(_put(new))
        items.append(_update(et.ticket_key(tenant_id, ticket["ticketId"]),
                             "SET #s = :void, resoldTo = :n, resoldAt = :at, resaleTxId = :tx, version = version + :one"
                             " REMOVE pendingTransferId, pendingTransferKind, resaleClaimOrderId",
                             {":void": "void", ":n": new_id, ":at": et.iso(at), ":one": 1,
                              ":tx": f"resale-refund-{transfer['transferId']}",
                              ":v": int(ticket["version"]), ":valid": "valid", ":t": transfer["transferId"]},
                             "version = :v AND #s = :valid AND pendingTransferId = :t", {"#s": "status"}))
        condition = "#s = :p" if late else "#s = :c AND claimOrderId = :o"
        items.append(_update(et.transfer_key(tenant_id, transfer["transferId"]),
                             "SET #s = :sold, newTicketId = :n, buyerOrderId = :o, soldAt = :at"
                             " REMOVE GSI2PK, GSI2SK, claimOrderId, claimedAt",
                             {":sold": "sold", ":n": new_id, ":o": order["orderId"], ":at": et.iso(at),
                              ":p": "pending", ":c": "claimed"}, condition, {"#s": "status"}))
        seller = et.get(et.order_key(tenant_id, ticket["orderId"])) or {}
        if not seller:
            return _resale_failed(tenant_id, order, intent, late)
        # the seller's money back: a refund TX on the seller's order, Stripe refund owed (REFUNDS row)
        refund = _tx(tenant_id, f"resale-refund-{transfer['transferId']}", "resale_refund", seller,
                     [ticket["ticketId"]], -price, [(-price, rate)], at, by="resale",
                     transferId=transfer["transferId"], buyerOrderId=order["orderId"],
                     paymentIntentId=seller.get("paymentIntentId"), stripeAccountId=seller.get("stripeAccountId"),
                     stripeKey=f"ticket-resale-{transfer['transferId']}")
        refund.update(et.refund_gsi(tenant_id, refund["txId"], et.iso(at)))
        refunds.append(refund)
        items.append(_put(refund))
        group = per_seller.setdefault(seller["orderId"], {"order": seller, "count": 0, "ore": 0})
        group["count"] += 1
        group["ore"] += price
    for group in per_seller.values():  # one update per seller order, however many of its tickets sold
        seller = group["order"]
        refunded = int(seller.get("refundedCount") or 0)
        status = "refunded" if refunded + group["count"] >= int(seller["qty"]) else "partly_refunded"
        items.append(_update(et.order_key(tenant_id, seller["orderId"]),
                             "SET refundedCount = refundedCount + :n, refundedOre = refundedOre + :p, #s = :st",
                             {":n": group["count"], ":p": group["ore"], ":st": status, ":rc": refunded},
                             "refundedCount = :rc", {"#s": "status"}))
    amount = int(order["amountOre"])
    sale = _tx(tenant_id, f"sale-{order['orderId']}", "resale_sale", order, [t["ticketId"] for t in new_tickets],
               amount, sale_lines, at, resaleOf=[t["ticketId"] for t in old],
               **({"paymentIntentId": intent["id"]} if intent else {}))
    cleanup_month = et.add_months(et.parse_iso(occ.get("startAt") or et.iso(at)), et.CONTACT_MONTHS).strftime("%Y-%m")
    paid_gsi = et.paid_order_gsi(tenant_id, order["eventId"], et.iso(at), order["orderId"])
    items.append(_update(
        et.order_key(tenant_id, order["orderId"]),
        "SET #s = :paid, paidAt = :at, GSI1PK = :g1, GSI1SK = :g1s, GSI2PK = :g2, GSI2SK = :g2s REMOVE #ttl",
        {":paid": "paid", ":at": et.iso(at), ":g1": paid_gsi["GSI1PK"], ":g1s": paid_gsi["GSI1SK"],
         ":g2": f"CLEANUP#{cleanup_month}", ":g2s": f"{tenant_id}#{order['orderId']}", ":st": order["status"]},
        "#s = :st", {"#s": "status", "#ttl": "ttl"}))
    items.append(_put(sale))
    try:
        _txn(items)
    except ClientError as exc:
        if not et.cancelled_txn(exc):
            raise
        again = et.get(et.order_key(tenant_id, order["orderId"])) or {}
        if again.get("status") in ("paid", "partly_refunded", "refunded"):
            return "already_issued"
        if retry and again:  # a seller order or ticket version moved under us: once more from a fresh read
            return _issue(tenant_id, again, intent, retry=False)
        return _resale_failed(tenant_id, order, intent, late)
    paid = et.get(et.order_key(tenant_id, order["orderId"]))
    for ticket, new in zip(old, new_tickets):
        _publish("Ticket.Transferred", {"tenantId": tenant_id, "locationId": ticket["locationId"],
                                        "occurrenceId": ticket["occurrenceId"], "ticketId": ticket["ticketId"],
                                        "newTicketId": new["ticketId"], "kind": "resale"})
    location = tenant.get_location(tenant_id, order["locationId"]) or {}
    _send_tickets(tenant_id, paid, ev, occ, location)
    for group in per_seller.values():
        _notify_seller(tenant_id, group["order"], group["count"], group["ore"], ev)
    for refund in refunds:  # the seller's Stripe refund now; a failure is retried by the sweep (REFUNDS row)
        try:
            _stripe_refund(tenant_id, refund)
        except et.EventsError:
            pass
    return "issued"


def _resale_failed(tenant_id, order, intent, late):
    """A listed ticket can no longer be sold: the claims go back (a pending
    order expires), the buyer's payment is refunded in full."""
    if not late:
        _expire_order(tenant_id, order)
    return _refund_late(tenant_id, order, intent, "resale_taken") if intent else "ignored:resale_taken"


def _notify_seller(tenant_id, seller, count, ore, ev):
    if not seller.get("buyerEmail"):
        return
    try:
        _notify(tenant_id, seller["locationId"], "events-tickets.refunded", seller["buyerEmail"],
                "Din biljett är såld",
                f"Hej {seller.get('buyerName') or ''}!\n\n"
                f"{'Din biljett' if count == 1 else f'{count} av dina biljetter'} till {ev.get('title', 'evenemanget')} "
                f"är såld vidare. {_kr(ore)} återbetalas till kortet du betalade med; det kan ta några bankdagar "
                "innan pengarna syns. Den sålda biljetten gäller inte längre.\n")
    except NotificationUnavailable as exc:
        logger.warning(json.dumps({"route": "resale", "notification": str(exc)}))


def _refund_late(tenant_id, order, intent, reason="sold_out"):
    """Paid after the hold expired and the seats are gone: the whole payment back."""
    try:
        stripe.request("POST", "/v1/refunds", {"payment_intent": intent["id"],
                                               "metadata": {"kind": "ticket", "tenantId": tenant_id,
                                                            "orderId": order["orderId"]}},
                       account=order["stripeAccountId"], idempotency_key=f"ticket-late-{order['orderId']}")
    except (stripe.StripeError, stripe.StripeUnavailable) as exc:
        raise RuntimeError("late payment refund failed") from exc
    et.table().update_item(Key=et.order_key(tenant_id, order["orderId"]), UpdateExpression="SET lateRefundAt = :at",
                           ExpressionAttributeValues={":at": et.iso(et.now())})
    return f"refunded:{reason}"


def _resend_unsent(tenant_id, order):
    if order.get("ticketsSentAt"):
        return
    paid = et.parse_iso(order.get("paidAt"))
    if paid and et.now() < paid + timedelta(seconds=TICKETS_SEND_GRACE_SECONDS):
        return
    occ_start = et.split_occurrence(order["eventId"], order["occurrenceId"])
    _send_tickets(tenant_id, order, et.get(et.event_key(tenant_id, order["eventId"])) or {},
                  et.get(et.occ_key(tenant_id, order["eventId"], occ_start)) or {},
                  tenant.get_location(tenant_id, order["locationId"]) or {})


# --- K18-K22 the ticket page and transfers ---------------------------------------------------------------

def _token_scope(event, kinds=("o", "t")):
    """(tenant_id, tickets in scope, order, kind, id) for a genuine token whose tenant has `events`."""
    parsed = qr.read_token(_path(event, "token"))
    if not parsed or parsed[0] not in kinds:
        raise et.EventsError(404, "not found")
    kind, tenant_id, item_id = parsed
    tenant_row = tenant.get_tenant(tenant_id)
    if not tenant_row or tenant_row.get("status") != "active" or not (
            (tenant_row.get("entitlements") or {}).get("features") or {}).get(et.FEATURE_KEY):
        raise et.EventsError(404, "not found")
    if kind == "o":
        order = et.get(et.order_key(tenant_id, item_id))
        if not order or order["status"] not in ("paid", "partly_refunded", "refunded"):
            raise et.EventsError(404, "not found")
        tickets = et.tickets_of_order(tenant_id, order, gifted=False)  # key lookups by deterministic id
    else:
        ticket = et.get(et.ticket_key(tenant_id, item_id))
        if not ticket:
            raise et.EventsError(404, "not found")
        order = et.get(et.order_key(tenant_id, ticket["orderId"])) or {}
        tickets = [ticket]
    return tenant_id, tickets, order, kind, item_id


def _window_open(ev, occ, flag):
    settings = ev.get("settings") or {}
    start = et.parse_iso(occ.get("startAt"))
    return bool(settings.get(flag)) and occ.get("status") == "scheduled" and bool(start) \
        and et.now() < start - timedelta(hours=et.TRANSFER_CLOSE_HOURS)


def _transfer_open(ev, occ):
    return _window_open(ev, occ, "transferAllowed")


def _resale_open(ev, occ):
    """Listing and buying listed tickets: the event allows resale and the
    start is more than two hours away (ADR-K 8)."""
    return _window_open(ev, occ, "resaleAllowed")


def ticket_page(event):
    tenant_id, tickets, order, _, _ = _token_scope(event)
    first = tickets[0] if tickets else None
    if not first:
        raise et.EventsError(404, "not found")
    ev = et.get(et.event_key(tenant_id, first["eventId"])) or {}
    start = et.split_occurrence(first["eventId"], first["occurrenceId"])
    occ = et.get(et.occ_key(tenant_id, first["eventId"], start)) or {}
    location = tenant.get_location(tenant_id, first["locationId"]) or {}
    out = []
    for t in tickets:
        view = et.ticket_view(t)
        view["priceOre"] = int(t.get("priceOre") or 0)
        if t["status"] == "valid":
            view["qr"] = qr.payload(tenant_id, t["occurrenceId"], t["ticketId"])
        if t.get("pendingTransferId"):
            view["pendingTransferId"] = t["pendingTransferId"]
            transfer = et.get(et.transfer_key(tenant_id, t["pendingTransferId"])) or {}
            view["pendingTransfer"] = {"transferId": t["pendingTransferId"], "kind": transfer.get("kind") or "gift",
                                       "status": transfer.get("status") or "pending",
                                       "priceOre": int(transfer.get("priceOre") or 0)}
        out.append(view)
    return http.respond(200, {
        "event": {"eventId": ev.get("eventId"), "title": ev.get("title"), "description": ev.get("description") or "",
                  "refundPolicy": (ev.get("settings") or {}).get("refundPolicy")},
        "occurrence": {"occurrenceId": occ.get("occurrenceId"), "startAt": occ.get("startAt"),
                       "endAt": occ.get("endAt"), "doorsAt": occ.get("doorsAt"), "status": occ.get("status")},
        "location": {"locationId": first["locationId"], "name": location.get("name")},
        "tickets": out, "wallet": passes.provider().available(),
        "transferAllowed": _transfer_open(ev, occ), "resaleAllowed": _resale_open(ev, occ)})


def ticket_pass(event):
    """K19: one valid ticket of this link as an Apple or Google Wallet pass (shared/features/
    events_tickets/passes.py). Wallet off, the kind's secret missing, or a ticket that is not this
    link's or no longer valid: 404."""
    tenant_id, tickets, _, _, _ = _token_scope(event)
    params = http.query(event)
    if set(params) - {"ticketId", "kind"}:
        raise et.EventsError(400, "unsupported query parameters")
    kind = params.get("kind")
    wallet = passes.provider()
    if kind not in passes.KINDS or not wallet.available().get(kind):
        raise et.EventsError(404, "not found")
    ticket = next((t for t in tickets if t["ticketId"] == params.get("ticketId") and t["status"] == "valid"), None)
    if not ticket:
        raise et.EventsError(404, "not found")
    ev = et.get(et.event_key(tenant_id, ticket["eventId"])) or {}
    occ = et.get(et.occ_key(tenant_id, ticket["eventId"],
                            et.split_occurrence(ticket["eventId"], ticket["occurrenceId"]))) or {}
    location = tenant.get_location(tenant_id, ticket["locationId"]) or {}
    data = passes.PassData(
        tenant_id=tenant_id, event_id=ticket["eventId"], ticket_id=ticket["ticketId"], title=ev.get("title") or "",
        start_at=occ.get("startAt") or "", end_at=occ.get("endAt") or "", location_name=location.get("name") or "",
        type_name=ticket.get("typeName") or "", holder_name=ticket.get("holderName") or "",
        qr=qr.payload(tenant_id, ticket["occurrenceId"], ticket["ticketId"]))
    return passes.response(wallet.issue(kind, data))


def create_transfer(event):
    tenant_id, tickets, order, _, _ = _token_scope(event)
    body = http.body(event, allowed=("transferId", "ticketId", "kind", "toEmail"))
    transfer_id = body.get("transferId")
    if not et.valid_id(transfer_id):
        raise et.EventsError(400, "transferId is invalid", "transferId")
    ticket = next((t for t in tickets if t["ticketId"] == body.get("ticketId")), None)
    if not ticket:
        raise et.EventsError(404, "not found")
    existing = et.get(et.transfer_key(tenant_id, transfer_id))
    if existing:
        if existing.get("ticketId") != ticket["ticketId"]:
            raise et.EventsError(409, "changed_retry")
        return http.respond(201, _transfer_view(existing))
    kind = body.get("kind")
    if kind not in ("gift", "resale"):
        raise et.EventsError(400, "kind must be gift or resale", "kind")
    if kind == "gift" and not et.valid_email(body.get("toEmail")):
        raise et.EventsError(400, "invalid_email", "toEmail")  # a listing (resale) has no recipient: toEmail ignored
    ev = et.get(et.event_key(tenant_id, ticket["eventId"])) or {}
    occ_start = et.split_occurrence(ticket["eventId"], ticket["occurrenceId"])
    occ = et.get(et.occ_key(tenant_id, ticket["eventId"], occ_start)) or {}
    if not (_transfer_open(ev, occ) if kind == "gift" else _resale_open(ev, occ)):
        raise et.EventsError(409, "transfer_closed")
    if ticket["status"] != "valid" or ticket.get("pendingTransferId"):
        raise et.EventsError(409, "invalid_state")
    price = int(ticket.get("priceOre") or 0)
    if kind == "resale" and price <= 0:
        raise et.EventsError(400, "invalid_price", "ticketId")  # a free ticket is given away, not sold
    if kind == "resale" and ticket.get("resaleOf"):
        raise et.EventsError(409, "resale_limit")  # one resale per seat: a bought-on ticket can be given, not sold
    at = et.now()
    if kind == "gift":
        # Each gift emails a stranger: a ticket may cause a few a day, an IP a few more an hour.
        counted = request_guard.check([("ticket-transfer", f"{tenant_id}|{ticket['ticketId']}",
                                        et.TRANSFERS_PER_TICKET_DAY, 86400),
                                       ("ticket-transfer-ip", http.source_ip(event), *TRANSFER_IP_LIMIT)], at)
    else:  # a listing emails nobody; the IP limit alone keeps a script from churning listings
        counted = request_guard.check([("ticket-transfer-ip", http.source_ip(event), *TRANSFER_IP_LIMIT)], at)
    transfer = {**et.transfer_key(tenant_id, transfer_id), "tenantId": tenant_id, "transferId": transfer_id,
                "ticketId": ticket["ticketId"], "kind": kind, "status": "pending", "createdAt": et.iso(at),
                "locationId": ticket["locationId"], "occurrenceId": ticket["occurrenceId"],
                "ttl": int(et.parse_iso(occ["startAt"]).timestamp()) + 86400,
                **et.transfer_gsi(tenant_id, ticket["ticketId"], transfer_id)}
    if kind == "gift":
        to = body["toEmail"].strip().lower()
        transfer.update({"toEmail": to, "toEmailHash": et.email_hash(tenant_id, to), "priceOre": 0})
    else:
        # Face value = what this ticket was sold for (its own priceOre), never above, never the type's new price.
        transfer.update({"priceOre": price, "vatRate": int(ticket.get("vatRate") or 0), "typeId": ticket["typeId"],
                         "typeName": ticket.get("typeName") or "", "sellerOrderId": ticket["orderId"],
                         **et.resale_gsi(tenant_id, ticket["eventId"], ticket["occurrenceId"], et.iso(at),
                                         transfer_id)})
    try:
        _txn([_update(et.ticket_key(tenant_id, ticket["ticketId"]),
                      "SET pendingTransferId = :t, pendingTransferKind = :k",
                      {":t": transfer_id, ":k": kind, ":valid": "valid", ":v": int(ticket["version"])},
                      "#s = :valid AND version = :v AND attribute_not_exists(pendingTransferId)", {"#s": "status"}),
              _put(transfer)])
    except ClientError as exc:
        request_guard.refund(counted)
        if not et.cancelled_txn(exc):
            raise
        raise et.EventsError(409, "invalid_state")
    if kind == "resale":
        return http.respond(201, _transfer_view(transfer))
    to = transfer["toEmail"]
    link = _site_link(tenant_id, "/biljett/ta-emot", qr.token("x", tenant_id, transfer_id))
    location = tenant.get_location(tenant_id, ticket["locationId"]) or {}
    try:
        _notify(tenant_id, ticket["locationId"], "events-tickets.transfer", to,
                f"En biljett till {ev.get('title', 'ett evenemang')}",
                f"Hej!\n\n{order.get('buyerName') or 'Någon'} ger dig en biljett ({ticket.get('typeName', '')}) till "
                f"{ev.get('title', '')} på {(location.get('name') or 'restaurangen').strip()}, "
                f"{_when(occ['startAt'], location)}.\n\n"
                + (f"Ta emot biljetten här: {link}\n" if link else ""))
    except NotificationUnavailable as exc:
        logger.warning(json.dumps({"route": "transfer", "notification": str(exc)}))
    return http.respond(201, _transfer_view(transfer))


def _transfer_view(t):
    return {"transferId": t["transferId"], "ticketId": t["ticketId"], "kind": t["kind"], "status": t["status"],
            "priceOre": int(t.get("priceOre") or 0)}


def accept_transfer(event):
    parsed = qr.read_token(_path(event, "transferToken"))
    if not parsed or parsed[0] != "x":
        raise et.EventsError(404, "not found")
    _, tenant_id, transfer_id = parsed
    tenant_row = tenant.get_tenant(tenant_id)
    if not tenant_row or tenant_row.get("status") != "active" or not (
            (tenant_row.get("entitlements") or {}).get("features") or {}).get(et.FEATURE_KEY):
        raise et.EventsError(404, "not found")
    body = http.body(event, allowed=("name",))
    try:
        name = et.text(body.get("name"), et.NAME_MAX, required=True)
    except ValueError:
        raise et.EventsError(400, "name is required", "name")
    transfer = et.get(et.transfer_key(tenant_id, transfer_id))
    if not transfer:
        raise et.EventsError(404, "not found")
    new_id = et.gift_ticket_id(transfer_id)
    if transfer["status"] == "accepted":
        return http.respond(200, {"ticketToken": qr.token("t", tenant_id, new_id)})
    if transfer["status"] != "pending":
        raise et.EventsError(409, "invalid_state")
    old = et.get(et.ticket_key(tenant_id, transfer["ticketId"]))
    if not old or old["status"] != "valid" or old.get("pendingTransferId") != transfer_id:
        raise et.EventsError(409, "invalid_state")
    at = et.now()
    new = {k: v for k, v in old.items() if k not in ("pendingTransferId", "pendingTransferKind", "checkedInAt",
                                                       "checkedInBy", "checkedInClientId", "holderEmail")}
    new.update({**et.ticket_key(tenant_id, new_id), "ticketId": new_id, "holderName": name,
                "holderEmailHash": transfer["toEmailHash"], "giftedFrom": old["ticketId"], "status": "valid",
                "version": 1, "issuedAt": et.iso(at),
                **et.occurrence_gsi(tenant_id, old["occurrenceId"], name, new_id)})
    try:
        _txn([_update(et.ticket_key(tenant_id, old["ticketId"]),
                      "SET #s = :tr, transferredTo = :n, version = version + :one"
                      " REMOVE pendingTransferId, pendingTransferKind",
                      {":tr": "transferred", ":n": new_id, ":one": 1, ":v": int(old["version"]), ":t": transfer_id},
                      "version = :v AND pendingTransferId = :t", {"#s": "status"}),
              _put(new),
              _update(et.transfer_key(tenant_id, transfer_id), "SET #s = :a, newTicketId = :n, acceptedAt = :at",
                      {":a": "accepted", ":n": new_id, ":at": et.iso(at), ":p": "pending"}, "#s = :p",
                      {"#s": "status"})])
    except ClientError as exc:
        if not et.cancelled_txn(exc):
            raise
        again = et.get(et.transfer_key(tenant_id, transfer_id)) or {}
        if again.get("status") == "accepted":
            return http.respond(200, {"ticketToken": qr.token("t", tenant_id, new_id)})
        raise et.EventsError(409, "invalid_state")
    _publish("Ticket.Transferred", {"tenantId": tenant_id, "locationId": old["locationId"],
                                    "occurrenceId": old["occurrenceId"], "ticketId": old["ticketId"],
                                    "newTicketId": new_id, "kind": "gift"})
    return http.respond(200, {"ticketToken": qr.token("t", tenant_id, new_id)})


def cancel_transfer(event):
    tenant_id, tickets, _, _, _ = _token_scope(event)
    transfer_id = _path(event, "transferId")
    transfer = et.get(et.transfer_key(tenant_id, transfer_id)) if et.valid_id(transfer_id) else None
    if not transfer or transfer["ticketId"] not in {t["ticketId"] for t in tickets}:
        raise et.EventsError(404, "not found")
    if transfer["status"] == "pending":
        try:
            _txn([_update(et.transfer_key(tenant_id, transfer_id), "SET #s = :c REMOVE GSI2PK, GSI2SK",
                          {":c": "cancelled", ":p": "pending"}, "#s = :p", {"#s": "status"}),
                  _update(et.ticket_key(tenant_id, transfer["ticketId"]),
                          "REMOVE pendingTransferId, pendingTransferKind", {":t": transfer_id},
                          "pendingTransferId = :t")])
        except ClientError as exc:
            if not et.cancelled_txn(exc):
                raise
            if (et.get(et.transfer_key(tenant_id, transfer_id)) or {}).get("status") in ("accepted", "claimed",
                                                                                           "sold"):
                raise et.EventsError(409, "invalid_state")
    elif transfer["status"] in ("accepted", "claimed", "sold"):
        # accepted: the gift is already the recipient's; claimed: a buyer is paying for the listing right
        # now (it comes back if the hold expires unpaid); sold: the money has moved.
        raise et.EventsError(409, "invalid_state")
    return {"statusCode": 204, "headers": {"Cache-Control": "no-store"}, "body": ""}


# --- hooks ---------------------------------------------------------------------------------------

def stripe_event(event):
    tenant_id, kind, account, obj = event.get("tenantId"), event.get("type"), event.get("account"), \
        event.get("object") or {}
    meta = obj.get("metadata") or {}
    order_id = meta.get("orderId")
    if not et.valid_id(order_id) or meta.get("tenantId") != tenant_id:
        return "ignored:unknown_order"
    order = et.get(et.order_key(tenant_id, order_id))
    if not order:
        return "ignored:unknown_order"
    # Another restaurant's connected account can never issue or void this restaurant's tickets.
    if not account or account != order.get("stripeAccountId"):
        return "ignored:account_mismatch"
    if kind == "payment_intent.succeeded":
        if obj.get("id") != order.get("paymentIntentId") or int(obj.get("amount") or 0) != int(order["amountOre"]):
            return "ignored:mismatch"
        return _issue(tenant_id, order, obj)
    if kind == "payment_intent.payment_failed":
        return "ignored:payment_failed"  # the hold stays until it expires; the guest may try again
    if kind in ("charge.refunded", "charge.dispute.closed"):
        if kind == "charge.dispute.closed" and obj.get("status") != "lost":
            return "ignored:dispute_won"
        refunded, charged = int(obj.get("amount_refunded") or 0), int(obj.get("amount") or 0)
        ours = int(order.get("refundedOre") or 0)  # the order's refund TX rows add up to this
        if kind == "charge.refunded" and refunded != charged:
            if refunded <= ours:
                return "ignored:own_refund"
            logger.warning(json.dumps({"event": "ticket.partial_refund", "tenantId": tenant_id, "orderId": order_id}))
            return "flagged:partial_refund"
        voided = 0
        for t in et.tickets_of_order(tenant_id, order):
            if t["status"] not in ("valid", "checked_in"):
                continue
            try:
                _refund_ticket(tenant_id, t, f"stripe-{t['ticketId']}", by="owner", reason=kind, stripe="external")
            except et.EventsError as exc:  # one ticket that moved meanwhile never leaves the others valid
                logger.warning(json.dumps({"event": "ticket.external_refund", "ticketId": t["ticketId"],
                                           "code": exc.code}))
                continue
            voided += 1
        return "voided" if voided else "already_refunded"
    return "ignored:type"


T_SALE = "Biljetter"
T_REFUND = "Återbetalda biljetter"
# A resale is a sale to the buyer and a refund to the seller of the same amount the same day: the two
# labels keep both legs visible in the entry, and they net to zero per VAT rate. NEEDS ACCOUNTANT REVIEW
# (whether a face-value resale should be booked gross like this or left out of the entry).
T_RESALE = "Vidaresålda biljetter"
T_RESALE_REFUND = "Återbetalda vidaresålda biljetter"
T_LABELS = {"sale": T_SALE, "refund": T_REFUND, "resale_sale": T_RESALE, "resale_refund": T_RESALE_REFUND}


def accounting_hook(event):
    """Postings per day from the TX rows (byLocationDay): sale -> debit the
    Stripe settlement, credit sales and output VAT at the ticket type's rate;
    refund -> the reverse; a resale is both (net zero). Uses the existing
    sales<rate>/vat<rate> roles until the accountant approves `ticketSales*`
    (ADR-K 10, NEEDS ACCOUNTANT REVIEW)."""
    tenant_id, location_id, first, last = event["tenantId"], event.get("locationId"), event.get("first"), \
        event.get("last")
    if not et.valid_day(first) or not et.valid_day(last) or first > last:
        return {"ok": False, "status": 400, "code": "invalid_range"}
    out = []
    for day in et.days_between(first, last):
        rows = et.query_all(IndexName=et.GSI2, KeyConditionExpression=Key("GSI2PK").eq(
            f"TENANT#{tenant_id}#LOC#{location_id}#{day}"))
        totals = {}
        for tx in rows:
            label = T_LABELS.get(tx.get("kind")) or (T_SALE if int(tx["amountOre"]) >= 0 else T_REFUND)
            for line in tx.get("vatLines") or []:
                key = (label, int(line["rate"]))
                net, vat = totals.get(key, (0, 0))
                totals[key] = (net + int(line["netOre"]), vat + int(line["vatOre"]))
        postings = []
        for (label, rate), (net, vat) in sorted(totals.items()):
            if not net and not vat:
                continue
            postings += [{"role": "stripe", "amount": net + vat, "text": f"{label} {rate} % moms"},
                         {"role": f"sales{rate}", "amount": -net, "text": f"{label} {rate} % moms"},
                         {"role": f"vat{rate}", "amount": -vat, "text": f"{label} utgående moms {rate} %"}]
        postings = [p for p in postings if p["amount"]]
        if postings:
            out.append({"day": day, "postings": postings, "warnings": []})
    return {"ok": True, "events": out}


def insights_hook(event):
    tenant_id, first, last = event["tenantId"], event.get("first"), event.get("last")
    location_ids = event.get("locationIds") or []
    if not et.valid_day(first) or not et.valid_day(last) or first > last or not isinstance(location_ids, list):
        return {"ok": False, "status": 400, "code": "invalid_range"}
    days = {d: {"day": d, "occurrences": 0, "ticketsSold": 0, "revenueOre": 0, "checkedIn": 0}
            for d in et.days_between(first, last)}
    for location_id in location_ids:
        for day in days:
            for tx in et.query_all(IndexName=et.GSI2, KeyConditionExpression=Key("GSI2PK").eq(
                    f"TENANT#{tenant_id}#LOC#{location_id}#{day}")):
                sign = 1 if int(tx["amountOre"]) >= 0 else -1
                days[day]["ticketsSold"] += sign * len(tx.get("ticketIds") or [])
                days[day]["revenueOre"] += int(tx["amountOre"])
    locations = set(location_ids)
    for ev in et.events_of(tenant_id):
        if ev.get("locationId") not in locations:
            continue
        tz = et.zone(tenant.get_location(tenant_id, ev["locationId"]))
        occs, _ = et.event_parts(tenant_id, ev["eventId"])
        for occ in occs:
            day = et.local_day(et.parse_iso(occ["startAt"]), tz)
            if day in days and occ["status"] != "cancelled":
                days[day]["occurrences"] += 1
                days[day]["checkedIn"] += int(occ.get("checkedIn") or 0)
    return {"ok": True, "days": [v for v in days.values() if any(v[k] for k in v if k != "day")]}


ORDER_CONTACT = ("buyerName", "buyerEmail", "buyerPhone", "buyerEmailHash")
TICKET_CONTACT = ("holderName", "holderEmail", "holderEmailHash")
TRANSFER_CONTACT = ("toEmail", "toEmailHash")


def _strip(tenant_id, row, fields, extra_set=None):
    expr = "REMOVE " + ", ".join(fields)
    values = None
    if extra_set:
        expr = "SET " + ", ".join(f"{k} = :{k}" for k in extra_set) + " " + expr
        values = {f":{k}": v for k, v in extra_set.items()}
    kwargs = {"Key": {"PK": row["PK"], "SK": row["SK"]}, "UpdateExpression": expr}
    if values:
        kwargs["ExpressionAttributeValues"] = values
    et.table().update_item(**kwargs)


def _strip_ticket(tenant_id, t):
    # GSI1SK starts with the holder name (guest list order): rewritten without it.
    _strip(tenant_id, t, TICKET_CONTACT, {"GSI1SK": f"#{t['ticketId']}"} if t.get("GSI1PK") else None)


def privacy_hook(event, erase):
    tenant_id = event["tenantId"]
    email = event.get("email")
    if not isinstance(email, str) or not email.strip():
        return {"items": []} if not erase else {"erased": {}, "kept": {}}
    wanted = et.email_hash(tenant_id, email)
    orders_ = [o for o in et.partition(tenant_id, "ORDER#") if o.get("buyerEmailHash") == wanted]
    order_ids = {o["orderId"] for o in orders_}
    tickets = [t for t in et.partition(tenant_id, "TICKET#") if t.get("holderEmailHash") == wanted]
    transfers = [t for t in et.partition(tenant_id, "TRANSFER#") if t.get("toEmailHash") == wanted]
    if not erase:
        return {"items": [
            *[{"kind": "ticket_order", "orderId": o["orderId"], "occurrenceId": o["occurrenceId"],
               "qty": int(o["qty"]), "amountOre": int(o["amountOre"]), "status": o["status"],
               "buyerName": o.get("buyerName"), "createdAt": o.get("createdAt")} for o in orders_],
            *[{"kind": "ticket", "ticketId": t["ticketId"], "occurrenceId": t["occurrenceId"],
               "holderName": t.get("holderName"), "status": t["status"]} for t in tickets
              if t.get("orderId") not in order_ids],
            *[{"kind": "ticket_transfer", "transferId": t["transferId"], "status": t["status"]} for t in transfers]]}
    for o in orders_:
        _strip(tenant_id, o, ORDER_CONTACT)
        for t in et.tickets_of_order(tenant_id, o, gifted=False):
            _strip_ticket(tenant_id, t)
    for t in tickets:
        _strip_ticket(tenant_id, t)
    for t in transfers:
        _strip(tenant_id, t, TRANSFER_CONTACT)
    # Tickets stay valid and TX rows stay (bookkeeping records).
    return {"erased": {"ticketOrders": len(orders_), "tickets": len(tickets), "ticketTransfers": len(transfers)},
            "kept": {"ticketTransactions": len([t for t in et.partition(tenant_id, "TX#")
                                                if t.get("orderId") in order_ids])}}


def purge_hook(event):
    tenant_id = event["tenantId"]
    rows = et.query_all(KeyConditionExpression=Key("PK").eq(et.pk(tenant_id)), ConsistentRead=True)
    last_tx = max([r.get("at") for r in rows if r["SK"].startswith("TX#")] or [et.iso(et.now())])
    keep_until = int(et.parse_iso(last_tx).replace(month=12, day=31, hour=23, minute=59).timestamp()) \
        + et.BOOKKEEPING_YEARS * 366 * 86400
    deleted = kept = 0
    for row in rows:
        if row["SK"].startswith("TX#"):
            et.table().update_item(Key={"PK": row["PK"], "SK": row["SK"]}, UpdateExpression="SET #ttl = :t",
                                   ExpressionAttributeNames={"#ttl": "ttl"},
                                   ExpressionAttributeValues={":t": keep_until})
            kept += 1
        else:
            et.table().delete_item(Key={"PK": row["PK"], "SK": row["SK"]})
            deleted += 1
    return {"done": True, "counts": {"deleted": deleted, "keptUntilBookkeepingEnds": kept}}


def _hook(event):
    hook, tenant_id = event.get("hook"), event.get("tenantId")
    if not isinstance(tenant_id, str) or not tenant_id:
        return {"ok": False, "status": 400, "code": "tenant_required"}
    try:
        if hook == "stripe.event":
            return {"ok": True, "result": stripe_event(event)}
        if hook == "accounting.events":
            return accounting_hook(event)
        if hook == "insights.events":
            return insights_hook(event)
        if hook in ("privacy.export", "privacy.erase"):
            return {"ok": True, **privacy_hook(event, erase=hook == "privacy.erase")}
        if hook == "tenant.purge":
            return {"ok": True, **purge_hook(event)}
    except et.EventsError as exc:
        return {"ok": False, "status": exc.status, "code": exc.code.replace(" ", "_")}
    return {"ok": False, "status": 400, "code": "unknown_hook"}


# --- scheduled jobs ------------------------------------------------------------------------------

def run_sweep(context=None):
    """Every 5 minutes: expired holds are released (the PaymentIntent is
    cancelled first so it can no longer be paid; a payment that still
    arrives is issued if seats are free, else refunded), every refund TX
    row Stripe still owes is retried (whatever route created it), open
    mass refund jobs continue. Idempotent and resumable: each row is
    conditional, and the work stops JOB_MARGIN_MS before the Lambda's
    deadline (`context.get_remaining_time_in_millis`) for the next run."""
    at = et.iso(et.now())
    time_left = _time_left(context)
    counts = {"expired": 0, "refunds": 0, "jobs": 0, "failed": 0, "stopped": False}
    for row in et.query_all(IndexName=et.GSI1, KeyConditionExpression=Key("GSI1PK").eq("HOLD")
                            & Key("GSI1SK").lt(at)):
        if not _in_time(time_left):
            counts["stopped"] = True
            break
        tenant_id, order_id = row.get("tenantId"), row.get("orderId")
        try:
            order = et.get(et.order_key(tenant_id, order_id))
            if not order or order["status"] != "pending":
                continue
            if order.get("paymentIntentId"):
                try:
                    stripe.request("POST", f"/v1/payment_intents/{order['paymentIntentId']}/cancel",
                                   account=order["stripeAccountId"],
                                   idempotency_key=f"ticket-cancel-{order_id}")
                except stripe.StripeError:
                    pass  # already succeeded or processing: the webhook decides
            counts["expired"] += 1 if _expire_order(tenant_id, order) else 0
        except (ClientError, BotoCoreError, stripe.StripeUnavailable) as exc:
            counts["failed"] += 1
            logger.warning(json.dumps({"sweep": "hold_failed", "type": type(exc).__name__}))
    for tx in et.pending_refunds():
        if not _in_time(time_left):
            counts["stopped"] = True
            break
        try:
            _stripe_refund(tx["tenantId"], tx)
            counts["refunds"] += 1
        except (et.EventsError, ClientError, BotoCoreError) as exc:
            counts["failed"] += 1
            logger.warning(json.dumps({"sweep": "refund_failed", "txId": tx.get("txId"), "type": type(exc).__name__}))
    for row in et.query_all(IndexName=et.GSI1, KeyConditionExpression=Key("GSI1PK").eq("JOBS")):
        if not _in_time(time_left):
            counts["stopped"] = True
            break
        try:
            _run_job(row["tenantId"], row["jobId"], time_left=time_left)
            counts["jobs"] += 1
        except Exception as exc:
            counts["failed"] += 1
            logger.warning(json.dumps({"sweep": "job_failed", "type": type(exc).__name__}))
    logger.info(json.dumps({"job": "sweep", **counts}))
    return counts


def run_nightly():
    """Contact fields of orders, their tickets and transfers removed 24
    months after the occurrence (GSI2 CLEANUP#<month>, the current and the
    two months before, so a missed night is caught up). Each row leaves the
    index when done, so a re-run only does what is left."""
    today = et.now()
    counts = {"cleaned": 0, "failed": 0}
    for back in (2, 1, 0):
        month = et.add_months(today.replace(day=1), -back).strftime("%Y-%m")
        for row in et.query_all(IndexName=et.GSI2, KeyConditionExpression=Key("GSI2PK").eq(f"CLEANUP#{month}")):
            tenant_id, order_id = row.get("tenantId"), row.get("orderId")
            try:
                order = et.get(et.order_key(tenant_id, order_id))
                if not order:
                    continue
                # The order's tickets by deterministic id (and the ones they were gifted on to), each
                # ticket's transfers by its GSI1 row: key lookups, no partition scan.
                for t in et.tickets_of_order(tenant_id, order):
                    _strip_ticket(tenant_id, t)
                    for transfer in et.transfers_of_ticket(tenant_id, t["ticketId"]):
                        _strip(tenant_id, transfer, TRANSFER_CONTACT)
                _strip(tenant_id, order, ORDER_CONTACT + ("GSI2PK", "GSI2SK"))
                counts["cleaned"] += 1
            except (ClientError, BotoCoreError) as exc:
                counts["failed"] += 1
                logger.warning(json.dumps({"nightly": "order_failed", "type": type(exc).__name__}))
    logger.info(json.dumps({"job": "nightly", **counts}))
    return counts


# --- dispatch --------------------------------------------------------------------------------------

ROUTES = {
    "GET /locations/{locationId}/events": list_events,
    "POST /locations/{locationId}/events": create_event,
    "GET /locations/{locationId}/events/{eventId}": get_event,
    "PUT /locations/{locationId}/events/{eventId}": update_event,
    "POST /locations/{locationId}/events/{eventId}/publish": publish_event,
    "PUT /locations/{locationId}/events/{eventId}/ticket-types/{typeId}": put_type,
    "DELETE /locations/{locationId}/events/{eventId}/ticket-types/{typeId}": delete_type,
    "PUT /locations/{locationId}/events/{eventId}/occurrences/{occurrenceId}": put_occurrence,
    "POST /locations/{locationId}/events/{eventId}/occurrences/{occurrenceId}/cancel": cancel_occurrence,
    "GET /locations/{locationId}/events/{eventId}/occurrences/{occurrenceId}/guest-list": guest_list,
    "POST /locations/{locationId}/events/{eventId}/occurrences/{occurrenceId}/check-ins": check_ins,
    "GET /locations/{locationId}/events/{eventId}/orders": list_orders,
    "POST /locations/{locationId}/events/tickets/{ticketId}/refund": refund_ticket,
    "GET /locations/{locationId}/events/jobs/{jobId}": get_job,
    "GET /public/locations/{locationId}/events": public_list,
    "GET /public/locations/{locationId}/events/{eventId}": public_detail,
    "POST /public/locations/{locationId}/events/checkout": checkout,
    "GET /public/tickets/{token}": ticket_page,
    "GET /public/tickets/{token}/pass": ticket_pass,
    "POST /public/tickets/{token}/transfers": create_transfer,
    "POST /public/ticket-transfers/{transferToken}/accept": accept_transfer,
    "DELETE /public/tickets/{token}/transfers/{transferId}": cancel_transfer,
}


def handler(event, context=None):
    if isinstance(event, dict) and "requestContext" not in event:
        if "hook" in event:
            return _hook(event)
        if event.get("job") == "sweep":
            return run_sweep(context)
        if event.get("job") == "nightly":
            return run_nightly()
        return {"ok": False, "status": 400, "code": "unknown_event"}
    method, path = http.route(event)
    route = f"{method} {path}"
    if route not in ROUTES:
        return _err(404, "not found")
    try:
        if path.startswith("/public/"):
            return ROUTES[route](event)
        try:
            location_id = http.path_id(event, "locationId")
        except ValueError:
            return _err(404, "not found")
        ctx = tenant.for_jwt(event, location_id=location_id, feature=et.FEATURE_KEY)
        _invocation["time_left"] = _time_left(context)
        return ROUTES[route](event, ctx)
    except tenant.TenantError as exc:
        return exc.response()
    except request_guard.Limited as exc:
        return http.respond(429, {"error": "too_many_requests", "code": "too_many_requests"},
                            {"Retry-After": str(exc.retry_after)})
    except et.EventsError as exc:
        if " " in exc.code:
            return http.respond(exc.status, {"error": exc.code, **({"field": exc.field} if exc.field else {})})
        return _err(exc.status, exc.code, exc.field)
    except turnstile.Unavailable:
        return _err(503, "service_unavailable")
    except ValueError as exc:
        return http.respond(400, {"error": str(exc)})
    except (BotoCoreError, ClientError) as exc:
        logger.error(json.dumps({"route": route, "aws": type(exc).__name__, "msg": str(exc)[:300]}))
        return _err(503, "service_unavailable")

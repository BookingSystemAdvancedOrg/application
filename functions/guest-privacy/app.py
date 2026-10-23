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
    GET    /privacy/guests?email=&phone=    everything stored about one guest
                                            (JSON export, GDPR art. 15/20)
    POST   /privacy/guests/erase            {email?, phone?} erase one guest
                                            (GDPR art. 17)

PURPOSE:
    The restaurant (controller) edits the privacy notice its guests see
    (shared/privacy_notice.py) and handles a guest who asks, by phone or in
    person, to stop getting offers. A withdrawal is a new record in the
    consent log (shared/consent.py), never an overwrite, and it affects
    marketing only: booking and order messages go out as before. The
    subscriber list is updated in the same step so no campaign reaches the
    guest. Not behind the "marketing" feature: a restaurant that turned
    marketing off must still be able to answer a guest.

    Export and erasure cover the marketing subscriber and welcome-offer
    rows, the consent log, and the guest's bookings, orders and catering
    requests in every location of the restaurant. Erasure:
      * consent: a withdrawal (source "erasure") on every channel with
        records; data-retention turns it into a bare suppression record at
        its next run (this function stays append-only on the log)
      * subscriber row and the welcome-offer address/code rows: deleted
        (a code reserved by a checkout or redeemed stays: it holds no address)
      * bookings, orders, catering requests: anonymised with the retention
        rules (shared/retention.anonymise_update): receipt fields, company
        invoice details and catering signatures stay (bokföringslagen,
        signed contracts); S3 documents are untouched
      * still in progress (a coming booking, an open order, a catering
        request not yet delivered or closed) is kept and listed as "kept":
        the restaurant cancels it first, then erases again

    Logs carry the tenant id and counts only, never the guest's address.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    USER_TABLE_NAME, CONSENT_TABLE_NAME, MARKETING_TABLE_NAME (optional),
    RESERVATION_TABLE_NAME, ORDER_TABLE_NAME, CATERING_TABLE_NAME

AWS RESOURCE ACCESS:
    GetItem/UpdateItem on the tenant table (PROFILE); Query/PutItem on the
    consent table; GetItem/UpdateItem/DeleteItem/Query on the marketing
    table; Query on the location table (own partition); Query/UpdateItem on
    the reservation, order and catering-requests tables.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from shared import catering, consent, dynamo, http, marketing, orders, privacy_notice, retention, tenant
from shared import marketing_offer as offer
from shared import reservations as r

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_NOTICE = "/privacy/notice"
_CONSENTS = "/privacy/consents"
_WITHDRAW = "/privacy/consents/withdraw"
_GUESTS = "/privacy/guests"
_ERASE = "/privacy/guests/erase"

# (kind, table env, SK prefix, export key) of the guest rows per location.
_GUEST_ROWS = (("booking", "RESERVATION_TABLE_NAME", "RESERVATION#", "bookings"),
               ("order", "ORDER_TABLE_NAME", "ORDER#", "orders"),
               ("catering", "CATERING_TABLE_NAME", "REQUEST#", "cateringRequests"))
_CATERING_IN_PROGRESS = (catering.REQUESTED, catering.OFFER_SENT, catering.CHANGE_REQUESTED, catering.SIGNED,
                         catering.CONFIRMED)
_ORDER_IN_PROGRESS = (orders.PENDING_PAYMENT, *orders.OPEN_STATUSES)


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
        # The subscriber list follows even without a grant on record (a
        # subscriber from before the consent log): no campaign reaches them.
        if channel == consent.EMAIL and os.environ.get("MARKETING_TABLE_NAME"):
            _unsubscribe(ctx.tenant_id, email)
        if consent.state(table, ctx.tenant_id, channel, identifier) != "granted":
            continue  # nothing to withdraw: no record is added
        consent.withdraw(table, tenant_id=ctx.tenant_id, channel=channel, identifier=identifier, source="admin",
                         actor=ctx.sub)
        withdrawn.append(channel)
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


# --- export and erasure ----------------------------------------------------------------

def _plain(value):
    """DynamoDB values -> JSON (string sets become sorted lists)."""
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _export_view(item):
    """A stored row without keys, link hashes and expiry."""
    return {k: _plain(v) for k, v in item.items()
            if k not in ("PK", "SK", "ttl") and not k.endswith("Hash")}


def _matches(item, email, phone):
    stored_email = item.get("customerEmail")
    if email and isinstance(stored_email, str) and stored_email.strip().lower() == email:
        return True
    return bool(phone and item.get("customerPhone") == phone)


def _guest_rows(ctx, email, phone):
    """(kind, table name, item) of every booking, order and catering request
    of the guest in the restaurant's locations."""
    # shortcut: reads each location's whole partition (no index by contact), fine for rare owner requests;
    # add a GSI on a hashed contact if a location's history grows past what 28 s can read.
    for location in tenant.list_locations(ctx.tenant_id):
        location_id = location.get("locationId") or str(location.get("SK", "")).removeprefix("LOCATION#")
        for kind, env, prefix, _ in _GUEST_ROWS:
            name = os.environ.get(env)
            if not name:
                continue
            table = dynamo.table(name)
            request = {"KeyConditionExpression": Key("PK").eq(f"LOCATION#{location_id}")
                       & Key("SK").begins_with(prefix)}
            while True:
                page = table.query(**request)
                for item in page.get("Items") or []:
                    if _matches(item, email, phone):
                        yield kind, name, {**item, "locationId": item.get("locationId") or location_id}
                if "LastEvaluatedKey" not in page:
                    break
                request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _marketing_rows(tenant_id, email):
    """(subscriber row, welcome-offer address row, code row), None where absent."""
    if not email or not os.environ.get("MARKETING_TABLE_NAME"):
        return None, None, None
    table = dynamo.table(os.environ["MARKETING_TABLE_NAME"])
    sub = table.get_item(Key=marketing.sub_key(tenant_id, marketing.subscriber_id(tenant_id, email)),
                         ConsistentRead=True).get("Item")
    address = table.get_item(Key=offer.email_key(tenant_id, offer.owner_key(tenant_id, email)),
                             ConsistentRead=True).get("Item")
    code = None
    if address and address.get("code"):
        code = table.get_item(Key=offer.code_key(tenant_id, address["code"]), ConsistentRead=True).get("Item")
    return sub, address, code


def _export(ctx, email, phone):
    out = {"email": email, "phone": phone, "bookings": [], "orders": [], "cateringRequests": []}
    name = consent.table_name()
    out["consents"] = _lookup(ctx, dynamo.table(name), email, phone) if name else {}
    sub, _, code = _marketing_rows(ctx.tenant_id, email)
    out["subscriber"] = _export_view(sub) if sub else None
    out["welcomeOfferCode"] = ({k: _plain(code.get(k)) for k in ("code", "percent", "status", "expiresAt",
                                                                "createdAt", "redeemedAt") if code.get(k) is not None}
                               if code else None)
    keys = {kind: key for kind, _, _, key in _GUEST_ROWS}
    for kind, _, item in _guest_rows(ctx, email, phone):
        out[keys[kind]].append(_export_view(item))
    out["exportedAt"] = consent.now_iso()
    logger.info(json.dumps({"guestData": "exported", "tenantId": ctx.tenant_id,
                            "rows": sum(len(out[k]) for k in keys.values())}))
    return out


def _in_progress(kind, item, today):
    if kind == "booking":
        return item.get("status") in r.ACTIVE and str(item.get("date") or "") >= today
    if kind == "order":
        return item.get("status") in _ORDER_IN_PROGRESS
    return item.get("status") in _CATERING_IN_PROGRESS


def _anonymise(name, kind, item, now):
    expr, names, values = retention.anonymise_update(kind, item, now)
    names["#pk"] = "PK"
    try:
        dynamo.table(name).update_item(
            Key={"PK": item["PK"], "SK": item["SK"]}, UpdateExpression=expr,
            ConditionExpression="attribute_exists(#pk) AND attribute_not_exists(#a)",
            ExpressionAttributeNames=names, ExpressionAttributeValues=values)
        return True
    except ClientError as exc:
        if not consent.conditional_failed(exc):
            raise
        return False


def _delete_marketing(tenant_id, email):
    sub, address, code = _marketing_rows(tenant_id, email)
    table = dynamo.table(os.environ["MARKETING_TABLE_NAME"]) if (sub or address) else None
    deleted = 0
    for row in (sub, address):
        if row:
            table.delete_item(Key={"PK": row["PK"], "SK": row["SK"]})
            deleted += 1
    if code and code.get("status") not in ("reserved", "redeemed"):
        try:
            table.delete_item(Key={"PK": code["PK"], "SK": code["SK"]}, ConditionExpression="#s = :s",
                              ExpressionAttributeNames={"#s": "status"},
                              ExpressionAttributeValues={":s": code.get("status")})
            deleted += 1
        except ClientError as exc:
            if not consent.conditional_failed(exc):
                raise
    return deleted


def _erase(ctx, email, phone):
    now = consent.now_iso()
    # A booking from yesterday (UTC) may still be today in Stockholm: one day of margin.
    today = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    result = {"consentWithdrawn": [], "marketingRowsDeleted": 0, "anonymised": 0, "kept": []}
    name = consent.table_name()
    if name:
        table = dynamo.table(name)
        for channel, identifier in ((consent.EMAIL, email), (consent.SMS, phone)):
            if identifier and consent.history(table, ctx.tenant_id, consent.subject(ctx.tenant_id, channel,
                                                                                    identifier), limit=1):
                consent.withdraw(table, tenant_id=ctx.tenant_id, channel=channel, identifier=identifier,
                                 source="erasure", actor=ctx.sub)
                result["consentWithdrawn"].append(channel)
    if email and os.environ.get("MARKETING_TABLE_NAME"):
        result["marketingRowsDeleted"] = _delete_marketing(ctx.tenant_id, email)
    for kind, table_name, item in _guest_rows(ctx, email, phone):
        if _in_progress(kind, item, today):
            result["kept"].append({"kind": kind, "locationId": item["locationId"], "status": item.get("status"),
                                   "date": item.get("date") or item.get("deliveryDate"),
                                   "id": item.get("reservationId") or item.get("orderId") or item.get("requestId")})
            continue
        if item.get("anonymisedAt"):
            continue
        result["anonymised"] += int(_anonymise(table_name, kind, item, now))
    logger.info(json.dumps({"guestData": "erased", "tenantId": ctx.tenant_id, "anonymised": result["anonymised"],
                            "kept": len(result["kept"]), "marketingRows": result["marketingRowsDeleted"],
                            "channels": result["consentWithdrawn"]}))
    return result


def _guests(ctx, method, path, event):
    if method == "GET" and path == _GUESTS:
        q = http.query(event)
        email, phone = _guest(q.get("email"), q.get("phone"))
        return http.respond(200, _export(ctx, email, phone))
    data = http.body(event, allowed={"email", "phone"})
    email, phone = _guest(data.get("email"), data.get("phone"))
    return http.respond(200, _erase(ctx, email, phone))


def handler(event, context):
    method, path = http.route(event)
    try:
        ctx = tenant.for_jwt(event, owner_only=True)
        if path == _NOTICE and method in ("GET", "PUT", "DELETE"):
            return _notice(ctx, method, event)
        if (path == _CONSENTS and method == "GET") or (path == _WITHDRAW and method == "POST"):
            return _consents(ctx, method, path, event)
        if (path == _GUESTS and method == "GET") or (path == _ERASE and method == "POST"):
            return _guests(ctx, method, path, event)
        return http.error(404, "not found")
    except tenant.TenantError as exc:
        return exc.response()
    except ValueError as exc:
        return http.error(400, str(exc))
    except (BotoCoreError, ClientError):
        logger.exception("guest-privacy failed")
        return http.error(503, "privacy service unavailable")

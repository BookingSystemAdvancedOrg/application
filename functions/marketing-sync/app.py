"""marketing-sync

TRIGGER:
    * DynamoDB Streams (event source mappings, ReportBatchItemFailures):
        reservation table  INSERT/MODIFY
        order table        MODIFY (the payment turning paid)
    * EventBridge: SES events for marketing emails (configuration set
      "marketing"): Bounce, Complaint, Delivery.

PURPOSE:
    Keeps the marketing data in step with what guests actually do, without
    any other function knowing about marketing:
      reservation becomes reserved (online booking, card guarantee done)
          -> +1 booking / +party size guests for the day, channel and
             campaign it came from (its `attribution`)
          -> guest ticked "offers": subscribed (consent time + source kept)
      reservation arrives, or is reserved for a date
          -> the subscriber's last visit (for "visited within / not since")
      order payment turns paid
          -> +1 order and its total for its channel and campaign
          -> its welcome-offer code is used up (offer results); a pending
             sign-up behind the code is confirmed and its grant goes into
             the consent log
          -> guest ticked "offers" when ordering: subscribed
      SES bounce (permanent) or complaint for a marketing email
          -> the address is suppressed for good; campaign counters (also
             for Google review requests: their MSG# row carries the
             consent subject, so a complaint withdraws without a subscriber)
    Only tenants whose plan has "marketing" get statistics and subscribers.
    A ticked box on a later booking subscribes a guest again after they
    unsubscribed (that is a new consent); bounced and complained addresses
    stay suppressed.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    MARKETING_TABLE_NAME, RESERVATION_TABLE_NAME, ORDER_TABLE_NAME,
    CONSENT_TABLE_NAME (optional)

AWS RESOURCE ACCESS:
    Stream read on the reservation and order tables; GetItem/PutItem/
    UpdateItem on the marketing table; Query/PutItem on the consent table;
    tenant-context read policy.
"""

import json
import logging
import os

from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import BotoCoreError, ClientError

from shared import consent, dynamo, marketing as m, tenant
from shared import marketing_offer as offer

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_deserializer = TypeDeserializer()
_COUNTED = ("reserved", "arrived")
_SUPPRESSED = ("bounced", "complained")


def _image(raw):
    return {k: _deserializer.deserialize(v) for k, v in (raw or {}).items()}


def _table():
    return dynamo.table(m.table_name())


def _table_name_of(arn):
    parts = (arn or "").split(":table/", 1)
    return parts[1].split("/", 1)[0] if len(parts) == 2 else None


def _has_marketing(tenant_id):
    row = tenant.get_tenant(tenant_id) if tenant_id else None
    if not row or row.get("status") != "active":
        return None
    features = ((row.get("entitlements") or {}).get("features") or {})
    return row if features.get("marketing") else None


def _attr(item):
    a = item.get("attribution") or {}
    return a.get("channel") or "direct", a.get("campaign"), a.get("campaignId")


# --- reservations ----------------------------------------------------------------------

def _reservation(new, old):
    tenant_id = new.get("tenantId")
    tenant_row = _has_marketing(tenant_id)
    if not tenant_row:
        return
    table = _table()
    status, was = new.get("status"), (old or {}).get("status")
    if status == "reserved" and was not in _COUNTED and new.get("source") == "online" \
            and m.first_time(table, tenant_id, "BOOKING", new.get("reservationId")):
        channel, campaign, campaign_id = _attr(new)
        day = m.local_day(new.get("createdAt"), new.get("timezone"))
        guests = int(new.get("partySize") or 0)
        m.bump_stats(table, tenant_id, day, new.get("locationId"), channel, campaign, bookings=1, guests=guests)
        m.bump_campaign(table, tenant_id, campaign_id, bookings=1, guests=guests)
    if status in _COUNTED and m.valid_email(new.get("customerEmail")):
        _subscriber(table, tenant_row, new, opted_in=bool(new.get("marketingOptIn")) and was not in _COUNTED)


def _subscriber(table, tenant_row, item, *, opted_in, source="booking"):
    m.upsert_subscriber(table, tenant_id=item["tenantId"], restaurant=tenant_row.get("name"),
                        email=item["customerEmail"], name=item.get("customerName"), source=source,
                        ref_id=item.get("reservationId") or item.get("orderId") or "",
                        location_id=item.get("locationId"), visit=item.get("date"), opted_in=opted_in)


# --- orders ---------------------------------------------------------------------------

def _order(new, old):
    if new.get("paymentStatus") != "paid" or (old or {}).get("paymentStatus") == "paid":
        return
    tenant_id = new.get("tenantId")
    table = _table()
    total = int(new.get("totalAmount") or 0)
    discount = new.get("discount") or {}
    if discount.get("code") and tenant_id:
        # Always, even if the plan lost marketing meanwhile: the code is used.
        _redeem(table, tenant_id, new, discount, total)
    tenant_row = _has_marketing(tenant_id)
    if not tenant_row:
        return
    if m.first_time(table, tenant_id, "ORDER", new.get("orderId")):
        channel, campaign, campaign_id = _attr(new)
        day = m.local_day(new.get("createdAt"), new.get("timezone"))
        m.bump_stats(table, tenant_id, day, new.get("locationId"), channel, campaign, orders=1, revenueOre=total)
        m.bump_campaign(table, tenant_id, campaign_id, orders=1, revenueOre=total)
    if m.valid_email(new.get("customerEmail")):
        _subscriber(table, tenant_row, new, opted_in=bool(new.get("marketingOptIn")), source="order")


def _redeem(table, tenant_id, order, discount, total):
    outcome = offer.redeem_code(table, tenant_id, discount["code"], order.get("orderId"), total,
                                int(discount.get("amount") or 0))
    if outcome == "other":
        # Two paid orders on one code: should never happen (checkout holds
        # it). Logged as an error so the alarm on errors catches it.
        logger.error(json.dumps({"marketing": "code_used_twice", "tenantId": tenant_id, "code": discount["code"],
                                 "orderId": order.get("orderId")}))
        return
    code_item = table.get_item(Key=offer.code_key(tenant_id, discount["code"])).get("Item") or {}
    if code_item.get("subscriberId"):
        # Using the code proves the address is theirs: a pending sign-up counts as confirmed.
        if m.confirm_subscriber(table, tenant_id, code_item["subscriberId"], via="code_used"):
            _grant_welcome_offer(table, tenant_id, code_item["subscriberId"], order)


def _grant_welcome_offer(table, tenant_id, sid, order):
    """The confirmed sign-up's grant in the consent log, as the email-link
    confirmation writes it (marketing-public). Without it marketing-send skips
    the guest while the lists count them. Never fails the stream record: the
    subscriber row is already updated; the failure is logged without the address."""
    name = consent.table_name()
    if not name:
        return
    try:
        row = table.get_item(Key=m.sub_key(tenant_id, sid)).get("Item") or {}
        if not row.get("email"):
            return
        version = row.get("consentVersion")
        consent.grant(dynamo.table(name), tenant_id=tenant_id, restaurant=row.get("restaurantName"),
                      version=version if version in consent.TEXTS else "welcome_offer_v1",
                      channels=[consent.EMAIL], email=row["email"], location_id=order.get("locationId"),
                      source="welcome_offer", ref=order.get("orderId"))
    except Exception:  # noqa: BLE001
        logger.exception("consent grant failed (welcome offer code used)")


# --- SES events -------------------------------------------------------------------------

def _ses(detail):
    kind = detail.get("eventType") or detail.get("notificationType")
    message_id = (detail.get("mail") or {}).get("messageId")
    if kind not in ("Bounce", "Complaint", "Delivery") or not message_id:
        return
    table = _table()
    ref = table.get_item(Key={"PK": f"MSG#{message_id}", "SK": "MSG"}).get("Item")
    if not ref:
        return
    tenant_id, campaign_id, sid = ref["tenantId"], ref.get("campaignId"), ref["subscriberId"]
    if kind == "Delivery":
        m.bump_campaign(table, tenant_id, campaign_id, delivered=1)
        return
    if kind == "Bounce" and (detail.get("bounce") or {}).get("bounceType") != "Permanent":
        return  # transient: mailbox full, try again next campaign
    status = "bounced" if kind == "Bounce" else "complained"
    try:
        table.update_item(
            Key=m.sub_key(tenant_id, sid),
            UpdateExpression="SET #st = :s, suppressedAt = :now",
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":s": status, ":now": m.now_iso()})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
    m.bump_campaign(table, tenant_id, campaign_id, **{status: 1})
    logger.info(json.dumps({"marketing": status, "tenantId": tenant_id, "campaignId": campaign_id}))
    if kind == "Complaint" and consent.table_name():
        # "This is spam" is a withdrawal: the consent log records it too.
        email = (table.get_item(Key=m.sub_key(tenant_id, sid)).get("Item") or {}).get("email")
        subject_id = ref.get("consentSubject")  # review requests: the guest may have no subscriber row
        if email or subject_id:
            consent.withdraw(dynamo.table(consent.table_name()), tenant_id=tenant_id, channel=consent.EMAIL,
                             identifier=email, subject_id=None if email else subject_id, source="complaint",
                             actor="ses")


def handler(event, context):
    if event.get("source") == "aws.ses" or "detail-type" in event:
        _ses(event.get("detail") or {})
        return {"ok": True}
    failures = []
    reservations = os.environ.get("RESERVATION_TABLE_NAME")
    orders = os.environ.get("ORDER_TABLE_NAME")
    for record in event.get("Records") or []:
        try:
            ddb = record.get("dynamodb") or {}
            new, old = _image(ddb.get("NewImage")), _image(ddb.get("OldImage"))
            if not new:
                continue
            name = _table_name_of(record.get("eventSourceARN"))
            if name == reservations:
                _reservation(new, old)
            elif name == orders:
                _order(new, old)
        except (BotoCoreError, ClientError):
            logger.exception("marketing-sync record failed")
            seq = (record.get("dynamodb") or {}).get("SequenceNumber")
            if seq:
                failures.append({"itemIdentifier": seq})
        except Exception:  # a malformed record must not block the stream
            logger.exception("marketing-sync skipped a record")
    return {"batchItemFailures": failures}


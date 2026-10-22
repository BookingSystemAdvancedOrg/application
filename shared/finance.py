"""Per-restaurant usage counters for the Finance page (Booqy Console).

The finance table (infrastructure storage/dynamodb/finance) keeps one row
per restaurant and day. The senders bump their own counters here with an
atomic ADD; finance-rollup (functions/finance-rollup) fills in requests,
bookings, orders and the allocated AWS cost each night and never touches
these fields.

    from shared import finance
    finance.count(tenant_id, emails=1)       # today (UTC)
    finance.count(tenant_id, location_id=loc, **finance.sms_counters(2, "reminder"))

With a location the same ADD also goes on the location's row
(LOC#<locationId>#DAY#<day> in the restaurant's partition), which is what
the Finance page's location filter reads.

Counting is best effort: a missing FINANCE_TABLE_NAME means "not counted",
and a failed write is logged, never raised - a booking confirmation must
not fail because a statistic could not be written.

ENV_VARS:
    FINANCE_TABLE_NAME -- the finance table. Unset: counting is off.
"""

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal

from botocore.exceptions import BotoCoreError, ClientError

from shared import dynamo

logger = logging.getLogger(__name__)

# sms = messages (the cost split), smsParts = what the invoice charges, by type:
# booking (confirmed, changed, cancelled, fee charged), reminder, order, catering.
COUNTERS = ("emails", "sms", "marketingEmails", "smsParts", "smsBooking", "smsReminder", "smsOrder", "smsCatering")
SMS_TYPES = ("booking", "reminder", "order", "catering")


def sms_counters(part_count, kind):
    """The counters one SMS of `kind` adds: messages, parts and parts by type."""
    kind = kind if kind in SMS_TYPES else "booking"
    return {"sms": 1, "smsParts": part_count, "sms" + kind.capitalize(): part_count}


def day_key(when=None):
    when = when or datetime.now(timezone.utc)
    return when.strftime("%Y-%m-%d")


def tenant_pk(tenant_id):
    return f"TENANT#{tenant_id}"


def day_sk(day):
    return f"DAY#{day}"


def location_sk(location_id, day):
    return f"LOC#{location_id}#DAY#{day}"


def count(tenant_id, *, day=None, location_id=None, **counters):
    """ADDs the given counters (emails=, sms=, marketingEmails=) to the
    restaurant's row for `day` (today by default), and to the location's
    row when `location_id` is given. Returns True when written."""
    name = os.environ.get("FINANCE_TABLE_NAME")
    if not name or not tenant_id:
        return False
    adds = {k: int(v) for k, v in counters.items() if k in COUNTERS and v}
    if not adds:
        return False
    day = day or day_key()
    names = {f"#{k}": k for k in adds}
    values = {f":{k}": Decimal(v) for k, v in adds.items()}
    values[":now"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    keys = [{"PK": tenant_pk(tenant_id), "SK": day_sk(day)}]
    if location_id:
        keys.append({"PK": tenant_pk(tenant_id), "SK": location_sk(location_id, day)})
    try:
        for key in keys:
            dynamo.table(name).update_item(
                Key=key, UpdateExpression="ADD " + ", ".join(f"#{k} :{k}" for k in adds) + " SET countedAt = :now",
                ExpressionAttributeNames=names, ExpressionAttributeValues=values)
        return True
    except (BotoCoreError, ClientError):
        logger.warning("finance counter not written for %s: %s", tenant_id, sorted(adds), exc_info=True)
        return False

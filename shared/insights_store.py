"""Reads and writes for the insights table (shared/insights.py builds and
sums the rows).

Every read is a Query on one location's partition (LOCATION#<id>) or, for
website visits, on the tenant's own TENANT#<t>#STATS partition - the
caller has already checked that the location belongs to the tenant
(shared/tenant.py). A range of days is one paged Query per table, not one
call per day.

ENV_VARS: INSIGHTS_TABLE_NAME, RESERVATION_TABLE_NAME, ORDER_TABLE_NAME,
CATERING_REQUESTS_TABLE_NAME, PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME,
MARKETING_TABLE_NAME (optional: no visits without it).
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

from boto3.dynamodb.conditions import Key

from shared import dynamo
from shared import insights as ins

JOB_SK = "JOB#latest"


def days_between(first, last):
    out, d = [], date.fromisoformat(first)
    end = date.fromisoformat(last)
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _query(table_name, condition, **extra):
    table = dynamo.table(table_name)
    items, kwargs = [], {"KeyConditionExpression": condition, **extra}
    while True:
        page = table.query(**kwargs)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _by_day(items, prefix):
    out = {}
    for item in items:
        day = str(item.get("SK", ""))[len(prefix):len(prefix) + 10]
        out.setdefault(day, []).append(item)
    return out


def load_source(tenant_id, location_id, first, last):
    """Everything build_day needs for the days first..last, in one Query per
    table."""
    pk = Key("PK").eq(f"LOCATION#{location_id}")
    reservations = _query(os.environ["RESERVATION_TABLE_NAME"],
                          pk & Key("SK").between(f"RESERVATION#{first}#", f"RESERVATION#{last}#~"))
    orders = _query(os.environ["ORDER_TABLE_NAME"], pk & Key("SK").between(f"ORDER#{first}#", f"ORDER#{last}#~"))
    card = _query(os.environ["ORDER_TABLE_NAME"], pk & Key("SK").between(f"CARD#{first}#", f"CARD#{last}#~"))
    catering = _query(os.environ["CATERING_REQUESTS_TABLE_NAME"], pk & Key("SK").begins_with("REQUEST#"))
    snapshots = _query(os.environ["PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME"], pk & Key("SK").begins_with("LAYOUT#v"))
    visits = {}
    marketing = os.environ.get("MARKETING_TABLE_NAME")
    if marketing:
        rows = _query(marketing, Key("PK").eq(f"TENANT#{tenant_id}#STATS")
                      & Key("SK").between(f"DAY#{first}#", f"DAY#{last}#~"))
        for item in rows:
            parts = str(item.get("SK", "")).split("#")
            if len(parts) > 2 and parts[2] == location_id:
                visits.setdefault(parts[1], []).append(item)
    return {
        "reservations": _by_day(reservations, "RESERVATION#"),
        "orders": _by_day(orders, "ORDER#"),
        "card": _by_day(card, "CARD#"),
        "catering": catering,
        "snapshots": snapshots,
        "visits": visits,
    }


def build_days(tenant_id, location, first, last, *, built_at=None):
    """Rows for the days first..last, computed from the operational tables."""
    source = load_source(tenant_id, location["locationId"], first, last)
    return [
        ins.build_day(location=location, day=day,
                      reservations=source["reservations"].get(day, ()),
                      orders=source["orders"].get(day, ()),
                      card=source["card"].get(day, ()),
                      catering=source["catering"], visits=source["visits"].get(day, ()),
                      snapshots=source["snapshots"], built_at=built_at)
        for day in days_between(first, last)
    ]


def write_rows(rows):
    table = dynamo.table(os.environ["INSIGHTS_TABLE_NAME"])
    with table.batch_writer(overwrite_by_pkeys=["PK", "SK"]) as batch:
        for row in rows:
            batch.put_item(Item=row)


def read_rows(location_id, first, last):
    return _query(os.environ["INSIGHTS_TABLE_NAME"],
                  Key("PK").eq(f"LOCATION#{location_id}") & Key("SK").between(f"DAY#{first}", f"DAY#{last}"))


def read_job(location_id):
    return dynamo.table(os.environ["INSIGHTS_TABLE_NAME"]).get_item(
        Key={"PK": f"LOCATION#{location_id}", "SK": JOB_SK}).get("Item")


def write_job(location_id, first, last, now=None):
    now = now or datetime.now(timezone.utc)
    previous = read_job(location_id) or {}
    dynamo.table(os.environ["INSIGHTS_TABLE_NAME"]).put_item(Item={
        "PK": f"LOCATION#{location_id}", "SK": JOB_SK,
        "builtAt": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "from": min(first, previous.get("from") or first),
        "to": max(last, previous.get("to") or last),
        "lastRun": {"from": first, "to": last},
    })

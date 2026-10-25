"""insights-rollup

TRIGGER:
    EventBridge Scheduler (insights_rollup_schedule, nightly). Input {} = the
    nightly run. {"from": "yyyy-mm-dd", "to": "yyyy-mm-dd"} (optionally with
    "tenantId") rebuilds a range by hand. The function invokes itself with a
    cursor when it gets close to its timeout ({"after", "round"}, plus the
    range when there was one).

PURPOSE:
    Writes the figures behind Statistik in the admin app: one row per
    restaurant location and day in the insights table (shared/insights.py
    has the row; no personal data).

    For every location of every tenant that is served (not provisioning,
    offboarded or purged):
      * no rows yet (a new location, or the first run after this function
        was deployed): backfills from the location's creation, at most
        BACKFILL_DAYS back (bookings are kept 395 days);
      * otherwise: recomputes the last ROLLUP_DAYS days up to yesterday in
        the location's time zone - no-shows are marked late, fees are
        charged and refunded and catering moves on after the day - and any
        days missed since the last run.
    The data is read with one Query per table for the whole range (in
    chunks of CHUNK_DAYS), rows are overwritten whole, so every run is
    idempotent and a run that stops halfway is finished by the next one.
    Today is never written: GET /insights computes it live.

ENV_VARS:
    ENVIRONMENT, INSIGHTS_TABLE_NAME, TENANT_TABLE_NAME, TENANT_LIST_INDEX_NAME,
    LOCATION_TABLE_NAME, RESERVATION_TABLE_NAME, ORDER_TABLE_NAME,
    CATERING_REQUESTS_TABLE_NAME, PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME,
    MARKETING_TABLE_NAME, ROLLUP_DAYS (8), BACKFILL_DAYS (395)

AWS RESOURCE ACCESS:
    Query/GetItem on the tenant table (GSI1) and the location, reservation,
    order, catering-requests and published-layout-snapshot tables; Query on
    the marketing table's TENANT#<t>#STATS partitions; GetItem/Query/
    PutItem/BatchWriteItem on the insights table; lambda:InvokeFunction on
    itself (continuation).
"""

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from shared import dynamo
from shared import insights as ins
from shared import insights_store as store

logger = logging.getLogger()
logger.setLevel(logging.INFO)

HANDOVER_MS = 120_000
CHUNK_DAYS = 92
MAX_RANGE_DAYS = 400
NOT_SERVED = ("provisioning", "provisioning_failed", "offboarded", "purged")

_lambda = None


def now_utc():
    return datetime.now(timezone.utc)


def _env_int(name, default):
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def list_tenants():
    table = dynamo.table(os.environ["TENANT_TABLE_NAME"])
    items, kwargs = [], {"IndexName": os.environ.get("TENANT_LIST_INDEX_NAME", "GSI1"),
                         "KeyConditionExpression": Key("GSI1PK").eq("TENANT")}
    while True:
        page = table.query(**kwargs)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def list_locations(tenant_id):
    table = dynamo.table(os.environ["LOCATION_TABLE_NAME"])
    items, kwargs = [], {"KeyConditionExpression": Key("PK").eq(f"TENANT#{tenant_id}")
                         & Key("SK").begins_with("LOCATION#")}
    while True:
        page = table.query(**kwargs)
        items.extend(i for i in page.get("Items") or [] if i.get("locationId"))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def yesterday_at(location, now):
    return (now.astimezone(ins._zone(location.get("timezone"))).date() - timedelta(days=1)).isoformat()


def window(location, job, now, event):
    """(first, last) days to build for this location, or None."""
    last = yesterday_at(location, now)
    created = (location.get("createdAt") or "")[:10] or None
    if event.get("from") or event.get("to"):
        first = str(event.get("from") or event.get("to"))[:10]
        last = min(last, str(event.get("to") or event.get("from"))[:10])
        first = max(first, (date.fromisoformat(last) - timedelta(days=MAX_RANGE_DAYS - 1)).isoformat())
    elif not job:
        first = (date.fromisoformat(last) - timedelta(days=_env_int("BACKFILL_DAYS", 395) - 1)).isoformat()
    else:
        first = (date.fromisoformat(last) - timedelta(days=_env_int("ROLLUP_DAYS", 8) - 1)).isoformat()
        done_to = str(job.get("to") or "")[:10]
        if done_to and done_to < first:
            first = (date.fromisoformat(done_to) + timedelta(days=1)).isoformat()
    if created and created > first:
        first = created
    return (first, last) if first <= last else None


def chunks(first, last, size=CHUNK_DAYS):
    d, end = date.fromisoformat(first), date.fromisoformat(last)
    while d <= end:
        stop = min(end, d + timedelta(days=size - 1))
        yield d.isoformat(), stop.isoformat()
        d = stop + timedelta(days=1)


def _remaining_ms(context):
    try:
        return context.get_remaining_time_in_millis()
    except Exception:  # noqa: BLE001 - tests and local runs
        return 10 ** 9


def _continue(context, event, after, round_no):
    global _lambda
    if _lambda is None:
        _lambda = boto3.client("lambda")
    payload = {k: event[k] for k in ("from", "to", "tenantId") if event.get(k)}
    payload.update({"after": after, "round": round_no + 1})
    _lambda.invoke(FunctionName=context.invoked_function_arn, InvocationType="Event",
                   Payload=json.dumps(payload).encode())


def roll_location(tenant_id, location, event, now):
    job = store.read_job(location["locationId"])
    span = window(location, job, now, event)
    if not span:
        return 0
    days = 0
    built_at = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    for first, last in chunks(*span):
        rows = store.build_days(tenant_id, location, first, last, built_at=built_at)
        store.write_rows(rows)
        store.write_job(location["locationId"], first, last, now)
        days += len(rows)
    return days


def handler(event, context):
    event = event if isinstance(event, dict) else {}
    now = now_utc()
    round_no = int(event.get("round") or 0)
    after = event.get("after") or ""
    only = event.get("tenantId")
    done, failed, handed_over = 0, [], None

    tenants = sorted((t for t in list_tenants() if t.get("tenantId") and t.get("status") not in NOT_SERVED),
                     key=lambda t: t["tenantId"])
    for tenant in tenants:
        if only and tenant["tenantId"] != only:
            continue
        for location in sorted(list_locations(tenant["tenantId"]), key=lambda loc: loc["locationId"]):
            key = f"{tenant['tenantId']}|{location['locationId']}"
            if key <= after:
                continue
            if _remaining_ms(context) < HANDOVER_MS:
                handed_over = after
                _continue(context, event, after, round_no)
                break
            try:
                done += roll_location(tenant["tenantId"], location, event, now)
            except (BotoCoreError, ClientError):
                logger.exception("insights-rollup failed for %s", key)
                failed.append(key)
            after = key
        if handed_over is not None:
            break

    summary = {"days": done, "failed": failed, "round": round_no, "handedOverAfter": handed_over}
    logger.info(json.dumps(summary))
    return summary

"""finance-rollup

TRIGGER:
    EventBridge Scheduler (finance_rollup_schedule, nightly after AWS has
    yesterday's cost ready). Input {} = the nightly run. Booqy Console's
    Rebuild invokes it with {"from": "yyyy-mm-dd", "to": "yyyy-mm-dd"} for a
    date range (platform-tenants). The function invokes itself with the
    days it did not reach before its timeout ({"from", "to", "round"}).

PURPOSE:
    Writes the figures behind the Finance page: for every day in the window,
    what the platform cost and how that cost splits between the customers.

      1. Cost Explorer: the account's UnblendedCost per service and day
         (credits and refunds excluded), converted with USD_TO_SEK and
         stored as PLATFORM / COST#<day>. The last COST_DAYS days are
         re-pulled every night because AWS restates recent days.
      2. Usage per customer and day (USAGE_DAYS days back, or the range):
           requests  - one CloudWatch Logs Insights query over the tenant-
                       aware Lambdas' log groups counting the
                       "TENANT_REQUEST tenant=<id>" line shared/tenant.py
                       logs per request;
           bookings  - RESERVATION#<day># rows per location (holds, pending
                       guarantees and expired holds excluded);
           orders    - ORDER#<day># rows per location (unpaid, failed and
                       cancelled excluded);
           emails, sms, marketingEmails - already on the day row, bumped by
                       notification and marketing-send (shared/finance.py).
      3. Allocation: each service's cost goes to a bucket (COST_ALLOCATION:
         baseline, email, sms; everything else = requests). The baseline is
         split equally over the customers active that day, email by emails
         sent, sms by SMS sent and the rest by requests. A bucket nobody
         used that day falls back to an equal split, so cost never
         disappears.
      4. Per customer and day: TENANT#<id> / DAY#<day> gets requests,
         bookings, orders, costSek (+ the split per bucket), mrrSek (the
         subscription that day, from PROFILE.billing), planId and active.
         The counters and the revenue fields other writers own are never
         touched. PLATFORM / DAY#<day> gets the totals and PLATFORM /
         JOB#latest the run (warnings included: e.g. Cost Explorer not
         enabled yet).

    A customer is "active on a day" from its createdAt until the day it was
    offboarded (suspended customers count: the platform still serves them).

    Every step is idempotent and recomputed fields are simply overwritten:
    a run that stops halfway is finished by the next one. One email through
    the alert topic per run with warnings.

ENV_VARS:
    ENVIRONMENT, FINANCE_TABLE_NAME, TENANT_TABLE_NAME, TENANT_LIST_INDEX_NAME,
    LOCATION_TABLE_NAME, RESERVATION_TABLE_NAME, ORDER_TABLE_NAME,
    LAMBDA_LOG_GROUPS (JSON list of log group names), COST_DAYS, USAGE_DAYS,
    USD_TO_SEK, COST_ALLOCATION (JSON {baseline, email, sms}: service lists),
    ALERT_TOPIC_ARN

AWS RESOURCE ACCESS:
    ce:GetCostAndUsage; logs:StartQuery/StopQuery on the listed log groups
    and logs:GetQueryResults; Query/GetItem on the tenant table (GSI1),
    Query on the location, reservation and order tables; GetItem/Query/
    PutItem/UpdateItem on the finance table; sns:Publish on the alert
    topic; lambda:InvokeFunction on itself (continuation).
"""

import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from shared import dynamo, finance

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MAX_RANGE_DAYS = 92
HANDOVER_MS = 90_000            # re-invoke when less than this remains
LOGS_CHUNK = 20                 # log groups per Logs Insights query
LOGS_WAIT_SECONDS = 90
REQUESTS_QUERY = ('parse @message "TENANT_REQUEST tenant=*" as tenant | filter ispresent(tenant) '
                  '| stats count(*) as n by tenant')
NOT_A_BOOKING = ("held", "pending", "expired")
NOT_AN_ORDER = ("pending_payment", "failed", "cancelled")
CENTS = Decimal("0.01")
MICRO = Decimal("0.0001")

_clients = {}


def _client(service, region=None):
    key = (service, region)
    if key not in _clients:
        _clients[key] = boto3.client(service, region_name=region) if region else boto3.client(service)
    return _clients[key]


def now_utc():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _env_int(name, default):
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def _rate():
    try:
        return Decimal(os.environ.get("USD_TO_SEK") or "10.5")
    except ArithmeticError:
        return Decimal("10.5")


def _allocation():
    raw = os.environ.get("COST_ALLOCATION") or "{}"
    try:
        conf = json.loads(raw)
    except ValueError:
        conf = {}
    return {bucket: set(conf.get(bucket) or []) for bucket in ("baseline", "email", "sms")}


def _q(value, places=CENTS):
    return Decimal(value).quantize(places, rounding=ROUND_HALF_UP)


# --- days ---------------------------------------------------------------------------------

def _parse_day(value):
    return date.fromisoformat(str(value)[:10])


def _days(first, last):
    out, d = [], first
    while d <= last:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def window(event, today):
    """The usage days and the cost days this run covers."""
    if event.get("from") or event.get("to"):
        first = _parse_day(event.get("from") or event.get("to"))
        last = _parse_day(event.get("to") or event.get("from"))
        if last > today - timedelta(days=1):
            last = today - timedelta(days=1)
        if first > last:
            first = last
        if (last - first).days >= MAX_RANGE_DAYS:
            first = last - timedelta(days=MAX_RANGE_DAYS - 1)
        days = _days(first, last)
        return days, days
    yesterday = today - timedelta(days=1)
    usage = _days(yesterday - timedelta(days=_env_int("USAGE_DAYS", 3) - 1), yesterday)
    cost = _days(yesterday - timedelta(days=_env_int("COST_DAYS", 7) - 1), yesterday)
    return usage, cost


# --- tenants -------------------------------------------------------------------------------

def list_tenants():
    table = dynamo.table(os.environ["TENANT_TABLE_NAME"])
    items, start = [], None
    while True:
        kwargs = {"IndexName": os.environ.get("TENANT_LIST_INDEX_NAME", "GSI1"),
                  "KeyConditionExpression": Key("GSI1PK").eq("TENANT")}
        if start:
            kwargs["ExclusiveStartKey"] = start
        page = table.query(**kwargs)
        items.extend(page.get("Items", []))
        start = page.get("LastEvaluatedKey")
        if not start:
            return items


def active_on(tenant, day):
    created = (tenant.get("createdAt") or "")[:10]
    if created and created > day:
        return False
    status = tenant.get("status")
    if status in ("provisioning", "provisioning_failed"):
        return False
    if status in ("offboarding", "offboarded", "purged"):
        gone = (tenant.get("offboardedAt") or tenant.get("purgedAt") or tenant.get("updatedAt") or "")[:10]
        return bool(gone) and day < gone
    return True


def mrr_on(tenant):
    billing = tenant.get("billing") if isinstance(tenant.get("billing"), dict) else {}
    if billing.get("status") in ("active", "past_due") and not billing.get("pauseCollection"):
        try:
            return _q(billing.get("mrrSek") or 0)
        except ArithmeticError:
            return Decimal(0)
    return Decimal(0)


def list_locations(tenant_id):
    table = dynamo.table(os.environ["LOCATION_TABLE_NAME"])
    out, start = [], None
    while True:
        kwargs = {"KeyConditionExpression": Key("PK").eq(f"TENANT#{tenant_id}") & Key("SK").begins_with("LOCATION#"),
                  "ProjectionExpression": "locationId"}
        if start:
            kwargs["ExclusiveStartKey"] = start
        page = table.query(**kwargs)
        out.extend(i["locationId"] for i in page.get("Items", []) if i.get("locationId"))
        start = page.get("LastEvaluatedKey")
        if not start:
            return out


def _count_rows(table_name, location_id, prefix, excluded):
    table = dynamo.table(table_name)
    n, start = 0, None
    while True:
        kwargs = {"KeyConditionExpression": Key("PK").eq(f"LOCATION#{location_id}") & Key("SK").begins_with(prefix),
                  "ProjectionExpression": "#s", "ExpressionAttributeNames": {"#s": "status"}}
        if start:
            kwargs["ExclusiveStartKey"] = start
        page = table.query(**kwargs)
        n += sum(1 for i in page.get("Items", []) if i.get("status") not in excluded)
        start = page.get("LastEvaluatedKey")
        if not start:
            return n


def count_bookings_and_orders(location_ids, day):
    bookings = orders = 0
    for location_id in location_ids:
        bookings += _count_rows(os.environ["RESERVATION_TABLE_NAME"], location_id, f"RESERVATION#{day}#", NOT_A_BOOKING)
        orders += _count_rows(os.environ["ORDER_TABLE_NAME"], location_id, f"ORDER#{day}#", NOT_AN_ORDER)
    return bookings, orders


# --- requests (Logs Insights) -----------------------------------------------------------

def _log_groups():
    try:
        groups = json.loads(os.environ.get("LAMBDA_LOG_GROUPS") or "[]")
    except ValueError:
        groups = []
    return [g for g in groups if isinstance(g, str) and g]


def _run_query(groups, start, end, warnings):
    logs = _client("logs")
    try:
        query_id = logs.start_query(logGroupNames=groups, startTime=start, endTime=end,
                                    queryString=REQUESTS_QUERY, limit=10000)["queryId"]
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ResourceNotFoundException" and len(groups) > 1:
            # A function that was never invoked has no log group yet: query
            # the rest one by one and skip the missing ones.
            counts = {}
            for group in groups:
                for tenant_id, n in _run_query([group], start, end, warnings).items():
                    counts[tenant_id] = counts.get(tenant_id, 0) + n
            return counts
        if code == "ResourceNotFoundException":
            return {}
        raise
    deadline = time.monotonic() + LOGS_WAIT_SECONDS
    while True:
        result = logs.get_query_results(queryId=query_id)
        status = result.get("status")
        if status == "Complete":
            break
        if status in ("Failed", "Cancelled", "Timeout", "Unknown"):
            warnings.append(f"request count query {status.lower()} for {len(groups)} log groups")
            return {}
        if time.monotonic() > deadline:
            try:
                logs.stop_query(queryId=query_id)
            except ClientError:
                pass
            warnings.append(f"request count query took too long for {len(groups)} log groups")
            return {}
        time.sleep(1)
    counts = {}
    for row in result.get("results", []):
        fields = {f.get("field"): f.get("value") for f in row}
        tenant_id, n = fields.get("tenant"), fields.get("n")
        if tenant_id and n:
            try:
                counts[tenant_id] = counts.get(tenant_id, 0) + int(float(n))
            except ValueError:
                continue
    return counts


def count_requests(day, warnings):
    groups = _log_groups()
    if not groups:
        return {}
    start = int(datetime.combine(_parse_day(day), datetime.min.time(), tzinfo=timezone.utc).timestamp())
    end = start + 86_400
    counts = {}
    for i in range(0, len(groups), LOGS_CHUNK):
        try:
            part = _run_query(groups[i:i + LOGS_CHUNK], start, end, warnings)
        except (BotoCoreError, ClientError) as exc:
            warnings.append(f"request count failed for {day}: {exc}")
            return counts
        for tenant_id, n in part.items():
            counts[tenant_id] = counts.get(tenant_id, 0) + n
    return counts


# --- cost (Cost Explorer) --------------------------------------------------------------

def fetch_cost(days, warnings):
    """{day: {service: usd}} for the days, from Cost Explorer (us-east-1)."""
    if not days:
        return {}
    start, end = days[0], (_parse_day(days[-1]) + timedelta(days=1)).isoformat()
    ce = _client("ce", "us-east-1")
    out = {d: {} for d in days}
    token = None
    try:
        while True:
            kwargs = {"TimePeriod": {"Start": start, "End": end}, "Granularity": "DAILY",
                      "Metrics": ["UnblendedCost"],
                      "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
                      "Filter": {"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit", "Refund"]}}}}
            if token:
                kwargs["NextPageToken"] = token
            page = ce.get_cost_and_usage(**kwargs)
            for result in page.get("ResultsByTime", []):
                day = (result.get("TimePeriod") or {}).get("Start")
                if day not in out:
                    continue
                for group in result.get("Groups", []):
                    service = (group.get("Keys") or ["?"])[0]
                    amount = ((group.get("Metrics") or {}).get("UnblendedCost") or {}).get("Amount") or "0"
                    out[day][service] = out[day].get(service, Decimal(0)) + Decimal(amount)
            token = page.get("NextPageToken")
            if not token:
                break
    except (BotoCoreError, ClientError) as exc:
        warnings.append(f"Cost Explorer: {exc}")
        return None
    return out


def store_cost(day, by_service, rate):
    table = dynamo.table(os.environ["FINANCE_TABLE_NAME"])
    usd = sum(by_service.values(), Decimal(0))
    table.put_item(Item={
        "PK": "PLATFORM", "SK": f"COST#{day}", "day": day,
        "usd": _q(usd, MICRO), "sek": _q(usd * rate), "usdToSek": rate,
        "byServiceUsd": {s: _q(v, MICRO) for s, v in by_service.items() if v},
        "updatedAt": _iso(now_utc()),
    })


def stored_cost(day):
    item = dynamo.table(os.environ["FINANCE_TABLE_NAME"]).get_item(
        Key={"PK": "PLATFORM", "SK": f"COST#{day}"}).get("Item")
    if not item:
        return None, None
    return {s: Decimal(v) for s, v in (item.get("byServiceUsd") or {}).items()}, Decimal(item.get("usdToSek") or 0)


# --- allocation -------------------------------------------------------------------------

def bucket_totals(by_service_usd, rate, allocation):
    totals = {"baseline": Decimal(0), "requests": Decimal(0), "email": Decimal(0), "sms": Decimal(0)}
    for service, usd in (by_service_usd or {}).items():
        bucket = "requests"
        for name in ("baseline", "email", "sms"):
            if service in allocation[name]:
                bucket = name
                break
        totals[bucket] += Decimal(usd) * rate
    return totals


def allocate(totals, usage):
    """usage: {tenant_id: {"requests", "emails", "sms"}} of the customers
    active that day -> {tenant_id: {bucket: sek, "total": sek}}."""
    tenants = list(usage)
    out = {t: {} for t in tenants}
    if not tenants:
        return out
    weights = {"baseline": {t: Decimal(1) for t in tenants},
               "requests": {t: Decimal(usage[t].get("requests") or 0) for t in tenants},
               "email": {t: Decimal(usage[t].get("emails") or 0) for t in tenants},
               "sms": {t: Decimal(usage[t].get("sms") or 0) for t in tenants}}
    for bucket, total in totals.items():
        w = weights[bucket]
        denominator = sum(w.values(), Decimal(0))
        if denominator <= 0:
            w = weights["baseline"]
            denominator = Decimal(len(tenants))
        for t in tenants:
            out[t][bucket] = _q(total * w[t] / denominator, MICRO)
    for t in tenants:
        out[t]["total"] = _q(sum((out[t][b] for b in totals), Decimal(0)))
    return out


# --- rows --------------------------------------------------------------------------------

def _day_rows(tenant_ids, day):
    """The existing DAY rows (counters) of the tenants, by tenant id."""
    table = dynamo.table(os.environ["FINANCE_TABLE_NAME"])
    rows = {}
    for tenant_id in tenant_ids:
        item = table.get_item(Key={"PK": finance.tenant_pk(tenant_id), "SK": finance.day_sk(day)}).get("Item")
        rows[tenant_id] = item or {}
    return rows


def write_tenant_day(tenant, day, usage, cost, mrr, active):
    table = dynamo.table(os.environ["FINANCE_TABLE_NAME"])
    table.update_item(
        Key={"PK": finance.tenant_pk(tenant["tenantId"]), "SK": finance.day_sk(day)},
        UpdateExpression="SET #r = :r, bookings = :b, orders = :o, costSek = :c, costSplitSek = :s, "
                         "mrrSek = :m, planId = :p, active = :a, computedAt = :now, #d = :d",
        ExpressionAttributeNames={"#r": "requests", "#d": "day"},
        ExpressionAttributeValues={
            ":r": Decimal(usage.get("requests") or 0), ":b": Decimal(usage.get("bookings") or 0),
            ":o": Decimal(usage.get("orders") or 0),
            ":c": cost.get("total", Decimal(0)),
            ":s": {k: v for k, v in cost.items() if k != "total"},
            ":m": mrr, ":p": tenant.get("planId") or "-", ":a": active, ":now": _iso(now_utc()), ":d": day})


def write_platform_day(day, totals):
    dynamo.table(os.environ["FINANCE_TABLE_NAME"]).put_item(Item={
        "PK": "PLATFORM", "SK": finance.day_sk(day), "day": day, **totals, "computedAt": _iso(now_utc())})


def write_job(started, days, warnings, done):
    table = dynamo.table(os.environ["FINANCE_TABLE_NAME"])
    item = {"startedAt": _iso(started), "finishedAt": _iso(now_utc()), "days": days,
            "warnings": warnings[:20], "done": done}
    table.put_item(Item={"PK": "PLATFORM", "SK": "JOB#latest", **item})
    table.put_item(Item={"PK": "PLATFORM", "SK": f"JOB#{_iso(started)}", **item})


# --- one day --------------------------------------------------------------------------------

def process_day(day, tenants, cost_by_service, rate, allocation, warnings):
    active = [t for t in tenants if active_on(t, day)]
    ids = [t["tenantId"] for t in active]
    requests = count_requests(day, warnings)
    rows = _day_rows(ids, day)
    usage = {}
    for t in active:
        tenant_id = t["tenantId"]
        try:
            bookings, orders = count_bookings_and_orders(list_locations(tenant_id), day)
        except (BotoCoreError, ClientError) as exc:
            warnings.append(f"bookings/orders for {tenant_id} on {day}: {exc}")
            bookings, orders = 0, 0
        row = rows.get(tenant_id) or {}
        usage[tenant_id] = {
            "requests": int(requests.get(tenant_id, 0)), "bookings": bookings, "orders": orders,
            "emails": int(row.get("emails") or 0), "sms": int(row.get("sms") or 0),
            "marketingEmails": int(row.get("marketingEmails") or 0),
        }
    totals = bucket_totals(cost_by_service, rate, allocation)
    split = allocate(totals, usage)
    platform = {"activeTenants": len(active),
                "newTenants": sum(1 for t in tenants if (t.get("createdAt") or "")[:10] == day),
                "churnedTenants": sum(1 for t in tenants if (t.get("offboardedAt") or "")[:10] == day),
                "mrrSek": Decimal(0), "costSek": _q(sum(totals.values(), Decimal(0))),
                "costSplitSek": {k: _q(v) for k, v in totals.items()},
                "unallocatedSek": Decimal(0),
                "requests": 0, "bookings": 0, "orders": 0, "emails": 0, "sms": 0, "marketingEmails": 0,
                "invoicedSek": Decimal(0), "paidSek": Decimal(0), "creditedSek": Decimal(0)}
    if not active:
        platform["unallocatedSek"] = platform["costSek"]
    for t in active:
        tenant_id = t["tenantId"]
        mrr = mrr_on(t)
        write_tenant_day(t, day, usage[tenant_id], split[tenant_id], mrr, True)
        platform["mrrSek"] += mrr
        for k in ("requests", "bookings", "orders", "emails", "sms", "marketingEmails"):
            platform[k] += usage[tenant_id][k]
        row = rows.get(tenant_id) or {}
        for k in ("invoicedSek", "paidSek", "creditedSek"):
            platform[k] += Decimal(row.get(k) or 0)
    write_platform_day(day, platform)
    return platform


# --- entry point ----------------------------------------------------------------------------

def _remaining_ms(context):
    try:
        return context.get_remaining_time_in_millis()
    except Exception:  # noqa: BLE001 - tests and local runs
        return 10 ** 9


def _continue(context, days, round_no):
    _client("lambda").invoke(
        FunctionName=context.invoked_function_arn, InvocationType="Event",
        Payload=json.dumps({"from": days[0], "to": days[-1], "round": round_no + 1}).encode())


def _notify(subject, lines):
    topic = os.environ.get("ALERT_TOPIC_ARN")
    if not topic:
        return
    try:
        _client("sns").publish(TopicArn=topic, Subject=subject[:100], Message="\n".join(lines))
    except (BotoCoreError, ClientError):
        logger.warning("could not publish the finance-rollup summary", exc_info=True)


def handler(event, context):
    event = event if isinstance(event, dict) else {}
    started = now_utc()
    usage_days, cost_days = window(event, started.date())
    round_no = int(event.get("round") or 0)
    warnings, done = [], []
    rate, allocation = _rate(), _allocation()

    cost = fetch_cost(cost_days, warnings)
    if cost is not None:
        for day in cost_days:
            store_cost(day, cost[day], rate)

    tenants = list_tenants()
    handed_over = None
    for i, day in enumerate(usage_days):
        if i and _remaining_ms(context) < HANDOVER_MS:
            handed_over = usage_days[i:]
            _continue(context, handed_over, round_no)
            warnings.append(f"continued in a new run for {handed_over[0]}..{handed_over[-1]}")
            break
        if cost is not None and day in cost:
            by_service, day_rate = cost[day], rate
        else:
            by_service, day_rate = stored_cost(day)
            if by_service is None:
                warnings.append(f"no cost known for {day} (Cost Explorer unavailable); cost set to 0")
                by_service, day_rate = {}, rate
        try:
            process_day(day, tenants, by_service, day_rate, allocation, warnings)
            done.append(day)
        except (BotoCoreError, ClientError) as exc:
            warnings.append(f"{day}: {exc}")
            logger.exception("finance-rollup failed on %s", day)

    write_job(started, usage_days, warnings, done)
    summary = {"days": done, "warnings": warnings, "round": round_no, "handedOver": handed_over}
    logger.info(json.dumps(summary, default=str))
    if warnings and not handed_over:
        _notify(f"[{os.environ.get('ENVIRONMENT', 'dev')}] finance-rollup: {len(warnings)} warning(s)",
                [f"Days done: {', '.join(done) or '-'}", ""] + [f"- {w}" for w in warnings])
    return summary

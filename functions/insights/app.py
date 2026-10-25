"""insights

TRIGGER:
    API Gateway -- GET /insights -- Auth: JWT (tenant pool)

    Query: from, to (yyyy-mm-dd, at most MAX_DAYS days), locationId (one of
    the tenant's locations, or "all"), service (all | lunch | dinner),
    channel (all | online | phone | onsite: booking figures only), compare
    (previous | year | none).

PURPOSE:
    Statistik in the admin app: the restaurant's own figures for a period
    and the period it is compared with (shared/insights.py: summarize).
    Rolled-up days come from the insights table (insights-rollup, nightly);
    the days after the last rollup (today, and yesterday before the nightly
    run) are computed live from the operational tables with the same code.
    A location that has not been rolled up yet gets its last LIVE_DAYS days
    live and the answer says pending=true.

    Tenant isolation: the tenant comes from the token (shared/tenant.py),
    locationId is checked against it, "all" is the tenant's own locations.
    Owners see everything. Staff see their assigned location only, and
    only when the owner allows it (PROFILE.insights.staffAccess:
    "operations" by default, "none" = 403); every amount is removed for
    staff on the server.

ENV_VARS:
    ENVIRONMENT, INSIGHTS_TABLE_NAME, TENANT_TABLE_NAME, LOCATION_TABLE_NAME,
    LOCATION_ID_INDEX_NAME, USER_TABLE_NAME, RESERVATION_TABLE_NAME,
    ORDER_TABLE_NAME, CATERING_REQUESTS_TABLE_NAME,
    PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME, MARKETING_TABLE_NAME

AWS RESOURCE ACCESS:
    Tenant-context read policy; Query/GetItem on the insights table; Query
    on the location table (the tenant's own partition); Query/GetItem on the
    reservation, order, catering-requests and published-layout-snapshot
    tables; Query on the marketing table's STATS partitions; GetItem on
    the user table (a staff caller's location).
"""

import logging
import re
from datetime import date, datetime, timedelta, timezone

from botocore.exceptions import BotoCoreError, ClientError

from shared import http, tenant
from shared import insights as ins
from shared import insights_store as store

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MAX_DAYS = 731
LIVE_DAYS = 31
SERVICES = ("all", "lunch", "dinner")
CHANNELS = ("all", "online", "phone", "onsite")
COMPARES = ("previous", "year", "none")
STAFF_ACCESS = ("operations", "none")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def now_utc():
    return datetime.now(timezone.utc)


def staff_access(tenant_row):
    raw = ((tenant_row or {}).get("insights") or {}).get("staffAccess")
    return raw if raw in STAFF_ACCESS else "operations"


def _day(value, name):
    if not isinstance(value, str) or not _DAY.fullmatch(value):
        raise ValueError(f"{name} must be yyyy-mm-dd")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be yyyy-mm-dd") from None
    return value


def _choice(params, name, options):
    value = params.get(name) or options[0]
    if value not in options:
        raise ValueError(f"{name} must be one of {', '.join(options)}")
    return value


def _year_back(iso):
    d = date.fromisoformat(iso)
    try:
        return d.replace(year=d.year - 1).isoformat()
    except ValueError:  # 29 February
        return d.replace(year=d.year - 1, day=28).isoformat()


def compare_range(first, last, mode):
    if mode == "none":
        return None
    if mode == "year":
        return _year_back(first), _year_back(last)
    n = (date.fromisoformat(last) - date.fromisoformat(first)).days + 1
    return ((date.fromisoformat(first) - timedelta(days=n)).isoformat(),
            (date.fromisoformat(first) - timedelta(days=1)).isoformat())


def _today(location, now):
    return now.astimezone(ins._zone(location.get("timezone"))).date().isoformat()


def rows_for(tenant_id, location, first, last, now):
    """Stored rows plus the live days after the last rollup. Returns
    (rows, pending, builtAt)."""
    loc_id = location["locationId"]
    today = _today(location, now)
    last = min(last, today)
    if first > last:
        return [], False, None
    job = store.read_job(loc_id)
    if job and job.get("to"):
        live_first = (date.fromisoformat(str(job["to"])[:10]) + timedelta(days=1)).isoformat()
        pending = False
    else:
        live_first = (date.fromisoformat(today) - timedelta(days=LIVE_DAYS - 1)).isoformat()
        pending = True
    rows = {r["SK"]: r for r in store.read_rows(loc_id, first, last)} if job else {}
    live_first = max(first, live_first)
    if live_first <= last:
        for row in store.build_days(tenant_id, location, live_first, last):
            rows[row["SK"]] = row
    return list(rows.values()), pending, (job or {}).get("builtAt")


def _summaries(ctx, locations, first, last, compare, service, channel, now):
    splits = {loc["locationId"]: ins.lunch_until(loc) for loc in locations}
    current, before, pending, built = [], [], False, []
    for loc in locations:
        rows, waiting, built_at = rows_for(ctx.tenant_id, loc, first, last, now)
        current.extend(rows)
        pending = pending or waiting
        if built_at:
            built.append(built_at)
        if compare:
            rows, _, _ = rows_for(ctx.tenant_id, loc, compare[0], compare[1], now)
            before.extend(rows)
    previous = ins.summarize(before, service=service, channel=channel, splits=splits) if compare else None
    summary = ins.summarize(current, service=service, channel=channel, splits=splits,
                            compare_dishes=ins.dish_index(previous) if previous else None)
    return summary, previous, pending, (min(built) if built else None)


def get_insights(event):
    params = http.query(event)
    location_param = params.get("locationId") or "all"
    if location_param != "all" and (len(location_param) > 128 or "#" in location_param):
        return http.error(400, "locationId is invalid")
    ctx = tenant.for_jwt(event, location_id=None if location_param == "all" else location_param)

    money = ctx.is_owner
    if not ctx.is_owner:
        if staff_access(ctx.tenant) == "none":
            return http.error(403, "staff_no_access")
        if ctx.location is None:
            assigned = tenant.staff_location(ctx.sub, ctx.tenant_id)
            location = tenant.get_location(ctx.tenant_id, assigned) if assigned else None
            if not location:
                return http.error(403, "no_location")
            locations = [location]
        else:
            locations = [ctx.location]
    else:
        locations = [ctx.location] if ctx.location else tenant.list_locations(ctx.tenant_id)

    try:
        first = _day(params.get("from"), "from")
        last = _day(params.get("to"), "to")
        if first > last:
            raise ValueError("from must not be after to")
        if (date.fromisoformat(last) - date.fromisoformat(first)).days + 1 > MAX_DAYS:
            raise ValueError(f"the period is at most {MAX_DAYS} days")
        service = _choice(params, "service", SERVICES)
        channel = _choice(params, "channel", CHANNELS)
        compare_mode = _choice(params, "compare", COMPARES)
    except ValueError as exc:
        return http.respond(400, {"error": str(exc)})

    compare = compare_range(first, last, compare_mode)
    now = now_utc()
    summary, previous, pending, built_at = _summaries(ctx, locations, first, last, compare, service, channel, now)
    if not money:
        summary = ins.without_money(summary)
        previous = ins.without_money(previous) if previous else None
    body = {
        "range": {"from": first, "to": last},
        "compare": {"mode": compare_mode, "from": compare[0], "to": compare[1]} if compare else {"mode": "none"},
        "filters": {"locationId": location_param, "service": service, "channel": channel},
        "locations": [{"locationId": loc["locationId"], "name": loc.get("name"),
                       "lunchUntil": f"{ins.lunch_until(loc):02d}:00"} for loc in locations],
        "money": money,
        "pending": pending,
        "builtAt": built_at,
        "current": summary,
        "previous": previous,
    }
    return http.respond(200, ins.plain(body))


def handler(event, context):
    method, path = http.route(event)
    if path != "/insights":
        return http.error(404, "not found")
    if method != "GET":
        return http.respond(405, {"error": "method not allowed"}, headers={"Allow": "GET"})
    try:
        return get_insights(event)
    except tenant.TenantError as exc:
        return exc.response()
    except (BotoCoreError, ClientError):
        logger.exception("insights failed")
        return http.error(503, "service_unavailable")
    except Exception:  # noqa: BLE001 - never leak a stack trace
        logger.exception("insights failed")
        return http.error(500, "internal_error")

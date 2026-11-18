"""tenant-setup

TRIGGER:
    API Gateway -- Auth: JWT (tenant user pool), owner only
    GET /tenant/setup               the Kom igång checklist, all locations
    PUT /tenant/setup/skips         {step, locationId?, skipped: bool}
    EventBridge Scheduler           {"setupTask": "sweep"} every 15 minutes

PURPOSE:
    Kom igång has one rule, decided here and nowhere else
    (shared/setup_checklist.py, docs/SETUP-CHECKLIST.md): every applicable
    step done or skipped, across all of the tenant's locations. Booqy Admin
    shows the list this returns; Booqy Console reads what is recorded on
    the tenant.

    Every evaluation (an owner opening Översikt, a skip, the sweep) stores
    PROFILE.setupState when it changed: counts, and each step's state per
    location. The first time the rule is true, and only then, one
    transaction sets PROFILE.setupCompletedAt (condition: not set yet),
    appends an AUDIT# row (setup_completed) and a NOTICES row for Booqy
    Console's bell. After that commit: Tenant.SetupCompleted on the event
    bus and one email to the operators (platform alerts topic). A step
    reopened later only changes setupState; there is never a second notice.

    The sweep keeps the console's progress current for restaurants that
    finish a step and never open Översikt again (active tenants only).

DATA:
    Tenant table (TENANT_TABLE_NAME):
        TENANT#<t> / PROFILE                     setupState, setupCompletedAt
        TENANT#<t> / SETUPSKIP#<step>[@<loc>]    step, locationId, at, by
        TENANT#<t> / AUDIT#<at>#<id>             setup_completed, setup_step_skipped, setup_step_unskipped
        NOTICES   / <at>#<t>#setup_completed     kind, tenantId, tenantName, at
    Read only: location, menu, print, published-layout-snapshot, user,
    google-business and phone-agent tables (keys in _load_facts).

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    USER_TABLE_NAME, USER_TENANT_INDEX_NAME, MENU_TABLE_NAME, PRINT_TABLE_NAME,
    PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME, GOOGLE_BUSINESS_TABLE_NAME (optional),
    PHONE_AGENT_TABLE_NAME (optional), EVENT_BUS_NAME, ALERT_TOPIC_ARN
    (optional: no email), CONSOLE_APP_URL (link in the email)

AWS RESOURCE ACCESS:
    Tenant table GetItem/Query (+ GSI1 for the sweep), UpdateItem/PutItem/
    DeleteItem/TransactWriteItems on the rows above; Query/GetItem on the
    read-only tables; events:PutEvents (source booqy.tenant-setup);
    sns:Publish on the alerts topic (infrastructure tenant-setup/).
"""

import json
import logging
import os
import secrets
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import BotoCoreError, ClientError

from shared import dpa, events, http, tenant
from shared import setup_checklist as rule
from shared.dynamo import client as ddb_client
from shared.dynamo import table

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SOURCE = "tenant-setup"
NOTICES_PK = "NOTICES"
NOTICE_KIND = "setup_completed"
SKIP_PREFIX = "SETUPSKIP#"
STAFF_ROLES = ("staff", "staff_user")

_sns = None


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _env(name):
    return os.environ.get(name) or None


def _query_all(tbl, **kwargs):
    items = []
    while True:
        page = tbl.query(**kwargs)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _count(tbl, **kwargs):
    n = 0
    while True:
        page = tbl.query(Select="COUNT", **kwargs)
        n += int(page.get("Count") or 0)
        if "LastEvaluatedKey" not in page:
            return n
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _guard(fn, default=None):
    """One fact that can't be read is unknown (None), never a crash: the step
    then counts as open."""
    try:
        return fn()
    except (BotoCoreError, ClientError, KeyError, TypeError, ValueError) as exc:
        logger.warning(json.dumps({"setup": "fact_unreadable", "error": type(exc).__name__}))
        return default


# --- facts ---------------------------------------------------------------------------------

def _has_hours(row):
    hours = row.get("businessHours") or {}
    return any(len(v or []) > 0 for v in hours.values()) if isinstance(hours, dict) else False


def _menu_items(location_id):
    tbl = table(os.environ["MENU_TABLE_NAME"])
    return _count(tbl, KeyConditionExpression=Key("PK").eq(f"LOCATION#{location_id}") & Key("SK").begins_with("MENU#"),
                  FilterExpression=Attr("active").eq(True))


def _printers(location_id):
    tbl = table(os.environ["PRINT_TABLE_NAME"])
    pk = f"LOCATION#{location_id}"
    count = _count(tbl, KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with("DEVICE#"))
    settings = tbl.get_item(Key={"PK": pk, "SK": "SETTINGS"}).get("Item") or {}
    return count, settings.get("printerSetup") == "skipped"


def _live_layout(location_id):
    tbl = table(os.environ["PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME"])
    row = tbl.get_item(Key={"PK": f"LOCATION#{location_id}", "SK": "LAYOUT#ACTIVATION"}).get("Item") or {}
    try:
        return int(row.get("currentVersion") or 0) > 0
    except (TypeError, ValueError):
        return False


def _staff_count(tenant_id):
    tbl = table(os.environ["USER_TABLE_NAME"])
    rows = _query_all(tbl, IndexName=os.environ.get("USER_TENANT_INDEX_NAME", "byTenant"),
                      KeyConditionExpression=Key("tenantId").eq(tenant_id))
    return sum(1 for r in rows if r.get("role") in STAFF_ROLES and r.get("status") != "system"
               and not str(r.get("PK", "")).startswith("USER#agent:"))


def _google(tenant_id, location_id):
    tbl = table(os.environ["GOOGLE_BUSINESS_TABLE_NAME"])
    pk = f"LOCATION#{location_id}"
    link = tbl.get_item(Key={"PK": pk, "SK": "LINK"}).get("Item") or {}
    skip = tbl.get_item(Key={"PK": pk, "SK": "SKIP"}).get("Item") or {}
    return link.get("tenantId") == tenant_id, skip.get("tenantId") == tenant_id


def _phone_hours(row):
    if (row.get("mode") or "after_hours") == "always":
        return True
    hours = row.get("agentHours") or {}
    return isinstance(hours, dict) and any(len(v or []) > 0 for v in hours.values())


def _phone_rows(tenant_id):
    tbl = table(os.environ["PHONE_AGENT_TABLE_NAME"])
    config = tbl.get_item(Key={"PK": f"TENANT#{tenant_id}", "SK": "CONFIG"}).get("Item") or {}
    rows = _query_all(tbl, KeyConditionExpression=Key("PK").eq(f"TENANT#{tenant_id}") & Key("SK").begins_with("LOC#"))
    by_location = {}
    for r in rows:
        sk = str(r.get("SK", ""))
        if sk.endswith("#CONFIG"):
            by_location[sk[len("LOC#"):-len("#CONFIG")]] = r
    return int(config.get("version") or 0) > 0, by_location


def _phone_progress(row):
    setup = row.get("setup") or {}
    number = (row.get("number") or {}).get("e164") if isinstance(row.get("number"), dict) else None
    return {"tider": _phone_hours(row), "nummer": bool(row.get("enabled") and number),
            "koppla": bool(setup.get("forwardedAt")), "prova": bool(setup.get("testedAt"))}


def _skips(tenant_id):
    tbl = table(os.environ["TENANT_TABLE_NAME"])
    rows = _query_all(tbl, KeyConditionExpression=Key("PK").eq(tenant.tenant_pk(tenant_id))
                      & Key("SK").begins_with(SKIP_PREFIX), ConsistentRead=True)
    return {str(r["SK"])[len(SKIP_PREFIX):] for r in rows}


def load_facts(tenant_id, row):
    features = ((row.get("entitlements") or {}).get("features") or {})
    stripe = row.get("stripe") or {}
    f = rule.Facts(
        features=dict(features),
        dpa_up_to_date=bool(dpa.status(row)["upToDate"]),
        stripe_ready=bool(stripe.get("accountId") and stripe.get("chargesEnabled")),
        staff_count=_guard(lambda: _staff_count(tenant_id)),
        skips=_guard(lambda: _skips(tenant_id), set()),
    )
    phone_named, phone_by_location = None, {}
    if features.get("phoneAgent") and _env("PHONE_AGENT_TABLE_NAME"):
        phone_named, phone_by_location = _guard(lambda: _phone_rows(tenant_id), (None, None)) or (None, None)
    f.phone_named = phone_named
    google_on = bool(features.get("googleBusiness") and _env("GOOGLE_BUSINESS_TABLE_NAME"))
    if google_on:
        connection = _guard(lambda: table(os.environ["GOOGLE_BUSINESS_TABLE_NAME"]).get_item(
            Key={"PK": f"TENANT#{tenant_id}", "SK": "CONNECTION"}).get("Item") or {})
        f.google_connected = None if connection is None else connection.get("status") == "connected"
    for loc in sorted(tenant.list_locations(tenant_id), key=lambda r: (str(r.get("name") or ""), r["locationId"])):
        lid = loc["locationId"]
        lf = rule.LocationFacts(location_id=lid, name=str(loc.get("name") or ""), has_hours=_has_hours(loc),
                                active_menu_items=_guard(lambda: _menu_items(lid)),
                                live_layout=_guard(lambda: _live_layout(lid)))
        if features.get("ordering"):
            printers = _guard(lambda: _printers(lid))
            if printers is not None:
                lf.printers, lf.printers_skipped = printers
        if google_on:
            google = _guard(lambda: _google(tenant_id, lid))
            if google is not None:
                lf.google_linked, lf.google_skipped = google
        if phone_by_location is not None and features.get("phoneAgent"):
            lf.phone = _phone_progress(phone_by_location.get(lid) or {})
        f.locations.append(lf)
    return f


# --- recording -----------------------------------------------------------------------------

def stored_state(result):
    """What PROFILE.setupState keeps: counts and each step's state per
    location (console list and customer page). No Swedish labels, no sub-steps."""
    return {
        "complete": result["complete"], "total": result["total"], "done": result["done"],
        "skipped": result["skipped"], "open": result["open"],
        "steps": [{"id": s["id"], "labelEn": s["labelEn"], "scope": s["scope"], "state": s["state"],
                   **({"locations": [{"locationId": p["locationId"], "name": p["name"], "state": p["state"]}
                                     for p in s["locations"]]} if "locations" in s else {})}
                  for s in result["steps"]],
    }


def _plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == int(value) else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _same(a, b):
    strip = (lambda s: {k: v for k, v in (s or {}).items() if k != "evaluatedAt"})
    return json.dumps(_plain(strip(a)), sort_keys=True) == json.dumps(_plain(strip(b)), sort_keys=True)


def _serialize(item):
    from boto3.dynamodb.types import TypeSerializer
    ser = TypeSerializer()
    return {k: ser.serialize(v) for k, v in item.items() if v is not None}


def record(tenant_id, row, result, actor):
    """Stores setupState when it changed; the first complete result also
    sets setupCompletedAt, the audit row and the notice, once. Returns
    setupCompletedAt (existing or new) or None."""
    state = stored_state(result)
    completed_at = row.get("setupCompletedAt")
    tbl_name = os.environ["TENANT_TABLE_NAME"]
    key = {"PK": tenant.tenant_pk(tenant_id), "SK": "PROFILE"}
    if result["complete"] and not completed_at:
        at = _now()
        stamped = {**state, "evaluatedAt": at}
        try:
            ddb_client().transact_write_items(TransactItems=[
                {"Update": {
                    "TableName": tbl_name, "Key": _serialize(key),
                    "UpdateExpression": "SET setupCompletedAt = :at, setupState = :s",
                    "ConditionExpression": "attribute_exists(PK) AND attribute_not_exists(setupCompletedAt)",
                    "ExpressionAttributeValues": _serialize({":at": at, ":s": stamped})}},
                {"Put": {"TableName": tbl_name, "Item": _serialize({
                    "PK": tenant.tenant_pk(tenant_id), "SK": f"AUDIT#{at}#{secrets.token_hex(6)}",
                    "action": "setup_completed", "by": actor, "at": at,
                    "details": {"done": result["done"], "skipped": result["skipped"], "total": result["total"]}})}},
                {"Put": {"TableName": tbl_name, "Item": _serialize({
                    "PK": NOTICES_PK, "SK": f"{at}#{tenant_id}#{NOTICE_KIND}", "kind": NOTICE_KIND,
                    "tenantId": tenant_id, "tenantName": row.get("name") or tenant_id, "at": at}),
                    "ConditionExpression": "attribute_not_exists(PK)"}},
            ])
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
            # Someone else recorded it first (an owner and the sweep at once): no second notice.
            fresh = tenant.get_tenant(tenant_id, fresh=True) or {}
            return fresh.get("setupCompletedAt")
        tenant.invalidate(tenant_id)
        _announce(tenant_id, row, result, at)
        return at
    if not _same(row.get("setupState"), state):
        table(tbl_name).update_item(
            Key=key, UpdateExpression="SET setupState = :s", ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeValues={":s": {**state, "evaluatedAt": _now()}})
        tenant.invalidate(tenant_id)
    return completed_at


def _announce(tenant_id, row, result, at):
    """After the commit: the bus event and the operators' email. Either may
    fail without undoing anything (the console notice is the record)."""
    try:
        events.publish(SOURCE, "Tenant.SetupCompleted", {
            "tenantId": tenant_id, "completedAt": at, "stepsDone": result["done"],
            "stepsSkipped": result["skipped"]})
    except Exception as exc:  # noqa: BLE001 - logged, never fails the request
        logger.error(json.dumps({"setup": "event_failed", "tenantId": tenant_id, "error": type(exc).__name__}))
    arn = _env("ALERT_TOPIC_ARN")
    if not arn:
        return
    global _sns
    if _sns is None:
        _sns = boto3.client("sns")
    env = os.environ.get("ENVIRONMENT", "dev")
    name = row.get("name") or tenant_id
    console = (os.environ.get("CONSOLE_APP_URL") or "").rstrip("/")
    lines = [
        f"{name} has finished Kom igång in Booqy Admin ({env}). Time to build their website.",
        "",
        f"Restaurant: {name} ({tenant_id})",
        f"Steps: {result['done']} done, {result['skipped']} skipped, {result['total']} in all",
        f"Finished: {at}",
    ]
    skipped = [s["labelEn"] for s in result["steps"] if s["state"] == rule.SKIPPED]
    if skipped:
        lines.append(f"Skipped: {', '.join(skipped)}")
    if console:
        lines += ["", f"Customer: {console}/tenants/{tenant_id}"]
    try:
        _sns.publish(TopicArn=arn, Subject=f"[{env}] Booqy setup finished: {name}"[:100], Message="\n".join(lines))
    except (BotoCoreError, ClientError) as exc:
        logger.error(json.dumps({"setup": "email_failed", "tenantId": tenant_id, "error": type(exc).__name__}))


def evaluate_and_record(tenant_id, row, actor):
    result = rule.evaluate(load_facts(tenant_id, row))
    completed_at = record(tenant_id, row, result, actor)
    return {**result, "completedAt": completed_at}


# --- routes --------------------------------------------------------------------------------

def get_setup(event):
    ctx = tenant.for_jwt(event, owner_only=True)
    row = tenant.get_tenant(ctx.tenant_id, fresh=True) or ctx.tenant
    return http.respond(200, evaluate_and_record(ctx.tenant_id, row, ctx.sub))


def put_skip(event):
    ctx = tenant.for_jwt(event, owner_only=True)
    body = http.body(event, allowed={"step", "locationId", "skipped"})
    step = rule.BY_ID.get(body.get("step")) if isinstance(body.get("step"), str) else None
    if step is None:
        raise ValueError("step is invalid")
    if not step.skippable:
        raise ValueError("step can not be skipped")
    skipped = body.get("skipped")
    if not isinstance(skipped, bool):
        raise ValueError("skipped must be true or false")
    location_id = body.get("locationId")
    if step.scope == rule.LOCATION:
        if not isinstance(location_id, str) or not location_id or "#" in location_id or "@" in location_id:
            raise ValueError("locationId is invalid")
        if not tenant.get_location(ctx.tenant_id, location_id):
            return http.error(404, "not_found")
    elif location_id is not None:
        raise ValueError("locationId is not used for this step")
    key = rule.skip_key(step.id, location_id if step.scope == rule.LOCATION else None)
    tbl = table(os.environ["TENANT_TABLE_NAME"])
    pk = tenant.tenant_pk(ctx.tenant_id)
    at = _now()
    if skipped:
        tbl.put_item(Item={"PK": pk, "SK": f"{SKIP_PREFIX}{key}", "step": step.id, "at": at, "by": ctx.sub,
                           **({"locationId": location_id} if location_id else {})})
    else:
        tbl.delete_item(Key={"PK": pk, "SK": f"{SKIP_PREFIX}{key}"})
    tbl.put_item(Item={"PK": pk, "SK": f"AUDIT#{at}#{secrets.token_hex(6)}",
                       "action": "setup_step_skipped" if skipped else "setup_step_unskipped",
                       "by": ctx.sub, "at": at,
                       "details": {"step": step.id, **({"locationId": location_id} if location_id else {})}})
    row = tenant.get_tenant(ctx.tenant_id, fresh=True) or ctx.tenant
    return http.respond(200, evaluate_and_record(ctx.tenant_id, row, ctx.sub))


ROUTES = {
    "GET /tenant/setup": get_setup,
    "PUT /tenant/setup/skips": put_skip,
}


def sweep():
    """Every active tenant, evaluated and recorded (the console's progress
    stays current). One tenant failing doesn't stop the rest."""
    tbl = table(os.environ["TENANT_TABLE_NAME"])
    rows = _query_all(tbl, IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq("TENANT"))
    checked = completed = failed = 0
    for summary in rows:
        tenant_id = summary.get("tenantId")
        if not tenant_id or summary.get("status") != "active":
            continue
        try:
            row = tenant.get_tenant(tenant_id, fresh=True) or {}
            if row.get("status") != "active":
                continue
            before = row.get("setupCompletedAt")
            after = evaluate_and_record(tenant_id, row, "system:setup-sweep")["completedAt"]
            checked += 1
            completed += int(bool(after and not before))
        except Exception as exc:  # noqa: BLE001 - next tenant
            failed += 1
            logger.error(json.dumps({"setup": "sweep_failed", "tenantId": tenant_id, "error": type(exc).__name__}))
    result = {"status": "ok" if not failed else "partial", "checked": checked, "completed": completed,
              "failed": failed}
    logger.info(json.dumps({"setup": "sweep", **result}))
    if failed and not checked:
        raise RuntimeError("setup sweep failed for every tenant")
    return result


def handler(event, context=None):
    if isinstance(event, dict) and event.get("setupTask") == "sweep" and "routeKey" not in event:
        return sweep()
    method, path = http.route(event)
    fn = ROUTES.get(f"{method} {path}")
    if fn is None:
        return http.error(404, "not_found")
    try:
        return fn(event)
    except tenant.TenantError as exc:
        return exc.response()
    except ValueError as exc:
        return http.respond(400, {"error": str(exc)})
    except (BotoCoreError, ClientError) as exc:
        logger.error(json.dumps({"setup": "aws", "error": type(exc).__name__}))
        return http.error(503, "service_unavailable")

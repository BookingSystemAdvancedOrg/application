"""meta-ads-jobs

TRIGGER:
    Step Functions `<prefix>meta-ads-publish` tasks:
        {"task": "CheckGuards" | "UploadImage" | "CreateCampaign" | "CreateAdSet"
                 | "CreateCreative" | "CreateAd" | "MarkPaused" | "MarkFailed",
         "tenantId", "campaignId", "error"?}
    EventBridge Scheduler (04:00 Europe/Stockholm): {"job": "insights", "day"?}
    meta-ads API (async, kill switch turned on) and Scheduler (every 15 min):
        {"job": "sweep"} - kill switch, plus publishes stuck in `publishing`
        without an executionArn for 10 minutes are started (or marked
        failed with `start_failed`)

PURPOSE:
    The Meta side of ADR-A. Every publish task is idempotent and resumable:
    it skips a step whose Meta id is already stored, and before creating an
    object it looks it up by its name (`booqy-<campaignId>-<step>`), so a
    call that succeeded but whose write was lost never creates a duplicate.
    Every ad set gets a lifetime budget, an end date and the DSA fields.

    Nightly insights store one INSIGHT row per campaign and day (a rerun
    overwrites, never double-counts), recompute the month's spend from those
    rows, mail the owner once at 80 % of the monthly cap and pause every
    active campaign at 100 %. The platform metric `Booqy/MetaAds
    PlatformSpendSek` feeds the spend alarm.

ENV_VARS:
    ENVIRONMENT, META_ADS_TABLE_NAME, TENANT_TABLE_NAME, USER_TABLE_NAME,
    USER_TENANT_INDEX_NAME, MARKETING_TABLE_NAME, MENU_IMAGES_BUCKET_NAME,
    EVENT_BUS_NAME, NOTIFICATION_FUNCTION_NAME, META_CLIENT,
    PUBLISH_STATE_MACHINE_ARN
"""

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from shared import dynamo, events, tenant
from shared.features import meta_ads as ads
from shared.meta_graph import MetaGraphError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MENU_PREFIX = "menu-images/{tenant}/locations/{location}/"


class GuardFailed(Exception):
    """Not retried by the state machine: goes to MarkFailed."""


def _publish(detail_type, detail):
    try:
        events.publish(ads.FEATURE_ID, detail_type, detail)
    except Exception as exc:
        logger.warning(json.dumps({"event": detail_type, "publish": "failed", "type": type(exc).__name__}))


def _name(campaign_id, step):
    return f"booqy-{campaign_id}-{step}"


def _store(tenant_id, campaign_id, field, value):
    """meta.<field> = value, only if not set yet (a retried task keeps the first)."""
    try:
        ads.table().update_item(Key=ads.campaign_key(tenant_id, campaign_id),
                                UpdateExpression="SET meta.#f = :v",
                                ConditionExpression="attribute_not_exists(meta.#f)",
                                ExpressionAttributeNames={"#f": field}, ExpressionAttributeValues={":v": value})
    except ClientError as exc:
        if not ads.conditional_failed(exc):
            raise


def _context(job):
    tenant_id, campaign_id = job.get("tenantId"), job.get("campaignId")
    item = ads.get_campaign(tenant_id, campaign_id) if tenant_id and campaign_id else None
    if not item:
        raise GuardFailed("not_found")
    return tenant_id, campaign_id, item


def _token(tenant_id):
    token = ads.get_token(tenant_id)
    if token is None:
        raise GuardFailed("not_connected")
    return token


def _ensure(tenant_id, item, field, kind, create):
    """The Meta object's id: stored, found by name, or created now."""
    meta = item.get("meta") or {}
    if meta.get(field):
        return meta[field]
    connection = ads.get_connection(tenant_id) or {}
    token = _token(tenant_id)
    name = _name(item["campaignId"], kind)
    object_id = ads.graph().find_by_name(token, connection.get("adAccountId"), kind, name) \
        or create(token, connection, name)
    _store(tenant_id, item["campaignId"], field, object_id)
    return object_id


def check_guards(tenant_id, campaign_id, item):
    if item.get("status") != "publishing":
        raise GuardFailed("invalid_status")
    connection = ads.get_connection(tenant_id)
    if not connection or connection.get("status") != "connected":
        raise GuardFailed("not_connected")
    if ads.blocked_by(tenant_id, connection):
        raise GuardFailed("kill_switch")
    tenant_row = tenant.get_tenant(tenant_id) or {}
    if not ((tenant_row.get("entitlements") or {}).get("features") or {}).get(ads.FEATURE_KEY):
        raise GuardFailed("feature_not_in_plan")


def upload_image(tenant_id, campaign_id, item):
    if (item.get("meta") or {}).get("imageHash"):
        return
    key = item["creative"]["imageKey"]
    prefix = MENU_PREFIX.format(tenant=tenant_id, location=item["locationId"])
    if not key.startswith(prefix) or ".." in key:
        raise GuardFailed("unknown_image")  # never upload another tenant's picture
    data = boto3.client("s3").get_object(Bucket=os.environ["MENU_IMAGES_BUCKET_NAME"], Key=key)["Body"].read()
    connection = ads.get_connection(tenant_id) or {}
    image_hash = ads.graph().upload_image(_token(tenant_id), connection.get("adAccountId"), data,
                                          _name(campaign_id, "image"))
    _store(tenant_id, campaign_id, "imageHash", image_hash)


def create_campaign(tenant_id, campaign_id, item):
    _ensure(tenant_id, item, "campaignId", "campaign", lambda token, conn, name: ads.graph().create_campaign(
        token, conn["adAccountId"], name, ads.OBJECTIVES[item["goal"]]))


def create_ad_set(tenant_id, campaign_id, item):
    connection = ads.get_connection(tenant_id) or {}
    dsa = connection.get("dsa") or {}
    if not dsa.get("beneficiary") or not dsa.get("payor"):
        raise GuardFailed("assets_missing")
    geo = {"lat": float(item["geo"]["lat"]), "lon": float(item["geo"]["lon"]), "radiusKm": int(item["radiusKm"])}

    def create(token, conn, name):
        return ads.graph().create_ad_set(
            token, conn["adAccountId"], item["meta"]["campaignId"], name,
            lifetime_budget_ore=int(item["budgetSek"]) * 100, start=item["startDate"], end=item["endDate"],
            geo=geo, age_min=18, dsa_beneficiary=dsa["beneficiary"], dsa_payor=dsa["payor"])
    _ensure(tenant_id, item, "adSetId", "adset", create)


def create_creative(tenant_id, campaign_id, item):
    creative = item["creative"]
    _ensure(tenant_id, item, "creativeId", "creative", lambda token, conn, name: ads.graph().create_creative(
        token, conn["adAccountId"], name, conn["pageId"], conn.get("instagramId"), item["meta"]["imageHash"],
        creative["headline"], creative["primaryText"], item["link"], creative["cta"]))


def create_ad(tenant_id, campaign_id, item):
    _ensure(tenant_id, item, "adId", "ad", lambda token, conn, name: ads.graph().create_ad(
        token, conn["adAccountId"], name, item["meta"]["adSetId"], item["meta"]["creativeId"]))


def mark_paused(tenant_id, campaign_id, item):
    try:
        ads.table().update_item(Key=ads.campaign_key(tenant_id, campaign_id),
                                UpdateExpression="SET #s = :p, version = version + :one",
                                ConditionExpression="#s = :publishing", ExpressionAttributeNames={"#s": "status"},
                                ExpressionAttributeValues={":p": "paused", ":publishing": "publishing", ":one": 1})
    except ClientError as exc:
        if not ads.conditional_failed(exc):
            raise
        return  # already marked: a retried task
    _publish("MetaCampaign.Created", {"tenantId": tenant_id, "campaignId": campaign_id,
                                      "locationId": item["locationId"], "goal": item["goal"],
                                      "budgetSek": int(item["budgetSek"]), "endDate": item["endDate"]})


def mark_failed(tenant_id, campaign_id, item, error):
    code = "meta_unavailable"
    if isinstance(error, dict):
        cause = str(error.get("Cause") or error.get("Error") or "")
        for known in ("kill_switch", "not_connected", "assets_missing", "unknown_image", "invalid_status",
                      "feature_not_in_plan", "not_found"):
            if known in cause:
                code = known
                break
    try:
        ads.table().update_item(Key=ads.campaign_key(tenant_id, campaign_id),
                                UpdateExpression="SET #s = :f, #e = :e, version = version + :one",
                                ConditionExpression="#s = :publishing",
                                ExpressionAttributeNames={"#s": "status", "#e": "error"},
                                ExpressionAttributeValues={":f": "failed", ":e": {"code": code},
                                                           ":publishing": "publishing", ":one": 1})
    except ClientError as exc:
        if not ads.conditional_failed(exc):
            raise


TASKS = {"CheckGuards": check_guards, "UploadImage": upload_image, "CreateCampaign": create_campaign,
         "CreateAdSet": create_ad_set, "CreateCreative": create_creative, "CreateAd": create_ad,
         "MarkPaused": mark_paused}


def run_task(job):
    name = job.get("task")
    if name == "MarkFailed":
        try:
            tenant_id, campaign_id, item = _context(job)
        except GuardFailed:
            return {"status": "failed"}
        mark_failed(tenant_id, campaign_id, item, job.get("error"))
        return {"status": "failed"}
    fn = TASKS.get(name)
    if fn is None:
        raise GuardFailed("unknown_task")
    tenant_id, campaign_id, item = _context(job)
    fn(tenant_id, campaign_id, item)
    return {"tenantId": tenant_id, "campaignId": campaign_id}


# --- nightly insights -----------------------------------------------------------------

def connected_tenants():
    tbl, out, kwargs = ads.table(), [], {"IndexName": "byConnected",
                                         "KeyConditionExpression": Key("GSI1PK").eq("CONNECTED")}
    while True:
        page = tbl.query(**kwargs)
        out.extend(i["GSI1SK"] for i in page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return out
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _put_metric(spend_sek):
    boto3.client("cloudwatch").put_metric_data(Namespace="Booqy/MetaAds", MetricData=[
        {"MetricName": "PlatformSpendSek", "Value": float(spend_sek), "Unit": "None"}])


def _owner_emails(tenant_id):
    name, index = os.environ.get("USER_TABLE_NAME"), os.environ.get("USER_TENANT_INDEX_NAME")
    if not name or not index:
        return []
    items = dynamo.table(name).query(IndexName=index, KeyConditionExpression=Key("tenantId").eq(tenant_id)).get(
        "Items") or []
    return [i["email"] for i in items if i.get("role") == "owner_user" and i.get("status") == "active"
            and i.get("email")]


def _notify(notice):
    name = os.environ.get("NOTIFICATION_FUNCTION_NAME")
    if name:
        boto3.client("lambda").invoke(FunctionName=name, InvocationType="Event",
                                      Payload=json.dumps({"notice": notice}).encode())


def _warn_owner(tenant_id, spend_sek, cap_sek):
    for email in _owner_emails(tenant_id):
        _notify({"version": 1, "kind": "meta-ads.cap-warning", "tenantId": tenant_id, "channel": "email",
                 "to": email, "subject": "Era annonser har använt 80 % av månadens budget",
                 "text": (f"Era annonser på Facebook och Instagram har kostat {spend_sek} kr den här månaden. "
                          f"Taket är {cap_sek} kr. När taket nås pausar Booqy alla annonser automatiskt. "
                          "Du kan ändra taket under Marknadsföring > Annonser."),
                 "replyTo": None})


def sync_tenant(tenant_id, day: date) -> int:
    """One tenant's insights for `day`. Returns the day's spend in öre."""
    tenant_row = tenant.get_tenant(tenant_id) or {}
    if not tenant.ever_on(tenant_row, ads.FEATURE_KEY):
        return 0
    token = ads.get_token(tenant_id)
    connection = ads.get_connection(tenant_id) or {}
    if token is None or not connection.get("adAccountId"):
        return 0
    day_s = day.isoformat()
    live = [c for c in ads.campaigns(tenant_id) if (c.get("meta") or {}).get("campaignId")
            and c.get("status") in ("active", "paused", "completed") and c["startDate"] <= day_s <= c["endDate"]]
    by_meta = {c["meta"]["campaignId"]: c for c in live}
    rows = ads.graph().insights(token, connection["adAccountId"], list(by_meta), day_s) if by_meta else []
    day_ore = 0
    ttl = int((ads.now() + timedelta(days=ads.INSIGHT_TTL_DAYS)).timestamp())
    for row in rows:
        item = by_meta.get(row.get("campaign_id"))
        if not item:
            continue
        got = ads.attribution(tenant_id, item["locationId"], item["utmCampaign"], day_s)
        ads.table().put_item(Item={  # overwrite: a rerun for the same day never double-counts
            **ads.insight_key(tenant_id, item["campaignId"], day_s), "tenantId": tenant_id,
            "impressions": int(row.get("impressions") or 0), "reach": int(row.get("reach") or row.get("impressions")
                                                                          or 0),
            "clicks": int(row.get("clicks") or 0), "spendOre": int(row.get("spend_ore") or 0), **got, "ttl": ttl})
        day_ore += int(row.get("spend_ore") or 0)
    month = ads.month_of(day_s)
    month_ore = sum(int(i.get("spendOre") or 0) for c in ads.campaigns(tenant_id)
                    for i in ads.insights(tenant_id, c["campaignId"], f"{month}-01", f"{month}-31"))
    spend = ads.table().update_item(Key=ads.spend_key(tenant_id, month), UpdateExpression="SET spendOre = :s, "
                                    "tenantId = :t", ExpressionAttributeValues={":s": month_ore, ":t": tenant_id},
                                    ReturnValues="ALL_NEW")["Attributes"]
    cap_sek = min(int(connection.get("monthlyCapSek") or ads.DEFAULT_MONTHLY_CAP_SEK),
                  ads.platform_settings()["maxMonthlyCapSek"])
    if month_ore >= cap_sek * 100:
        if not spend.get("cappedAt"):
            ads.table().update_item(Key=ads.spend_key(tenant_id, month), UpdateExpression="SET cappedAt = :at",
                                    ExpressionAttributeValues={":at": ads.iso(ads.now())})
            _publish("MetaSpend.CapReached", {"tenantId": tenant_id, "month": month, "spendSek": month_ore // 100,
                                              "capSek": cap_sek})
        ads.pause_all(tenant_id, "cap")
    elif month_ore >= Decimal(cap_sek * 100) * ads.WARN_SHARE:
        try:
            ads.table().update_item(Key=ads.spend_key(tenant_id, month), UpdateExpression="SET warnedAt = :at",
                                    ConditionExpression="attribute_not_exists(warnedAt)",
                                    ExpressionAttributeValues={":at": ads.iso(ads.now())})
            _warn_owner(tenant_id, month_ore // 100, cap_sek)
        except ClientError as exc:
            if not ads.conditional_failed(exc):
                raise
    for item in live:  # finished campaigns
        if item["endDate"] < ads.now().date().isoformat() and item.get("status") in ("active", "paused"):
            ads.table().update_item(Key=ads.campaign_key(tenant_id, item["campaignId"]),
                                    UpdateExpression="SET #s = :c", ConditionExpression="#s IN (:a, :p)",
                                    ExpressionAttributeNames={"#s": "status"},
                                    ExpressionAttributeValues={":c": "completed", ":a": "active", ":p": "paused"})
    return day_ore


def run_insights(job):
    day = job.get("day")
    day = date.fromisoformat(day) if isinstance(day, str) else datetime.now(timezone.utc).date() - timedelta(days=1)
    total_ore, failed = 0, []
    for tenant_id in connected_tenants():
        try:
            total_ore += sync_tenant(tenant_id, day)
        except (MetaGraphError, ClientError) as exc:
            failed.append(tenant_id)
            logger.error(json.dumps({"insights": "failed", "tenantId": tenant_id, "type": type(exc).__name__}))
    _put_metric(total_ore // 100)
    return {"day": day.isoformat(), "spendSek": total_ore // 100, "failed": failed}


STRANDED_AFTER = timedelta(minutes=10)


def _start_execution(campaign_id, payload):
    """Same execution name as A8 (the campaign id): a start the API already
    made is a no-op (ExecutionAlreadyExists).

    Known limit: if that existing execution has already finished (e.g. it
    failed before its MarkFailed task ran), the campaign keeps `publishing`
    with this ARN and the sweep skips it from then on; the owner must retry.
    DescribeExecution is deliberately not granted to this function, so the
    sweep cannot tell a running execution from a finished one."""
    arn = os.environ["PUBLISH_STATE_MACHINE_ARN"]
    try:
        return boto3.client("stepfunctions").start_execution(
            stateMachineArn=arn, name=campaign_id, input=json.dumps(payload))["executionArn"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ExecutionAlreadyExists":
            raise
        return arn.replace(":stateMachine:", ":execution:") + f":{campaign_id}"


def restart_stranded(tenant_id) -> dict:
    """A campaign still `publishing` without an executionArn 10 minutes after
    it was created lost its StartExecution (the API died or Step Functions
    refused) and the owner did not retry. Start it now; if that fails too,
    mark it failed with `start_failed` so it never sits in `publishing`."""
    started = failed = 0
    cutoff = ads.iso(ads.now() - STRANDED_AFTER)
    for item in ads.campaigns(tenant_id):
        if item.get("status") != "publishing" or item.get("executionArn") or (item.get("createdAt") or "") > cutoff:
            continue
        key = ads.campaign_key(tenant_id, item["campaignId"])
        try:
            arn = _start_execution(item["campaignId"], {"tenantId": tenant_id, "campaignId": item["campaignId"]})
        except ClientError as exc:
            logger.error(json.dumps({"sweep": "start_failed", "tenantId": tenant_id,
                                     "campaignId": item["campaignId"], "type": type(exc).__name__}))
            try:
                ads.table().update_item(Key=key, UpdateExpression="SET #s = :f, #e = :e, version = version + :one",
                                        ConditionExpression="#s = :publishing AND attribute_not_exists(executionArn)",
                                        ExpressionAttributeNames={"#s": "status", "#e": "error"},
                                        ExpressionAttributeValues={":f": "failed", ":e": {"code": "start_failed"},
                                                                   ":publishing": "publishing", ":one": 1})
                failed += 1
            except ClientError as cond:
                if not ads.conditional_failed(cond):
                    raise
            continue
        try:
            ads.table().update_item(Key=key, UpdateExpression="SET executionArn = :a",
                                    ConditionExpression="attribute_not_exists(executionArn)",
                                    ExpressionAttributeValues={":a": arn})
        except ClientError as cond:
            if not ads.conditional_failed(cond):
                raise
        started += 1
    return {"started": started, "failed": failed}


def run_sweep(job):
    """Every 15 minutes: pause every active campaign of every blocked tenant
    (kill switch) and restart publishes whose workflow never started."""
    paused = started = failed = 0
    for tenant_id in connected_tenants():
        if ads.blocked_by(tenant_id):
            paused += ads.pause_all(tenant_id, "kill_switch")
        try:
            stranded = restart_stranded(tenant_id)
        except Exception as exc:  # one tenant must not stop the kill-switch pause of the rest
            logger.error(json.dumps({"sweep": "restart_failed", "tenantId": tenant_id,
                                     "type": type(exc).__name__}))
            continue
        started, failed = started + stranded["started"], failed + stranded["failed"]
    return {"paused": paused, "started": started, "failed": failed}


def handler(event, context=None):
    event = event or {}
    if "task" in event:
        return run_task(event)
    if event.get("job") == "insights":
        return run_insights(event)
    if event.get("job") == "sweep":
        return run_sweep(event)
    raise ValueError("unknown job")

"""meta-ads

TRIGGER:
    API Gateway -- Auth: JWT (tenant pool), owner only
    GET    /tenant/meta-ads/connection                       A1
    POST   /tenant/meta-ads/connection/start                 A2 {} -> {authorizeUrl}
    POST   /tenant/meta-ads/connection/callback              A3 {code, state}
    GET    /tenant/meta-ads/assets                           A4
    PUT    /tenant/meta-ads/connection                       A5 assets, DSA, monthly cap
    DELETE /tenant/meta-ads/connection                       A6
    POST   /locations/{locationId}/meta-ads/copy             A7 Bedrock text suggestions
    POST   /locations/{locationId}/meta-ads/campaigns        A8 -> 202 (publish workflow)
    GET    /locations/{locationId}/meta-ads/campaigns        A9
    GET    /locations/{locationId}/meta-ads/campaigns/{campaignId}            A10
    POST   /locations/{locationId}/meta-ads/campaigns/{campaignId}/activate   A11
    POST   /locations/{locationId}/meta-ads/campaigns/{campaignId}/pause      A12
    GET    /locations/{locationId}/meta-ads/insights?from=&to=                 A13

    Lambda invoke (no requestContext): {"hook": "platform"} (operator routes
    A14-A17 forwarded by Booqy Console) and {"hook": "tenant.purge"}.

PURPOSE:
    Facebook/Instagram ads for restaurants (ADR-A). Every Meta call goes
    through shared/meta_graph (simulator unless META_CLIENT=real). A
    campaign is always created PAUSED by the publish workflow and needs
    "Starta annonsen" (A11). No Pixel, no Conversions API, no guest data to
    Meta; attribution is the existing cookieless UTM counters.

ENV_VARS:
    ENVIRONMENT, META_ADS_TABLE_NAME, TENANT_TABLE_NAME, LOCATION_TABLE_NAME,
    LOCATION_ID_INDEX_NAME, MENU_TABLE_NAME, MARKETING_TABLE_NAME,
    PUBLISH_STATE_MACHINE_ARN, META_REDIRECT_URI, SITE_BASE_DOMAIN,
    EVENT_BUS_NAME, JOBS_FUNCTION_NAME, META_CLIENT, BEDROCK_CLIENT,
    BEDROCK_MODEL_ID
"""

import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from shared import dynamo, events, http, tenant
from shared.bedrock import TextModelError
from shared.features import meta_ads as ads
from shared.meta_graph import MetaGraphError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
MAX_RANGE_DAYS = 93
COPY_SCHEMA = {"type": "object", "required": ["suggestions"], "properties": {"suggestions": {
    "type": "array", "minItems": 3, "maxItems": 3, "items": {
        "type": "object", "required": ["headline", "primaryText"], "properties": {
            "headline": {"type": "string", "maxLength": ads.MAX_HEADLINE},
            "primaryText": {"type": "string", "maxLength": ads.MAX_PRIMARY}}}}}}
COPY_SYSTEM = ("Du skriver korta, sakliga annonstexter på svenska för en restaurangs Facebook- och "
               "Instagramannons. Inga påhittade priser, erbjudanden eller påståenden. Ingen alkohol. "
               "Svara bara med JSON enligt schemat.")


def _err(status, code, field=None):
    body = {"error": code, "code": code}
    if field:
        body["field"] = field
    return http.respond(status, body)


def _publish(detail_type, detail):
    try:
        events.publish(ads.FEATURE_ID, detail_type, detail)
    except Exception as exc:
        logger.warning(json.dumps({"event": detail_type, "publish": "failed", "type": type(exc).__name__}))


def _today():
    return datetime.now(timezone.utc).date()


# --- connection -----------------------------------------------------------------------

def _connection_view(ctx, item):
    item = item or {}
    month_ore = ads.spend_this_month_ore(ctx.tenant_id)
    out = {"status": item.get("status") or "disconnected",
           "dsa": ads.plain(item.get("dsa") or _default_dsa(ctx)),
           "monthlyCapSek": int(item.get("monthlyCapSek") or ads.DEFAULT_MONTHLY_CAP_SEK),
           "spendThisMonthSek": month_ore // 100,
           "killSwitch": ads.blocked_by(ctx.tenant_id, item) is not None,
           "missingPermissions": [s for s in _wanted_scopes(ctx) if s not in (item.get("scopes") or [])]
           if item.get("status") == "connected" else [],
           "version": int(item.get("version") or 0)}
    assets = {k: item[k] for k in ("adAccountId", "pageId", "instagramId", "businessId") if item.get(k)}
    if assets:
        out["assets"] = assets
    return out


def _default_dsa(ctx):
    name = (ctx.tenant.get("legalName") or ctx.tenant.get("name") or "").strip()
    return {"beneficiary": name, "payor": name}


def _wanted_scopes(ctx):
    features = (ctx.tenant.get("entitlements") or {}).get("features") or {}
    scopes = list(ads.ADS_SCOPES)
    if features.get("socialPosting"):
        scopes += list(ads.SOCIAL_SCOPES)
    return scopes


def get_connection(event, ctx):
    return http.respond(200, _connection_view(ctx, ads.get_connection(ctx.tenant_id)))


def start(event, ctx):
    http.body(event, allowed=(), required=False)
    state = ads.new_state()
    at = ads.now()
    ads.table().put_item(Item={**ads.oauth_key(ctx.tenant_id, state), "tenantId": ctx.tenant_id, "sub": ctx.sub,
                               "createdAt": ads.iso(at),
                               "ttl": int((at + timedelta(minutes=ads.OAUTH_TTL_MINUTES)).timestamp())})
    url = ads.graph().authorize_url(state, os.environ.get("META_REDIRECT_URI", ""), _wanted_scopes(ctx))
    return http.respond(200, {"authorizeUrl": url})


def callback(event, ctx):
    body = http.body(event, allowed=("code", "state"))
    code, state = body.get("code"), body.get("state")
    if not isinstance(code, str) or not code or not isinstance(state, str) or not 8 <= len(state) <= 128 \
            or "#" in state:
        raise ads.AdsError(400, "invalid_state", "state")
    now_epoch = int(ads.now().timestamp())
    try:  # single use: the row is gone after the first callback
        ads.table().delete_item(Key=ads.oauth_key(ctx.tenant_id, state),
                                ConditionExpression="attribute_exists(PK) AND #sub = :sub AND #ttl > :now",
                                ExpressionAttributeNames={"#sub": "sub", "#ttl": "ttl"},
                                ExpressionAttributeValues={":sub": ctx.sub, ":now": now_epoch})
    except ClientError as exc:
        if not ads.conditional_failed(exc):
            raise
        raise ads.AdsError(400, "invalid_state", "state")
    token = ads.graph().exchange_code(code, os.environ.get("META_REDIRECT_URI", ""))
    ads.put_token(ctx.tenant_id, token.access_token, token.scopes)
    at = ads.iso(ads.now())
    item = ads.table().update_item(
        Key=ads.connection_key(ctx.tenant_id),
        UpdateExpression="SET #s = :c, scopes = :sc, connectedAt = :at, connectedBy = :by, tenantId = :t, "
                         "GSI1PK = :g, GSI1SK = :t, version = if_not_exists(version, :zero) + :one",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":c": "connected", ":sc": list(token.scopes), ":at": at, ":by": ctx.sub,
                                   ":t": ctx.tenant_id, ":g": "CONNECTED", ":zero": 0, ":one": 1},
        ReturnValues="ALL_NEW")["Attributes"]
    return http.respond(200, _connection_view(ctx, item))


def _require_token(ctx):
    connection = ads.get_connection(ctx.tenant_id)
    token = ads.get_token(ctx.tenant_id) if connection and connection.get("status") == "connected" else None
    if token is None:
        raise ads.AdsError(409, "not_connected")
    return connection, token


def _assets(token):
    found = ads.graph().list_assets(token)
    missing = []
    for kind, present in (("business", found.businesses), ("ad_account", found.ad_accounts),
                          ("page", found.pages), ("instagram", found.instagram)):
        if not present:
            missing.append({"kind": kind, "deepLink": ads.DEEP_LINKS[kind]})
    if found.ad_accounts and not any(a.get("has_payment_method") for a in found.ad_accounts):
        missing.append({"kind": "payment_method", "deepLink": ads.DEEP_LINKS["payment_method"]})
    return found, missing


def assets(event, ctx):
    _, token = _require_token(ctx)
    found, missing = _assets(token)
    return http.respond(200, {"businesses": found.businesses, "adAccounts": found.ad_accounts,
                              "pages": found.pages, "instagram": found.instagram, "missing": missing})


def put_connection(event, ctx):
    body = http.body(event, allowed=("adAccountId", "pageId", "instagramId", "dsa", "monthlyCapSek", "version"))
    connection, token = _require_token(ctx)
    version = body.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ads.AdsError(400, "version is required", "version")
    ads_on = bool(((ctx.tenant.get("entitlements") or {}).get("features") or {}).get(ads.FEATURE_KEY))
    found, _ = _assets(token)
    ids = {"adAccountId": {a["id"] for a in found.ad_accounts}, "pageId": {p["id"] for p in found.pages},
           "instagramId": {i["id"] for i in found.instagram}}
    if not isinstance(body.get("pageId"), str) or body["pageId"] not in ids["pageId"]:
        raise ads.AdsError(400, "unknown_asset", "pageId")
    for field in ("adAccountId", "instagramId"):
        if body.get(field) is not None and body[field] not in ids[field]:
            raise ads.AdsError(400, "unknown_asset", field)
    if ads_on and not body.get("adAccountId"):
        raise ads.AdsError(400, "unknown_asset", "adAccountId")
    dsa = body.get("dsa")
    if dsa is not None or ads_on:
        if not isinstance(dsa, dict) or set(dsa) != {"beneficiary", "payor"} or not all(
                isinstance(v, str) and 0 < len(v.strip()) <= 120 for v in dsa.values()):
            raise ads.AdsError(400, "dsa must name the beneficiary and the payor", "dsa")
        dsa = {k: v.strip() for k, v in dsa.items()}
    cap = body.get("monthlyCapSek")
    if cap is not None or ads_on:
        ceiling = ads.platform_settings()["maxMonthlyCapSek"]
        if not isinstance(cap, int) or isinstance(cap, bool) or not ads.MIN_BUDGET_SEK <= cap <= ceiling:
            raise ads.AdsError(400, "invalid_cap", "monthlyCapSek")
    currency = next((a.get("currency") for a in found.ad_accounts if a["id"] == body.get("adAccountId")), None)
    sets, values = ["pageId = :p", "version = version + :one", "updatedAt = :at"], \
        {":p": body["pageId"], ":one": 1, ":at": ads.iso(ads.now()), ":v": version}
    removes = []
    for field, value in (("adAccountId", body.get("adAccountId")), ("instagramId", body.get("instagramId")),
                         ("dsa", dsa), ("monthlyCapSek", cap), ("currency", currency)):
        if value is not None:
            sets.append(f"{field} = :{field}")
            values[f":{field}"] = value
        elif field == "instagramId":
            removes.append(field)
    if found.businesses:
        sets.append("businessId = :b")
        values[":b"] = found.businesses[0]["id"]
    expr = "SET " + ", ".join(sets) + (" REMOVE " + ", ".join(removes) if removes else "")
    try:
        item = ads.table().update_item(Key=ads.connection_key(ctx.tenant_id), UpdateExpression=expr,
                                       ConditionExpression="version = :v", ExpressionAttributeValues=values,
                                       ReturnValues="ALL_NEW")["Attributes"]
    except ClientError as exc:
        if not ads.conditional_failed(exc):
            raise
        raise ads.AdsError(409, "changed_retry")
    if item.get("adAccountId") and not connection.get("adAccountId"):
        _publish("MetaConnection.Connected", {"tenantId": ctx.tenant_id, "adAccountId": item["adAccountId"],
                                              "pageId": item["pageId"],
                                              **({"instagramId": item["instagramId"]} if item.get("instagramId")
                                                 else {})})
    return http.respond(200, _connection_view(ctx, item))


def disconnect(tenant_id, reason):
    """Pauses every active campaign, revokes and deletes the token, marks
    the connection disconnected. Safe to repeat."""
    connection = ads.get_connection(tenant_id)
    if not connection or connection.get("status") == "disconnected":
        ads.delete_token(tenant_id)
        return False
    token = ads.get_token(tenant_id)
    ads.pause_all(tenant_id, "owner")
    if token is not None:
        try:
            ads.graph().revoke(token)
        except MetaGraphError:
            logger.warning(json.dumps({"metaAds": "revoke_failed", "tenantId": tenant_id}))
    ads.delete_token(tenant_id)
    ads.table().update_item(Key=ads.connection_key(tenant_id),
                            UpdateExpression="SET #s = :d, version = version + :one REMOVE GSI1PK, GSI1SK, scopes",
                            ExpressionAttributeNames={"#s": "status"},
                            ExpressionAttributeValues={":d": "disconnected", ":one": 1})
    _publish("MetaConnection.Disconnected", {"tenantId": tenant_id, "reason": reason})
    return True


def delete_connection(event, ctx):
    disconnect(ctx.tenant_id, "owner")
    return {"statusCode": 204, "headers": {"Cache-Control": "no-store"}, "body": ""}


# --- copy and campaigns ---------------------------------------------------------------

def _menu_item(ctx, menu_item_id):
    if not isinstance(menu_item_id, str) or not menu_item_id or "#" in menu_item_id or len(menu_item_id) > 128:
        raise ads.AdsError(404, "not found")
    item = dynamo.table(os.environ["MENU_TABLE_NAME"]).get_item(
        Key={"PK": f"LOCATION#{ctx.location_id}", "SK": f"MENU#{menu_item_id}"}).get("Item")
    if not item:
        raise ads.AdsError(404, "not found")
    if item.get("alcohol") is True:
        raise ads.AdsError(400, "alcohol_not_allowed", "menuItemId")
    return item


def _goal(value):
    if value not in ads.GOALS:
        raise ads.AdsError(400, "goal must be bookings, orders, catering or awareness", "goal")
    return value


def copy(event, ctx):
    body = http.body(event, allowed=("menuItemId", "goal"))
    goal = _goal(body.get("goal"))
    dish = _menu_item(ctx, body.get("menuItemId"))
    month = ads.month_of(_today())
    try:
        ads.table().update_item(Key=ads.copyuse_key(ctx.tenant_id, month),
                                UpdateExpression="ADD #c :one SET #ttl = if_not_exists(#ttl, :ttl), tenantId = :t",
                                ConditionExpression="attribute_not_exists(#c) OR #c < :max",
                                ExpressionAttributeNames={"#c": "count", "#ttl": "ttl"},
                                ExpressionAttributeValues={":one": 1, ":max": ads.COPY_QUOTA_PER_MONTH,
                                                           ":t": ctx.tenant_id,
                                                           ":ttl": int((ads.now() + timedelta(
                                                               days=ads.COPYUSE_TTL_DAYS)).timestamp())})
    except ClientError as exc:
        if not ads.conditional_failed(exc):
            raise
        raise ads.AdsError(429, "quota_exceeded")
    prompt = json.dumps({"restaurang": ctx.tenant.get("name") or "", "rätt": dish.get("name") or "",
                         "beskrivning": (dish.get("description") or "")[:300], "mål": goal}, ensure_ascii=False)
    try:
        raw = ads.text_model().complete(COPY_SYSTEM, prompt, max_tokens=600, json_schema=COPY_SCHEMA)
        parsed = json.loads(raw)
        suggestions = [{"headline": str(s["headline"]).strip()[:ads.MAX_HEADLINE],
                        "primaryText": str(s["primaryText"]).strip()[:ads.MAX_PRIMARY]}
                       for s in parsed["suggestions"]][:3]
    except (TextModelError, ValueError, KeyError, TypeError):
        raise ads.AdsError(502, "ai_unavailable")
    return http.respond(200, {"suggestions": suggestions})


def _int_in(body, field, low, high, code):
    value = body.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ads.AdsError(400, code, field)
    return value


def _text(body, field, limit):
    value = body.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ads.AdsError(400, f"{field} is required", field)
    if len(value.strip()) > limit:
        raise ads.AdsError(400, "text_too_long", field)
    return value.strip()


def _start_execution(name, payload):
    """StartExecution with the campaign id as the execution name: a second
    start for the same campaign is a no-op (ExecutionAlreadyExists)."""
    arn = os.environ["PUBLISH_STATE_MACHINE_ARN"]
    client = boto3.client("stepfunctions")
    try:
        return client.start_execution(stateMachineArn=arn, name=name, input=json.dumps(payload))["executionArn"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ExecutionAlreadyExists":
            raise
        return arn.replace(":stateMachine:", ":execution:") + f":{name}"


def _site_url(ctx):
    slug = ctx.tenant.get("slug")
    domain = os.environ.get("SITE_BASE_DOMAIN", "booqy.se")
    return f"https://{slug}.{domain}" if slug else f"https://{domain}"


def create_campaign(event, ctx):
    body = http.body(event, allowed=("campaignId", "goal", "menuItemId", "headline", "primaryText", "budgetSek",
                                     "days", "radiusKm"))
    campaign_id = body.get("campaignId")
    if not ads.valid_client_id(campaign_id):
        raise ads.AdsError(400, "campaignId is invalid", "campaignId")
    existing = ads.get_campaign(ctx.tenant_id, campaign_id)
    if existing:
        if existing.get("locationId") != ctx.location_id:
            raise ads.AdsError(404, "not found")
        if existing.get("status") == "publishing" and not existing.get("executionArn"):
            _start(ctx, campaign_id)  # the first call died or StartExecution failed: start it now
        return http.respond(202, ads.campaign_view(existing))  # retry: same item, no second execution
    goal = _goal(body.get("goal"))
    budget = _int_in(body, "budgetSek", ads.MIN_BUDGET_SEK, ads.MAX_BUDGET_SEK, "invalid_budget")
    days = _int_in(body, "days", ads.MIN_DAYS, ads.MAX_DAYS, "invalid_days")
    radius = _int_in(body, "radiusKm", ads.MIN_RADIUS_KM, ads.MAX_RADIUS_KM, "invalid_radius")
    headline = _text(body, "headline", ads.MAX_HEADLINE)
    primary = _text(body, "primaryText", ads.MAX_PRIMARY)
    dish = _menu_item(ctx, body.get("menuItemId"))
    connection = ads.get_connection(ctx.tenant_id)
    if not connection or connection.get("status") != "connected":
        raise ads.AdsError(409, "not_connected")
    position = ((ctx.location or {}).get("cateringOrigin") or {}).get("position")
    if not connection.get("adAccountId") or not connection.get("pageId") or not dish.get("imageKey") \
            or not position:
        raise ads.AdsError(409, "assets_missing")
    if ads.blocked_by(ctx.tenant_id, connection):
        raise ads.AdsError(409, "kill_switch")
    ads.check_cap(ctx.tenant_id, connection, budget)
    first = _today() + timedelta(days=1)
    item = {
        **ads.campaign_key(ctx.tenant_id, campaign_id), "tenantId": ctx.tenant_id, "campaignId": campaign_id,
        "locationId": ctx.location_id, "goal": goal, "status": "publishing",
        "creative": {"menuItemId": dish["SK"].removeprefix("MENU#"), "imageKey": dish["imageKey"],
                     "headline": headline, "primaryText": primary, "cta": ads.CTAS[goal]},
        "budgetSek": budget, "startDate": first.isoformat(), "endDate": ads.end_date(first, days).isoformat(),
        "radiusKm": radius, "geo": {"lon": position[0], "lat": position[1]},
        "utmCampaign": ads.utm_campaign(campaign_id), "link": ads.landing_url(_site_url(ctx), goal, campaign_id),
        "meta": {}, "version": 1, "createdBy": ctx.sub, "createdAt": ads.iso(ads.now()),
    }
    try:
        ads.table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    except ClientError as exc:
        if not ads.conditional_failed(exc):
            raise
        return http.respond(202, ads.campaign_view(ads.get_campaign(ctx.tenant_id, campaign_id)))
    _start(ctx, campaign_id)
    return http.respond(202, ads.campaign_view(item))


def _start(ctx, campaign_id):
    """Idempotent by execution name. If StartExecution fails the item stays
    `publishing` without executionArn and the client's retry starts it."""
    arn = _start_execution(campaign_id, {"tenantId": ctx.tenant_id, "campaignId": campaign_id})
    ads.table().update_item(Key=ads.campaign_key(ctx.tenant_id, campaign_id),
                            UpdateExpression="SET executionArn = :a", ExpressionAttributeValues={":a": arn})


def _campaign(event, ctx):
    try:
        campaign_id = http.path_id(event, "campaignId")
    except ValueError:
        raise ads.AdsError(404, "not found")
    item = ads.get_campaign(ctx.tenant_id, campaign_id)
    if not item or item.get("locationId") != ctx.location_id:
        raise ads.AdsError(404, "not found")
    return item


def list_campaigns(event, ctx):
    params = http.query(event)
    if set(params) - {"cursor"} or params.get("cursor"):
        raise ads.AdsError(400, "cursor is invalid", "cursor")
    items = [i for i in ads.campaigns(ctx.tenant_id) if i.get("locationId") == ctx.location_id]
    items.sort(key=lambda i: i.get("createdAt") or "", reverse=True)
    return http.respond(200, {"campaigns": [ads.campaign_view(i) for i in items]})  # one page: no cursor


def get_campaign(event, ctx):
    item = _campaign(event, ctx)
    out = ads.campaign_view(item)
    ad_id = (item.get("meta") or {}).get("adId")
    if ad_id:
        token = ads.get_token(ctx.tenant_id)
        if token is not None:
            out["previewUrl"] = ads.graph().preview_url(token, ad_id)
    return http.respond(200, out)


def _version(event):
    body = http.body(event, allowed=("version",))
    version = body.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ads.AdsError(400, "version is required", "version")
    return version


def activate(event, ctx):
    version = _version(event)
    item = _campaign(event, ctx)
    if item.get("status") == "active":
        return http.respond(200, ads.campaign_view(item))  # retry after success
    if item.get("status") != "paused" or str(item.get("endDate")) <= _today().isoformat():
        raise ads.AdsError(409, "invalid_status")
    if int(item["version"]) != version:
        raise ads.AdsError(409, "changed_retry")
    connection = ads.get_connection(ctx.tenant_id)
    if ads.blocked_by(ctx.tenant_id, connection):
        raise ads.AdsError(409, "kill_switch")
    spent = sum(int(i.get("spendOre") or 0) for i in ads.insights(ctx.tenant_id, item["campaignId"]))
    ads.check_cap(ctx.tenant_id, connection, max(0, int(item["budgetSek"]) - spent // 100),
                  exclude=item["campaignId"])
    token = ads.get_token(ctx.tenant_id)
    if token is None:
        raise ads.AdsError(409, "not_connected")
    for object_id in reversed(ads.meta_ids(item)):  # ad, ad set, campaign
        ads.graph().set_status(token, object_id, "ACTIVE")
    try:
        updated = ads.table().update_item(
            Key=ads.campaign_key(ctx.tenant_id, item["campaignId"]),
            UpdateExpression="SET #s = :a, version = version + :one, activatedAt = :at REMOVE pausedBy",
            ConditionExpression="#s = :p AND version = :v", ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":a": "active", ":p": "paused", ":one": 1, ":v": version,
                                       ":at": ads.iso(ads.now())},
            ReturnValues="ALL_NEW")["Attributes"]
    except ClientError as exc:
        if not ads.conditional_failed(exc):
            raise
        # Lost the race (a pause, a cap sweep or another activate). Meta is
        # ACTIVE now; put it back unless the winner was an activate.
        if (ads.get_campaign(ctx.tenant_id, item["campaignId"]) or {}).get("status") != "active":
            for object_id in ads.meta_ids(item):  # campaign, ad set, ad
                ads.graph().set_status(token, object_id, "PAUSED")
        raise ads.AdsError(409, "changed_retry")
    _publish("MetaCampaign.Activated", {"tenantId": ctx.tenant_id, "campaignId": item["campaignId"],
                                        "locationId": ctx.location_id})
    return http.respond(200, ads.campaign_view(updated))


def pause(event, ctx):
    version = _version(event)
    item = _campaign(event, ctx)
    if item.get("status") == "paused":
        return http.respond(200, ads.campaign_view(item))
    if item.get("status") != "active":
        raise ads.AdsError(409, "invalid_status")
    if int(item["version"]) != version:
        raise ads.AdsError(409, "changed_retry")
    updated = ads.pause_campaign(ctx.tenant_id, item, "owner")
    return http.respond(200, ads.campaign_view(updated or ads.get_campaign(ctx.tenant_id, item["campaignId"])))


def insights(event, ctx):
    params = http.query(event)
    first, last = params.get("from"), params.get("to")
    try:
        if not (isinstance(first, str) and _DAY.fullmatch(first) and isinstance(last, str) and _DAY.fullmatch(last)):
            raise ValueError
        start, end = date.fromisoformat(first), date.fromisoformat(last)
    except ValueError:
        raise ads.AdsError(400, "invalid_range", "from")
    if start > end or (end - start).days > MAX_RANGE_DAYS or set(params) - {"from", "to"}:
        raise ads.AdsError(400, "invalid_range", "from")
    rows, totals = [], {"spendSek": 0, "reach": 0, "clicks": 0, "bookings": 0, "orders": 0}
    spend_ore = 0
    for item in ads.campaigns(ctx.tenant_id):
        if item.get("locationId") != ctx.location_id:
            continue
        for row in ads.insights(ctx.tenant_id, item["campaignId"], first, last):
            ore, bookings, orders = int(row.get("spendOre") or 0), int(row.get("bookings") or 0), \
                int(row.get("orders") or 0)
            spend_ore += ore
            rows.append({"campaignId": item["campaignId"], "day": row["SK"].rsplit("#", 1)[1],
                         "spendSek": ore // 100, "reach": int(row.get("reach") or 0),
                         "clicks": int(row.get("clicks") or 0), "bookings": bookings, "orders": orders,
                         "costPerBookingSek": ads.cost_per(ore, bookings),
                         "costPerOrderSek": ads.cost_per(ore, orders)})
            for k in ("reach", "clicks", "bookings", "orders"):
                totals[k] += rows[-1][k]
    totals["spendSek"] = spend_ore // 100
    totals["costPerBookingSek"] = ads.cost_per(spend_ore, totals["bookings"])
    totals["costPerOrderSek"] = ads.cost_per(spend_ore, totals["orders"])
    rows.sort(key=lambda r: (r["day"], r["campaignId"]))
    return http.respond(200, {"rows": rows, "totals": totals})


# --- hooks: operator routes and purge --------------------------------------------------

def _invoke_jobs(payload):
    name = os.environ.get("JOBS_FUNCTION_NAME")
    if name:
        boto3.client("lambda").invoke(FunctionName=name, InvocationType="Event", Payload=json.dumps(payload).encode())


def _platform(event):
    op, body = event.get("op"), event.get("body") or {}
    operator = (event.get("operator") or {}).get("sub")
    tenant_id = event.get("tenantId")
    if op == "meta-ads.settings.get":
        return {"ok": True, "status": 200, "body": ads.platform_settings()}
    if op == "meta-ads.settings.put":
        settings = ads.put_platform_settings(body, operator)
        if settings["killSwitch"]:
            _invoke_jobs({"job": "sweep"})
        return {"ok": True, "status": 200, "body": settings}
    if op in ("meta-ads.tenant.get", "meta-ads.tenant.pause"):
        if not isinstance(tenant_id, str) or not tenant_id or not tenant.get_tenant(tenant_id, fresh=True):
            return {"ok": False, "status": 404, "code": "not_found"}
        if op == "meta-ads.tenant.pause":
            if set(body) != {"paused"} or not isinstance(body.get("paused"), bool):
                return {"ok": False, "status": 400, "code": "invalid_paused", "field": "paused"}
            ads.table().update_item(Key=ads.connection_key(tenant_id),
                                    UpdateExpression="SET paused = :p, tenantId = :t, "
                                                     "#s = if_not_exists(#s, :d), version = if_not_exists(version, :z)",
                                    ExpressionAttributeNames={"#s": "status"},
                                    ExpressionAttributeValues={":p": body["paused"], ":t": tenant_id,
                                                               ":d": "disconnected", ":z": 0})
            if body["paused"]:
                ads.pause_all(tenant_id, "kill_switch")
            return {"ok": True, "status": 200, "body": {"paused": body["paused"]}}
        connection = ads.get_connection(tenant_id) or {}
        out = {"status": connection.get("status") or "disconnected", "paused": bool(connection.get("paused")),
               "spendThisMonthSek": ads.spend_this_month_ore(tenant_id) // 100}
        out.update({k: connection[k] for k in ("adAccountId", "pageId", "instagramId") if connection.get(k)})
        return {"ok": True, "status": 200, "body": out}
    return {"ok": False, "status": 400, "code": "unknown_op"}


def purge(tenant_id):
    """Offboarding: pause what runs, delete the token and every row."""
    ads.pause_all(tenant_id, "kill_switch")
    ads.delete_token(tenant_id)
    tbl, deleted = ads.table(), 0
    items = ads._query_all(Key("PK").eq(ads.pk(tenant_id)), ProjectionExpression="PK, SK")
    with tbl.batch_writer() as batch:
        for item in items:
            batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
            deleted += 1
    return {"done": True, "counts": {"items": deleted}}


def _hook(event):
    try:
        if event.get("hook") == "platform":
            return _platform(event)
        tenant_id = event.get("tenantId")
        if event.get("hook") == "tenant.purge" and isinstance(tenant_id, str) and tenant_id:
            return {"ok": True, **purge(tenant_id)}
    except ads.AdsError as exc:
        return {"ok": False, "status": exc.status, "code": exc.code.replace(" ", "_"),
                **({"field": exc.field} if exc.field else {})}
    return {"ok": False, "status": 400, "code": "unknown_hook"}


# --- dispatch -------------------------------------------------------------------------

_CONNECT = {"GET /tenant/meta-ads/connection", "POST /tenant/meta-ads/connection/start",
            "POST /tenant/meta-ads/connection/callback", "GET /tenant/meta-ads/assets",
            "PUT /tenant/meta-ads/connection", "DELETE /tenant/meta-ads/connection"}

ROUTES = {
    "GET /tenant/meta-ads/connection": get_connection,
    "POST /tenant/meta-ads/connection/start": start,
    "POST /tenant/meta-ads/connection/callback": callback,
    "GET /tenant/meta-ads/assets": assets,
    "PUT /tenant/meta-ads/connection": put_connection,
    "DELETE /tenant/meta-ads/connection": delete_connection,
    "POST /locations/{locationId}/meta-ads/copy": copy,
    "POST /locations/{locationId}/meta-ads/campaigns": create_campaign,
    "GET /locations/{locationId}/meta-ads/campaigns": list_campaigns,
    "GET /locations/{locationId}/meta-ads/campaigns/{campaignId}": get_campaign,
    "POST /locations/{locationId}/meta-ads/campaigns/{campaignId}/activate": activate,
    "POST /locations/{locationId}/meta-ads/campaigns/{campaignId}/pause": pause,
    "GET /locations/{locationId}/meta-ads/insights": insights,
}


def handler(event, context=None):
    if isinstance(event, dict) and "requestContext" not in event:
        if "hook" in event:
            return _hook(event)
        return {"ok": False, "status": 400, "code": "unknown_event"}
    method, path = http.route(event)
    route = f"{method} {path}"
    fn = ROUTES.get(route)
    if fn is None:
        return _err(404, "not found")
    try:
        location_id = None
        if "{locationId}" in path:
            try:
                location_id = http.path_id(event, "locationId")
            except ValueError:
                return _err(404, "not found")
        feature = ads.CONNECT_KEYS if route in _CONNECT else ads.FEATURE_KEY
        ctx = tenant.for_jwt(event, location_id=location_id, feature=feature, owner_only=True)
        return fn(event, ctx)
    except tenant.TenantError as exc:
        return exc.response()
    except ads.AdsError as exc:
        if " " in exc.code:
            return http.respond(exc.status, {"error": exc.code, **({"field": exc.field} if exc.field else {})})
        return _err(exc.status, exc.code, exc.field)
    except MetaGraphError as exc:
        logger.warning(json.dumps({"route": route, "meta": exc.code}))
        if exc.code == "invalid_code":
            return _err(400, "invalid_state", "code")
        return _err(502, "meta_unavailable")
    except ValueError as exc:
        return http.respond(400, {"error": str(exc)})
    except (BotoCoreError, ClientError) as exc:
        logger.error(json.dumps({"route": route, "aws": type(exc).__name__}))
        return _err(503, "service_unavailable")

"""Meta ads (feature A, ADR-A): shared logic for the meta-ads API Lambda
and the meta-ads-jobs Lambda (publish workflow tasks, nightly insights,
kill-switch sweep).

Table `<prefix>meta-ads` (META_ADS_TABLE_NAME), partition `TENANT#<t>`:

    CONNECTION                          status, assets, dsa, monthlyCapSek, paused
    OAUTH#<state>                       sub, ttl (+10 min)
    CAMPAIGN#<campaignId>               the campaign and its Meta ids
    INSIGHT#<campaignId>#<yyyy-mm-dd>   one day of Meta numbers + our attribution
    SPEND#<yyyy-mm>                     month to date, warnedAt / cappedAt
    COPYUSE#<yyyy-mm>                   Bedrock suggestions used this month

Platform settings live on the tenant table (`PLATFORM#META_ADS` /
`SETTINGS`). The tenant's Meta token is a Secrets Manager secret
`booqy/<env>/meta-ads/<tenantId>`, never logged, returned or evented.

Spend is bounded three ways (ADR-A 6): lifetime budgets with an end date
on every ad set, Booqy caps (campaign 100-20 000 SEK, owner monthly cap
under the platform ceiling) and the kill switch (platform-wide or per
tenant `paused`).
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from shared import dynamo
from shared import marketing as mkt

FEATURE_ID = "meta-ads"
FEATURE_KEY = "metaAds"
CONNECT_KEYS = ("metaAds", "socialPosting")  # A1-A6 accept either (ADR-A 10)

GOALS = ("bookings", "orders", "catering", "awareness")
STATUSES = ("publishing", "paused", "active", "completed", "failed", "rejected")
MIN_BUDGET_SEK, MAX_BUDGET_SEK = 100, 20_000
MIN_DAYS, MAX_DAYS = 3, 30
MIN_RADIUS_KM, MAX_RADIUS_KM = 1, 40
MAX_HEADLINE, MAX_PRIMARY = 40, 125
DEFAULT_MONTHLY_CAP_SEK = 3_000
DEFAULT_MAX_MONTHLY_CAP_SEK = 20_000
COPY_QUOTA_PER_MONTH = 200
OAUTH_TTL_MINUTES = 10
INSIGHT_TTL_DAYS = 25 * 31
COPYUSE_TTL_DAYS = 13 * 31
WARN_SHARE = Decimal("0.8")

ADS_SCOPES = ("ads_management", "ads_read", "business_management", "pages_show_list", "pages_read_engagement",
              "instagram_basic")
SOCIAL_SCOPES = ("pages_manage_posts", "instagram_content_publish")

# Meta objective per goal (Outcome-based objectives, v2023+).
OBJECTIVES = {"bookings": "OUTCOME_TRAFFIC", "orders": "OUTCOME_TRAFFIC", "catering": "OUTCOME_LEADS",
              "awareness": "OUTCOME_AWARENESS"}
CTAS = {"bookings": "BOOK_TRAVEL", "orders": "ORDER_NOW", "catering": "LEARN_MORE", "awareness": "LEARN_MORE"}
LANDING = {"bookings": "/boka-bord", "orders": "/bestall", "catering": "/catering", "awareness": "/"}

DEEP_LINKS = {
    "business": "https://business.facebook.com/overview",
    "ad_account": "https://business.facebook.com/settings/ad-accounts",
    "page": "https://www.facebook.com/pages/create",
    "instagram": "https://business.facebook.com/settings/instagram-account-v2s",
    "payment_method": "https://business.facebook.com/billing_hub/payment_settings",
}

_CLIENT_ID = re.compile(r"[0-9A-Za-z_-]{8,64}")


class AdsError(Exception):
    def __init__(self, status: int, code: str, field: Optional[str] = None):
        super().__init__(code)
        self.status, self.code, self.field = status, code, field


# --- basics ---------------------------------------------------------------------------

def table():
    return dynamo.table(os.environ["META_ADS_TABLE_NAME"])


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def month_of(day) -> str:
    return str(day)[:7]


def pk(tenant_id) -> str:
    return f"TENANT#{tenant_id}"


def connection_key(tenant_id):
    return {"PK": pk(tenant_id), "SK": "CONNECTION"}


def oauth_key(tenant_id, state):
    return {"PK": pk(tenant_id), "SK": f"OAUTH#{state}"}


def campaign_key(tenant_id, campaign_id):
    return {"PK": pk(tenant_id), "SK": f"CAMPAIGN#{campaign_id}"}


def insight_key(tenant_id, campaign_id, day):
    return {"PK": pk(tenant_id), "SK": f"INSIGHT#{campaign_id}#{day}"}


def spend_key(tenant_id, month):
    return {"PK": pk(tenant_id), "SK": f"SPEND#{month}"}


def copyuse_key(tenant_id, month):
    return {"PK": pk(tenant_id), "SK": f"COPYUSE#{month}"}


def valid_client_id(value) -> bool:
    return isinstance(value, str) and bool(_CLIENT_ID.fullmatch(value))


def conditional_failed(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def plain(value):
    """Decimal-free copy for JSON answers."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


def utm_campaign(campaign_id: str) -> str:
    return f"booqy-{campaign_id[:10].lower()}"


def landing_url(site_url: str, goal: str, campaign_id: str, channel: str = "facebook") -> str:
    """The ad's link with the UTM tags the cookieless counters read."""
    base = (site_url or "").rstrip("/") + LANDING[goal]
    return (f"{base}?utm_source={channel}&utm_medium=paid_social"
            f"&utm_campaign={utm_campaign(campaign_id)}")


# --- platform settings and kill switch ------------------------------------------------

PLATFORM_KEY = {"PK": "PLATFORM#META_ADS", "SK": "SETTINGS"}


def platform_settings() -> dict:
    item = dynamo.table(os.environ["TENANT_TABLE_NAME"]).get_item(Key=PLATFORM_KEY, ConsistentRead=True).get(
        "Item") or {}
    return {"killSwitch": bool(item.get("killSwitch")),
            "maxMonthlyCapSek": int(item.get("maxMonthlyCapSek") or DEFAULT_MAX_MONTHLY_CAP_SEK)}


def put_platform_settings(body: dict, operator_sub: Optional[str]) -> dict:
    if not isinstance(body, dict) or set(body) - {"killSwitch", "maxMonthlyCapSek"}:
        raise AdsError(400, "unsupported fields")
    kill, cap = body.get("killSwitch"), body.get("maxMonthlyCapSek")
    if not isinstance(kill, bool):
        raise AdsError(400, "invalid_kill_switch", "killSwitch")
    if not isinstance(cap, int) or isinstance(cap, bool) or not MIN_BUDGET_SEK <= cap <= 1_000_000:
        raise AdsError(400, "invalid_cap", "maxMonthlyCapSek")
    dynamo.table(os.environ["TENANT_TABLE_NAME"]).put_item(Item={
        **PLATFORM_KEY, "killSwitch": kill, "maxMonthlyCapSek": cap, "updatedAt": iso(now()),
        "updatedBy": operator_sub or "operator"})
    return {"killSwitch": kill, "maxMonthlyCapSek": cap}


def get_connection(tenant_id) -> Optional[dict]:
    return table().get_item(Key=connection_key(tenant_id), ConsistentRead=True).get("Item")


def blocked_by(tenant_id, connection=None) -> Optional[str]:
    """'kill_switch' when the platform switch or the operator's per-tenant
    pause is on, else None."""
    if platform_settings()["killSwitch"]:
        return "kill_switch"
    connection = connection if connection is not None else get_connection(tenant_id)
    if (connection or {}).get("paused"):
        return "kill_switch"
    return None


# --- money ----------------------------------------------------------------------------

def spend_this_month_ore(tenant_id, month=None) -> int:
    item = table().get_item(Key=spend_key(tenant_id, month or month_of(now().date())), ConsistentRead=True).get(
        "Item") or {}
    return int(item.get("spendOre") or 0)


def committed_ore(tenant_id, month=None, exclude=None) -> int:
    """Budget still promised this month: the unspent lifetime budgets of
    active campaigns (other than `exclude`) plus month-to-date spend. Used
    so a new or re-activated campaign cannot push the month over the cap."""
    month = month or month_of(now().date())
    total = spend_this_month_ore(tenant_id, month)
    for item in campaigns(tenant_id):
        if item["campaignId"] == exclude or item.get("status") != "active":
            continue
        spent = sum(int(i.get("spendOre") or 0) for i in insights(tenant_id, item["campaignId"]))
        total += max(0, int(item["budgetSek"]) * 100 - spent)
    return total


def check_cap(tenant_id, connection, budget_sek, exclude=None) -> None:
    cap_sek = int((connection or {}).get("monthlyCapSek") or DEFAULT_MONTHLY_CAP_SEK)
    cap_sek = min(cap_sek, platform_settings()["maxMonthlyCapSek"])
    if committed_ore(tenant_id, exclude=exclude) + int(budget_sek) * 100 > cap_sek * 100:
        raise AdsError(409, "cap_exceeded")


# --- campaigns and insights -----------------------------------------------------------

def _query_all(condition, **extra) -> list:
    tbl, items, kwargs = table(), [], {"KeyConditionExpression": condition, "ConsistentRead": True, **extra}
    while True:
        page = tbl.query(**kwargs)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def campaigns(tenant_id) -> list:
    return _query_all(Key("PK").eq(pk(tenant_id)) & Key("SK").begins_with("CAMPAIGN#"))


def get_campaign(tenant_id, campaign_id) -> Optional[dict]:
    return table().get_item(Key=campaign_key(tenant_id, campaign_id), ConsistentRead=True).get("Item")


def insights(tenant_id, campaign_id, first=None, last=None) -> list:
    cond = Key("PK").eq(pk(tenant_id))
    if first and last:
        cond = cond & Key("SK").between(f"INSIGHT#{campaign_id}#{first}", f"INSIGHT#{campaign_id}#{last}")
    else:
        cond = cond & Key("SK").begins_with(f"INSIGHT#{campaign_id}#")
    return _query_all(cond)


def campaign_view(item: dict) -> dict:
    out = {k: plain(item.get(k)) for k in ("campaignId", "locationId", "goal", "status", "budgetSek", "startDate",
                                            "endDate", "radiusKm", "utmCampaign", "version", "createdAt")}
    out["creative"] = plain(item.get("creative") or {})
    if item.get("error"):
        out["error"] = plain(item["error"])
    return out


def attribution(tenant_id, location_id, campaign_slug, day) -> dict:
    """Bookings, orders and revenue the cookieless counters attributed to
    this campaign's UTM tag on one day (read-only on the marketing table)."""
    name = os.environ.get("MARKETING_TABLE_NAME")
    out = {"bookings": 0, "orders": 0, "revenueOre": 0}
    if not name:
        return out
    rows = dynamo.table(name).query(
        KeyConditionExpression=Key("PK").eq(mkt.stats_pk(tenant_id))
        & Key("SK").begins_with(f"DAY#{day}#{location_id}#")).get("Items") or []
    wanted = mkt.slug(campaign_slug)
    for row in rows:
        if mkt.parse_stats_sk(row["SK"])["campaign"] == wanted:
            for field in out:
                out[field] += int(row.get(field) or 0)
    return out


def cost_per(spend_ore: int, count: int) -> Optional[int]:
    return round(spend_ore / 100 / count) if count else None


# --- Secrets Manager: the tenant's Meta token -----------------------------------------

def secret_name(tenant_id) -> str:
    return f"booqy/{os.environ['ENVIRONMENT']}/meta-ads/{tenant_id}"


def _secrets():
    return boto3.client("secretsmanager")


def put_token(tenant_id, token: str, scopes) -> None:
    value = json.dumps({"accessToken": token, "scopes": list(scopes or ())})
    client = _secrets()
    try:
        client.create_secret(Name=secret_name(tenant_id), SecretString=value,
                             Tags=[{"Key": "tenantId", "Value": tenant_id}, {"Key": "feature", "Value": FEATURE_ID}])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceExistsException":
            raise
        client.put_secret_value(SecretId=secret_name(tenant_id), SecretString=value)


def get_token(tenant_id):
    """The tenant's Token (shared.meta_graph.Token) or None."""
    from shared.meta_graph import Token
    try:
        raw = _secrets().get_secret_value(SecretId=secret_name(tenant_id))["SecretString"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("ResourceNotFoundException", "InvalidRequestException"):
            return None
        raise
    data = json.loads(raw)
    return Token(data["accessToken"], tuple(data.get("scopes") or ()))


def delete_token(tenant_id) -> None:
    try:
        _secrets().delete_secret(SecretId=secret_name(tenant_id), RecoveryWindowInDays=7)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in ("ResourceNotFoundException", "InvalidRequestException"):
            raise


def new_state() -> str:
    return secrets.token_urlsafe(24)


def end_date(start: date, days: int) -> date:
    return start + timedelta(days=days)


# --- clients (simulator unless META_CLIENT / BEDROCK_CLIENT = real) -------------------

_graph = None
_text = None


def graph():
    global _graph
    if _graph is None:
        from shared import meta_graph
        _graph = meta_graph.client()
    return _graph


def text_model():
    global _text
    if _text is None:
        from shared import bedrock
        _text = bedrock.client()
    return _text


def meta_ids(item) -> list:
    """Campaign, ad set and ad ids that exist on Meta for a campaign item."""
    meta = item.get("meta") or {}
    return [meta[k] for k in ("campaignId", "adSetId", "adId") if meta.get(k)]


def pause_campaign(tenant_id, item, by: str, token=None) -> Optional[dict]:
    """Pauses an active campaign on Meta and here. Returns the new item, or
    None when it was not active (someone else got there first)."""
    from shared import events
    token = token or get_token(tenant_id)
    if token is not None:
        for object_id in meta_ids(item):
            graph().set_status(token, object_id, "PAUSED")
    try:
        answer = table().update_item(
            Key=campaign_key(tenant_id, item["campaignId"]),
            UpdateExpression="SET #s = :paused, version = version + :one, pausedBy = :by, updatedAt = :at",
            ConditionExpression="#s = :active", ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":paused": "paused", ":active": "active", ":one": 1, ":by": by,
                                       ":at": iso(now())},
            ReturnValues="ALL_NEW")
    except ClientError as exc:
        if not conditional_failed(exc):
            raise
        return None
    try:
        events.publish(FEATURE_ID, "MetaCampaign.Paused", {"tenantId": tenant_id, "campaignId": item["campaignId"],
                                                           "by": by})
    except Exception:  # a lost event never fails the pause
        pass
    return answer["Attributes"]


def pause_all(tenant_id, by: str) -> int:
    token = get_token(tenant_id)
    count = 0
    for item in campaigns(tenant_id):
        if item.get("status") == "active" and pause_campaign(tenant_id, item, by, token):
            count += 1
    return count

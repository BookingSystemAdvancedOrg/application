"""tenant-purge

TRIGGER:
    EventBridge Scheduler (tenant_purge_schedule: nightly in production,
    hourly in dev). Input {} = a sweep. The function also invokes itself
    asynchronously to carry on when a run gets close to its timeout:
    {"sweep": true, "round": <n>}.

PURPOSE:
    Deletes an offboarded restaurant's data once its retention period is
    over (docs/MULTI_TENANCY.md, Offboarding). The date is the PROFILE row's
    purgeAt: written by the offboarding workflow (offboarded + the retention
    days), set to "now" by Booqy Console's "Delete customer", and filled in
    here for restaurants offboarded before this existed (offboardedAt +
    retention, but never sooner than BACKFILL_GRACE_DAYS from today, so an
    old offboarding isn't deleted the moment this ships).

    A sweep lists every tenant profile (tenant table GSI1, GSI1PK =
    "TENANT": the same list Booqy Console shows), so there is no per-tenant
    timer to keep in sync: changing the retention, postponing or cancelling
    is a change to one date. Only tenants still "offboarded" are touched.

    Deleted, per restaurant:
      * every LOCATION#<id> partition of the location-keyed tables
        (bookings, table holds, orders, menus, layouts, catering requests
        and discount tiers), the catering history of each request, and the
        location rows themselves
      * marketing: subscribers, templates, campaigns and their recipients,
        offers, codes, statistics, usage, queued sends, and the consent log
        (CONSENT_TABLE_NAME)
      * users: the Cognito users of the tenant pool and their profiles
      * menu images (menu-images/<tenantId>/ in the menu image bucket)
      * Kassa's fiskaly API key for the restaurant (the secret
        FISKALY_UNIT_PREFIX + tenantId, 7-day recovery window). The fiskaly
        UNIT itself, Kassa's journal and its receipts stay (bookkeeping,
        Skatteverket audits); fiskaly has no API to disable the key, so
        Booqy disables it in fiskaly HUB if wanted.
      * domains we ran DNS for: auto-renew is switched off for domains
        registered through us, and the hosted zone is deleted once the
        domain no longer points at it (a zone still in use is kept and
        reported, so the restaurant's email doesn't stop working)
      * the website repo site-<slug> on GitHub (site-repo "delete", when
        SITE_REPO_FUNCTION_ARN is set; prod also cleans up dev's side): a
        refusal (e.g. the GitHub App lacks Administration write) is
        reported and doesn't stop the purge
      * the tenant's own rows (audit log, domain orders), except domains
        that are still attached (reported: run domain-detach first)

    Kept: a small PROFILE tombstone (company name, org. and VAT number,
    slug, plan, dates, Stripe account id, the billing agreement without
    who changed it, status "purged") with no personal data: the next
    invoicing run still bills the days used before the customer left (a
    "Delete customer" right after offboarding), one activity log row about the website repo (written after the
    log is deleted), the SLUG# claim (Booqy Console's New customer takes
    over a purged customer's slug, with a fresh repo), the STRIPE_ACCOUNT# claim (the restaurant keeps its
    Stripe account), catering documents (7-year Object Lock, bookkeeping),
    and whatever Stripe holds. Point-in-time backups keep deleted rows for
    up to 35 days.

    Every step is idempotent: a run that stops halfway (timeout, error) is
    finished by the next one. Stream consumers only react to INSERT and
    MODIFY, so these deletes send no emails or SMS to guests.

    One summary email per run that did something (purged, scheduled or
    failed), through the alert topic.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, TENANT_LIST_INDEX_NAME,
    LOCATION_TABLE_NAME, USER_TABLE_NAME, USER_TENANT_INDEX_NAME,
    COGNITO_USER_POOL_ID, LOCATION_TABLE_NAMES (JSON list),
    CATERING_REQUESTS_TABLE_NAME, CATERING_REQUEST_HISTORY_TABLE_NAME,
    MARKETING_TABLE_NAME, CONSENT_TABLE_NAME, CONSENT_TENANT_INDEX_NAME,
    MENU_IMAGES_BUCKET_NAME, RETENTION_DAYS,
    BACKFILL_GRACE_DAYS, ALERT_TOPIC_ARN, PROTECTED_ZONE_IDS (JSON list),
    FISKALY_UNIT_PREFIX (booqy/<env>/fiskaly-unit/), SITE_REPO_FUNCTION_ARN
    (empty: site repos are off)

AWS RESOURCE ACCESS:
    Query/GetItem/UpdateItem/PutItem/DeleteItem/BatchWriteItem on the tenant
    table, Query + BatchWriteItem/DeleteItem on the location-keyed tables,
    the catering history, marketing, location and user tables (user table
    through its byTenant index), cognito-idp:AdminDeleteUser on the tenant
    pool, s3:ListBucket/DeleteObject under menu-images/, Route 53 record and
    zone deletion (never the platform's own zones), route53domains
    DisableDomainAutoRenew, secretsmanager:DeleteSecret on
    booqy/<env>/fiskaly-unit/*, sns:Publish on the alert topic, and
    lambda:InvokeFunction on itself (continuation) and on site-repo.
"""

import json
import logging
import os
import urllib.parse
import uuid
import urllib.request
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from shared import dynamo

logger = logging.getLogger()
logger.setLevel(logging.INFO)

STOP_MARGIN_MS = 60_000      # stop starting new work this long before the deadline
MAX_ROUNDS = 10              # continuation invocations per sweep
MARKETING_PARTS = ("STATS", "SUB", "SEEN", "TPL", "CMP", "USAGE", "OFFER", "CODE")
TOMBSTONE_FIELDS = ("PK", "SK", "GSI1PK", "GSI1SK", "tenantId", "name", "legalName", "orgNumber",
                    "vatNumber", "slug", "planId", "createdAt", "offboardingStartedAt",
                    "offboardingRequestedBy", "offboardedAt", "purgeAt", "entitlements")
REGISTERED_MODES = ("register", "transfer")
DOH_URL = "https://dns.google/resolve"

_clients = {}


class OutOfTime(Exception):
    """Raised between batches when the run is close to its deadline."""


def _client(name, region=None):
    key = (name, region)
    if key not in _clients:
        _clients[key] = boto3.client(name, region_name=region) if region else boto3.client(name)
    return _clients[key]


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _env_list(name):
    try:
        value = json.loads(os.environ.get(name) or "[]")
    except json.JSONDecodeError:
        return []
    return [v for v in value if isinstance(v, str) and v]


class Budget:
    def __init__(self, context):
        self._getter = getattr(context, "get_remaining_time_in_millis", None)

    def check(self):
        if self._getter is not None and self._getter() < STOP_MARGIN_MS:
            raise OutOfTime()


# --------------------------------------------------------------- DynamoDB
def _delete_keys(table_name, keys, budget):
    """BatchWriteItem in chunks of 25 (the table resource's batch writer
    resends unprocessed items itself)."""
    if not keys:
        return 0
    table = dynamo.table(table_name)
    for start in range(0, len(keys), 25):
        budget.check()
        with table.batch_writer() as batch:
            for key in keys[start:start + 25]:
                batch.delete_item(Key=key)
    return len(keys)


def _query_keys(table_name, condition, *, index=None, projection="PK, SK", filter_expression=None):
    table = dynamo.table(table_name)
    request = {"KeyConditionExpression": condition, "ProjectionExpression": projection}
    if index:
        request["IndexName"] = index
    if filter_expression is not None:
        request["FilterExpression"] = filter_expression
    while True:
        page = table.query(**request)
        yield from page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            return
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _delete_partition(table_name, pk, budget):
    keys = [{"PK": i["PK"], "SK": i["SK"]} for i in _query_keys(table_name, Key("PK").eq(pk))]
    return _delete_keys(table_name, keys, budget)


# ----------------------------------------------------------------- steps
def _purge_locations(tenant_id, budget, counts):
    location_table = os.environ["LOCATION_TABLE_NAME"]
    requests_table = os.environ["CATERING_REQUESTS_TABLE_NAME"]
    history_table = os.environ["CATERING_REQUEST_HISTORY_TABLE_NAME"]
    tables = _env_list("LOCATION_TABLE_NAMES")
    rows = list(_query_keys(location_table, Key("PK").eq(f"TENANT#{tenant_id}"), projection="PK, SK, locationId"))
    for row in rows:
        location_id = row.get("locationId") or str(row["SK"]).removeprefix("LOCATION#")
        pk = f"LOCATION#{location_id}"
        # Catering history is keyed by request, so read the request ids
        # before their rows go.
        for item in _query_keys(requests_table, Key("PK").eq(pk) & Key("SK").begins_with("REQUEST#")):
            counts["cateringHistory"] += _delete_partition(history_table, f"REQUEST#{str(item['SK'])[8:]}", budget)
        for name in tables:
            counts["locationRows"] += _delete_partition(name, pk, budget)
        # The location row last: a run that stops above finds it again.
        _delete_keys(location_table, [{"PK": row["PK"], "SK": row["SK"]}], budget)
        counts["locations"] += 1


def _purge_marketing(tenant_id, budget, counts):
    table_name = os.environ["MARKETING_TABLE_NAME"]
    campaigns = _query_keys(table_name, Key("PK").eq(f"TENANT#{tenant_id}#CMP") & Key("SK").begins_with("CMP#"))
    for campaign in campaigns:
        counts["marketing"] += _delete_partition(table_name, f"CMP#{tenant_id}#{str(campaign['SK'])[4:]}", budget)
    # DynamoDB refuses a FilterExpression on the sort key, so the queue
    # (small: only scheduled sends) is matched here.
    queued = [{"PK": i["PK"], "SK": i["SK"]} for i in _query_keys(table_name, Key("PK").eq("SCHEDULE"))
              if f"#{tenant_id}#" in str(i["SK"])]
    counts["marketing"] += _delete_keys(table_name, queued, budget)
    for part in MARKETING_PARTS:
        counts["marketing"] += _delete_partition(table_name, f"TENANT#{tenant_id}#{part}", budget)
    # The consent log (shared/consent.py), found through its byTenant index:
    # with the restaurant gone nobody can send, so nothing is kept.
    consent_table = os.environ.get("CONSENT_TABLE_NAME")
    if consent_table:
        rows = [{"PK": i["PK"], "SK": i["SK"]} for i in _query_keys(
            consent_table, Key("tenantId").eq(tenant_id), index=os.environ.get("CONSENT_TENANT_INDEX_NAME", "byTenant"))]
        counts["marketing"] += _delete_keys(consent_table, rows, budget)


def _purge_users(tenant_id, budget, counts):
    table_name = os.environ["USER_TABLE_NAME"]
    pool = os.environ["COGNITO_USER_POOL_ID"]
    idp = _client("cognito-idp")
    users = list(_query_keys(table_name, Key("tenantId").eq(tenant_id), index=os.environ["USER_TENANT_INDEX_NAME"],
                             projection="PK, SK, cognitoSub, email"))
    for user in users:
        budget.check()
        username = user.get("cognitoSub") or user.get("email")
        if username:
            try:
                idp.admin_delete_user(UserPoolId=pool, Username=username)
            except idp.exceptions.UserNotFoundException:
                pass
        _delete_keys(table_name, [{"PK": user["PK"], "SK": user["SK"]}], budget)
        counts["users"] += 1


def _purge_images(tenant_id, budget, counts):
    bucket = os.environ.get("MENU_IMAGES_BUCKET_NAME")
    if not bucket:
        return
    s3 = _client("s3")
    prefix = f"menu-images/{tenant_id}/"
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        budget.check()
        objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if objects:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objects, "Quiet": True})
            counts["images"] += len(objects)


def _purge_fiskaly_key(tenant_id, counts):
    """Kassa's fiskaly key of the restaurant. Gone or already scheduled for
    deletion counts as done."""
    prefix = os.environ.get("FISKALY_UNIT_PREFIX")
    if not prefix:
        return
    try:
        _client("secretsmanager").delete_secret(SecretId=prefix + tenant_id, RecoveryWindowInDays=7)
        counts["fiskalyKey"] = 1
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("ResourceNotFoundException", "InvalidRequestException"):
            raise


def _ns_lookup(domain):
    """The domain's public name servers (lower case, no trailing dot), or
    None when the lookup itself failed."""
    query = urllib.parse.urlencode({"name": domain, "type": "NS"})
    try:
        with urllib.request.urlopen(f"{DOH_URL}?{query}", timeout=5) as response:
            data = json.loads(response.read())
    except (OSError, ValueError):
        return None
    if data.get("Status") not in (0, 3):
        return None
    return {str(a.get("data", "")).lower().rstrip(".") for a in data.get("Answer", []) if a.get("type") == 2}


def _delete_zone(zone_id, budget):
    r53 = _client("route53")
    try:
        zone = r53.get_hosted_zone(Id=zone_id)["HostedZone"]
    except r53.exceptions.NoSuchHostedZone:
        return
    apex = zone["Name"]
    changes = []
    for page in r53.get_paginator("list_resource_record_sets").paginate(HostedZoneId=zone_id):
        for record in page["ResourceRecordSets"]:
            if record["Name"] == apex and record["Type"] in ("NS", "SOA"):
                continue
            changes.append({"Action": "DELETE", "ResourceRecordSet": record})
    for start in range(0, len(changes), 100):
        budget.check()
        r53.change_resource_record_sets(HostedZoneId=zone_id, ChangeBatch={"Changes": changes[start:start + 100]})
    try:
        r53.delete_hosted_zone(Id=zone_id)
    except r53.exceptions.NoSuchHostedZone:
        pass


def _purge_zones(tenant_id, zones, budget, report):
    protected = set(_env_list("PROTECTED_ZONE_IDS"))
    domains = _client("route53domains", "us-east-1")
    kept = []
    for row in zones:
        budget.check()
        domain = row.get("domain") or str(row["SK"]).removeprefix("ZONE#")
        if row.get("mode") in REGISTERED_MODES:
            try:
                domains.disable_domain_auto_renew(DomainName=domain)
                report["autoRenewOff"].append(domain)
            except ClientError as e:
                report["notes"].append(f"{domain}: auto-renew not switched off ({e.response['Error']['Code']})")
        zone_id = str(row.get("hostedZoneId") or "").split("/")[-1]
        if zone_id and zone_id in protected:
            report["notes"].append(f"{domain}: hosted zone {zone_id} is a platform zone, not deleted")
            kept.append(row)
            continue
        if zone_id:
            ours = {str(ns).lower().rstrip(".") for ns in row.get("nameServers") or []}
            live = _ns_lookup(domain)
            if live is None or (ours and live & ours):
                why = "still points at it" if live else "name servers could not be checked"
                report["zonesKept"].append(f"{domain} ({why})")
                kept.append(row)
                continue
            _delete_zone(zone_id, budget)
            report["zonesDeleted"].append(domain)
        _release_claim(tenant_id, domain)
    return kept


def _release_claim(tenant_id, host):
    table = dynamo.table(os.environ["TENANT_TABLE_NAME"])
    try:
        table.delete_item(Key={"PK": f"DOMAIN#{host}", "SK": "TENANT"},
                          ConditionExpression=Attr("tenantId").eq(tenant_id))
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


def _delete_site_repo(tenant_id, report):
    """site-repo "delete". Returns the activity log entry (action, details),
    or None when site repos are off."""
    arn = os.environ.get("SITE_REPO_FUNCTION_ARN")
    if not arn:
        return None
    try:
        response = _client("lambda").invoke(
            FunctionName=arn, Payload=json.dumps({"action": "delete", "tenantId": tenant_id}).encode())
        body = json.loads(response["Payload"].read() or b"{}")
        if response.get("FunctionError"):
            raise RuntimeError(body.get("errorMessage") or "site-repo failed")
    except Exception as e:  # reported; the data is purged either way
        error = (str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}")[:500]
        report["notes"].append(f"Website repo NOT deleted: {error}")
        return "site_repo_delete_failed", {"error": error}
    if body.get("status") == "kept":
        report["notes"].append(f"Website repo {body.get('repo')} {body.get('status')}"
                               + (f" ({body['reason']})" if body.get("reason") else ""))
    return ("site_repo_deleted" if body.get("repoDeleted") else "site_repo_not_deleted"), \
        {k: body[k] for k in ("repo", "status", "reason") if body.get(k)}


def _audit(tenant_id, action, details, now):
    at = iso(now)
    dynamo.table(os.environ["TENANT_TABLE_NAME"]).put_item(Item={
        "PK": f"TENANT#{tenant_id}", "SK": f"AUDIT#{at}#{uuid.uuid4().hex[:12]}", "action": action,
        "by": "tenant-purge", "at": at, "details": details})


def _purge_tenant_rows(tenant_id, budget, report):
    """Every TENANT#<t> row except PROFILE. Zones are handled first (a zone
    still in use keeps its row, so the operator can see it); attached
    domains are left alone and reported."""
    table_name = os.environ["TENANT_TABLE_NAME"]
    pk = f"TENANT#{tenant_id}"
    rows = []
    table = dynamo.table(table_name)
    request = {"KeyConditionExpression": Key("PK").eq(pk)}
    while True:
        page = table.query(**request)
        rows.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    zones = [r for r in rows if str(r["SK"]).startswith("ZONE#")]
    kept_zones = {r["SK"] for r in _purge_zones(tenant_id, zones, budget, report)}
    doomed = []
    for row in rows:
        sk = str(row["SK"])
        if sk == "PROFILE" or sk in kept_zones:
            continue
        if sk.startswith("DOMAIN#"):
            report["notes"].append(f"{sk[7:]} is still attached (status {row.get('status')}): run domain-detach")
            continue
        doomed.append({"PK": row["PK"], "SK": row["SK"]})
    _delete_keys(table_name, doomed, budget)


def _tombstone(profile, now):
    stone = {k: profile[k] for k in TOMBSTONE_FIELDS if k in profile}
    account = (profile.get("stripe") or {}).get("accountId")
    if account:
        stone["stripe"] = {"accountId": account}
    if isinstance(profile.get("billing"), dict):
        # The agreement: the last invoice (the days used before leaving) is
        # made on the next 25th, which may come after the purge.
        stone["billing"] = {k: v for k, v in profile["billing"].items() if not k.endswith("By")}
    stone.update(status="purged", locationCount=0, purgedAt=iso(now), updatedAt=iso(now))
    return stone


def purge_tenant(tenant_id, budget):
    """Deletes one tenant's data. Returns a report, or None when the tenant
    isn't due (status changed, date moved)."""
    table = dynamo.table(os.environ["TENANT_TABLE_NAME"])
    key = {"PK": f"TENANT#{tenant_id}", "SK": "PROFILE"}
    profile = table.get_item(Key=key, ConsistentRead=True).get("Item")
    now = now_utc()
    purge_at = parse_iso((profile or {}).get("purgeAt"))
    if not profile or profile.get("status") != "offboarded" or not purge_at or purge_at > now:
        return None
    counts = {k: 0 for k in ("locations", "locationRows", "cateringHistory", "marketing", "users", "images",
                             "fiskalyKey")}
    report = {"tenantId": tenant_id, "name": profile.get("name"), "counts": counts, "autoRenewOff": [],
              "zonesDeleted": [], "zonesKept": [], "notes": []}
    logger.info(json.dumps({"purge": tenant_id, "purgeAt": profile.get("purgeAt")}))
    table.update_item(Key=key, UpdateExpression="SET purgeStartedAt = if_not_exists(purgeStartedAt, :n)",
                      ExpressionAttributeValues={":n": iso(now)})
    _purge_locations(tenant_id, budget, counts)
    _purge_marketing(tenant_id, budget, counts)
    _purge_users(tenant_id, budget, counts)
    _purge_images(tenant_id, budget, counts)
    _purge_fiskaly_key(tenant_id, counts)
    repo = _delete_site_repo(tenant_id, report)        # before the rows: it reads PROFILE and SITE
    _purge_tenant_rows(tenant_id, budget, report)
    if repo:
        _audit(tenant_id, *repo, now_utc())
    budget.check()
    stone = _tombstone(profile, now_utc())
    stone["purgeSummary"] = {**counts, "zonesKept": report["zonesKept"], "notes": report["notes"]}
    try:
        table.put_item(Item=stone, ConditionExpression=Attr("status").eq("offboarded"))
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    logger.info(json.dumps({"purged": tenant_id, **counts}, default=str))
    return report


# ----------------------------------------------------------------- sweep
def _profiles():
    table = dynamo.table(os.environ["TENANT_TABLE_NAME"])
    request = {"IndexName": os.environ.get("TENANT_LIST_INDEX_NAME", "GSI1"),
               "KeyConditionExpression": Key("GSI1PK").eq("TENANT"),
               "ProjectionExpression": "tenantId, #s, purgeAt, offboardedAt, #n",
               "ExpressionAttributeNames": {"#s": "status", "#n": "name"}}
    while True:
        page = table.query(**request)
        yield from page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            return
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _schedule(profile, now):
    """Sets purgeAt on an offboarded tenant that has none. Returns the date."""
    retention = timedelta(days=int(os.environ.get("RETENTION_DAYS", "90")))
    grace = timedelta(days=int(os.environ.get("BACKFILL_GRACE_DAYS", "7")))
    offboarded = parse_iso(profile.get("offboardedAt")) or now
    when = max(offboarded + retention, now + grace)
    try:
        dynamo.table(os.environ["TENANT_TABLE_NAME"]).update_item(
            Key={"PK": f"TENANT#{profile['tenantId']}", "SK": "PROFILE"},
            UpdateExpression="SET purgeAt = :p",
            ConditionExpression=Attr("purgeAt").not_exists() & Attr("status").eq("offboarded"),
            ExpressionAttributeValues={":p": iso(when)})
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise
    return when


def _continue(context, event):
    arn = getattr(context, "invoked_function_arn", None)
    if not arn:
        return False
    _client("lambda").invoke(FunctionName=arn, InvocationType="Event", Payload=json.dumps(event).encode())
    return True


def _notify(result):
    topic = os.environ.get("ALERT_TOPIC_ARN")
    if not topic or not (result["purged"] or result["scheduled"] or result["failed"]):
        return
    env_name = os.environ.get("ENVIRONMENT", "dev")
    lines = []
    for r in result["purged"]:
        c = r["counts"]
        lines.append(f"Deleted: {r['name']} ({r['tenantId']}): {c['locations']} location(s), "
                     f"{c['locationRows']} booking/order/menu/layout/catering rows, {c['cateringHistory']} catering "
                     f"history rows, {c['marketing']} marketing rows, {c['users']} user(s), {c['images']} image(s).")
        for label, items in (("Auto-renew off", r["autoRenewOff"]), ("Hosted zones deleted", r["zonesDeleted"]),
                             ("Hosted zones kept", r["zonesKept"]), ("Check", r["notes"])):
            if items:
                lines.append(f"  {label}: {', '.join(items)}")
    for s in result["scheduled"]:
        lines.append(f"Scheduled: {s['name']} ({s['tenantId']}) is deleted on {s['purgeAt'][:10]}.")
    for f in result["failed"]:
        lines.append(f"FAILED: {f['tenantId']}: {f['error']} (the next run tries again)")
    subject = f"{env_name}: tenant data {'deletion FAILED' if result['failed'] else 'deletion'}"
    try:
        _client("sns").publish(TopicArn=topic, Subject=subject[:100], Message="\n".join(lines) + "\n\n"
                               "Kept: a tombstone without personal data, catering documents and the "
                               "Stripe account. Backups keep deleted rows for up to 35 days.")
    except ClientError:
        logger.exception("could not send the summary email")


def handler(event, context):
    event = event if isinstance(event, dict) else {}
    budget = Budget(context)
    now = now_utc()
    result = {"purged": [], "scheduled": [], "failed": [], "continued": False}
    due = []
    for profile in _profiles():
        if profile.get("status") != "offboarded" or not profile.get("tenantId"):
            continue
        if not profile.get("purgeAt"):
            when = _schedule(profile, now)
            if when:
                result["scheduled"].append({"tenantId": profile["tenantId"], "name": profile.get("name"),
                                            "purgeAt": iso(when)})
            continue
        purge_at = parse_iso(profile["purgeAt"])
        if purge_at and purge_at <= now:
            due.append(profile["tenantId"])
    for tenant_id in sorted(due):
        try:
            budget.check()
            report = purge_tenant(tenant_id, budget)
            if report:
                result["purged"].append(report)
        except OutOfTime:
            round_ = int(event.get("round", 0)) + 1
            if round_ <= MAX_ROUNDS:
                result["continued"] = _continue(context, {"sweep": True, "round": round_})
            else:
                result["failed"].append({"tenantId": tenant_id, "error": "ran out of time"})
            break
        except Exception as e:  # one tenant's failure must not stop the others
            logger.exception("purge failed for %s", tenant_id)
            result["failed"].append({"tenantId": tenant_id, "error": f"{type(e).__name__}: {e}"[:300]})
    _notify(result)
    summary = {"purged": [r["tenantId"] for r in result["purged"]], "scheduled": len(result["scheduled"]),
               "failed": [f["tenantId"] for f in result["failed"]], "continued": result["continued"]}
    logger.info(json.dumps(summary))
    return summary

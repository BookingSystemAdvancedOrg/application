"""tenant-purge: deletes an offboarded restaurant's data once purgeAt has
passed, leaves every other tenant alone, and keeps only a tombstone."""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from shared import dynamo as shared_dynamo

APP = Path(__file__).parents[1] / "functions" / "tenant-purge" / "app.py"
REGION = "eu-north-1"
NOW = datetime(2026, 10, 20, 2, 0, tzinfo=timezone.utc)
GONE = "01gggggggggggggggggggggggg"     # offboarded, due
KEEP = "01kkkkkkkkkkkkkkkkkkkkkkkk"     # active neighbour
LOC_GONE, LOC_KEEP = "loc-gone", "loc-keep"
BUCKET = "test-menu-images"
LOCATION_TABLES = ["test-reservation", "test-order", "test-menu", "test-catering-requests"]


def _table(res, name, gsis=()):
    attrs = [{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}]
    indexes = []
    for index, hash_key, range_key in gsis:
        attrs.append({"AttributeName": hash_key, "AttributeType": "S"})
        schema = [{"AttributeName": hash_key, "KeyType": "HASH"}]
        if range_key:
            attrs.append({"AttributeName": range_key, "AttributeType": "S"})
            schema.append({"AttributeName": range_key, "KeyType": "RANGE"})
        indexes.append({"IndexName": index, "KeySchema": schema, "Projection": {"ProjectionType": "ALL"}})
    res.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
                     AttributeDefinitions=attrs, **({"GlobalSecondaryIndexes": indexes} if indexes else {}))
    return res.Table(name)


class FakeDomains:
    def __init__(self):
        self.auto_renew_off = []

    def disable_domain_auto_renew(self, DomainName):
        self.auto_renew_off.append(DomainName)


class FakeClient:
    def __init__(self):
        self.calls = []

    def publish(self, **kwargs):
        self.calls.append(kwargs)

    def invoke(self, **kwargs):
        self.calls.append(kwargs)


class Context:
    invoked_function_arn = "arn:aws:lambda:eu-north-1:123456789012:function:dev-tenant-purge:live"

    def __init__(self, remaining=[600_000]):
        self.remaining = list(remaining)

    def get_remaining_time_in_millis(self):
        return self.remaining.pop(0) if len(self.remaining) > 1 else self.remaining[0]


@pytest.fixture
def env(monkeypatch):
    values = {
        "ENVIRONMENT": "dev", "AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t", "AWS_SESSION_TOKEN": "t",
        "AWS_DEFAULT_REGION": REGION, "TENANT_TABLE_NAME": "test-tenant", "TENANT_LIST_INDEX_NAME": "GSI1",
        "LOCATION_TABLE_NAME": "test-location", "USER_TABLE_NAME": "test-user", "USER_TENANT_INDEX_NAME": "byTenant",
        "LOCATION_TABLE_NAMES": '["test-reservation", "test-order", "test-menu", "test-catering-requests"]',
        "CATERING_REQUESTS_TABLE_NAME": "test-catering-requests",
        "CATERING_REQUEST_HISTORY_TABLE_NAME": "test-catering-history", "MARKETING_TABLE_NAME": "test-marketing",
        "MENU_IMAGES_BUCKET_NAME": BUCKET, "RETENTION_DAYS": "90", "BACKFILL_GRACE_DAYS": "7",
        "ALERT_TOPIC_ARN": "arn:aws:sns:eu-north-1:123456789012:alerts", "PROTECTED_ZONE_IDS": "[]",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        res = boto3.resource("dynamodb", region_name=REGION)
        tables = {
            "tenant": _table(res, "test-tenant", [("GSI1", "GSI1PK", "GSI1SK")]),
            "location": _table(res, "test-location"),
            "user": _table(res, "test-user", [("byTenant", "tenantId", None)]),
            "history": _table(res, "test-catering-history"),
            "marketing": _table(res, "test-marketing"),
            **{name: _table(res, name) for name in LOCATION_TABLES},
        }
        idp = boto3.client("cognito-idp", region_name=REGION)
        pool = idp.create_user_pool(PoolName="tenants")["UserPool"]["Id"]
        monkeypatch.setenv("COGNITO_USER_POOL_ID", pool)
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})

        spec = importlib.util.spec_from_file_location("tenant_purge_app", APP)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "now_utc", lambda: NOW)
        fakes = {"domains": FakeDomains(), "sns": FakeClient(), "lambda": FakeClient()}
        module._clients[("route53domains", "us-east-1")] = fakes["domains"]
        module._clients[("sns", None)] = fakes["sns"]
        module._clients[("lambda", None)] = fakes["lambda"]
        monkeypatch.setattr(module, "_ns_lookup", lambda domain: set())

        yield {"app": module, "t": tables, "idp": idp, "pool": pool, "s3": s3,
               "r53": boto3.client("route53", region_name=REGION), **fakes}
        shared_dynamo._resource = None
        shared_dynamo._client = None


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def seed_tenant(e, tenant_id, location_id, *, status="active", purge_at=None, offboarded_at=None, slug="x"):
    t = e["t"]
    profile = {"PK": f"TENANT#{tenant_id}", "SK": "PROFILE", "GSI1PK": "TENANT", "GSI1SK": slug,
               "tenantId": tenant_id, "name": f"Restaurang {slug}", "legalName": f"{slug} AB",
               "orgNumber": "556677-8899", "slug": slug, "status": status, "planId": "standard",
               "contactEmail": "owner@example.se", "contactPhone": "+46701234567",
               "address": {"street": "Gatan 1", "postalCode": "12345", "city": "Stockholm"},
               "branding": {"logoUrl": "https://example.se/logo.png"}, "ownerEmail": "owner@example.se",
               "stripe": {"accountId": f"acct_{slug}", "chargesEnabled": True}, "locationCount": 1,
               "entitlements": {"maxLocations": 3, "features": {"reservations": True}}, "createdAt": "2025-01-01T00:00:00Z"}
    if purge_at:
        profile["purgeAt"] = _iso(purge_at)
    if offboarded_at:
        profile["offboardedAt"] = _iso(offboarded_at)
    t["tenant"].put_item(Item=profile)
    t["tenant"].put_item(Item={"PK": f"SLUG#{slug}", "SK": "TENANT", "tenantId": tenant_id})
    t["tenant"].put_item(Item={"PK": f"STRIPE_ACCOUNT#acct_{slug}", "SK": "TENANT", "tenantId": tenant_id})
    t["tenant"].put_item(Item={"PK": f"TENANT#{tenant_id}", "SK": "AUDIT#2026-01-01#1", "by": "op"})
    t["location"].put_item(Item={"PK": f"TENANT#{tenant_id}", "SK": f"LOCATION#{location_id}",
                                 "tenantId": tenant_id, "locationId": location_id})
    pk = f"LOCATION#{location_id}"
    for i in range(30):   # more than one batch
        t["test-reservation"].put_item(Item={"PK": pk, "SK": f"RESERVATION#2026-05-01#{i:03d}",
                                             "customerEmail": "guest@example.se"})
    t["test-order"].put_item(Item={"PK": pk, "SK": "ORDER#1", "customerPhone": "+46700000000"})
    t["test-menu"].put_item(Item={"PK": pk, "SK": "ITEM#1"})
    t["test-catering-requests"].put_item(Item={"PK": pk, "SK": "REQUEST#req-" + slug})
    t["test-catering-requests"].put_item(Item={"PK": pk, "SK": "TIER#1"})
    t["history"].put_item(Item={"PK": f"REQUEST#req-{slug}", "SK": "OFFER#v0001"})
    t["history"].put_item(Item={"PK": f"REQUEST#req-{slug}", "SK": "AUDIT#1"})
    m = t["marketing"]
    m.put_item(Item={"PK": f"TENANT#{tenant_id}#SUB", "SK": "SUB#abc", "email": "guest@example.se"})
    m.put_item(Item={"PK": f"TENANT#{tenant_id}#CMP", "SK": "CMP#c1"})
    m.put_item(Item={"PK": f"CMP#{tenant_id}#c1", "SK": "TO#abc"})
    m.put_item(Item={"PK": f"TENANT#{tenant_id}#STATS", "SK": "DAY#2026-05-01#x#web#-"})
    m.put_item(Item={"PK": "SCHEDULE", "SK": f"2026-11-01T10:00:00Z#{tenant_id}#c1"})
    sub = e["idp"].admin_create_user(UserPoolId=e["pool"], Username=f"owner-{slug}@example.se",
                                     MessageAction="SUPPRESS")["User"]["Username"]
    t["user"].put_item(Item={"PK": f"USER#{sub}", "SK": "PROFILE", "tenantId": tenant_id, "cognitoSub": sub,
                             "email": f"owner-{slug}@example.se"})
    for i in range(3):
        e["s3"].put_object(Bucket=BUCKET, Key=f"menu-images/{tenant_id}/locations/{location_id}/menu/{i}.jpg",
                           Body=b"x")


def count(table, pk):
    return len(table.query(KeyConditionExpression="PK = :p", ExpressionAttributeValues={":p": pk})["Items"])


def images(e, tenant_id):
    return e["s3"].list_objects_v2(Bucket=BUCKET, Prefix=f"menu-images/{tenant_id}/").get("KeyCount", 0)


def users(e):
    return {u["Username"] for u in e["idp"].list_users(UserPoolId=e["pool"])["Users"]}


def test_due_tenant_is_purged_and_neighbour_untouched(env):
    seed_tenant(env, GONE, LOC_GONE, status="offboarded", purge_at=NOW - timedelta(minutes=1),
                offboarded_at=NOW - timedelta(days=90), slug="gone")
    seed_tenant(env, KEEP, LOC_KEEP, slug="keep")
    t = env["t"]

    result = env["app"].handler({}, Context())
    assert result["purged"] == [GONE] and result["failed"] == []

    for name in LOCATION_TABLES:
        assert count(t[name], f"LOCATION#{LOC_GONE}") == 0
        assert count(t[name], f"LOCATION#{LOC_KEEP}") > 0
    assert count(t["history"], "REQUEST#req-gone") == 0 and count(t["history"], "REQUEST#req-keep") == 2
    assert count(t["location"], f"TENANT#{GONE}") == 0 and count(t["location"], f"TENANT#{KEEP}") == 1
    for pk in (f"TENANT#{GONE}#SUB", f"TENANT#{GONE}#CMP", f"CMP#{GONE}#c1", f"TENANT#{GONE}#STATS"):
        assert count(t["marketing"], pk) == 0
    assert count(t["marketing"], f"TENANT#{KEEP}#SUB") == 1
    schedule = [i["SK"] for i in t["marketing"].query(KeyConditionExpression="PK = :p",
                                                      ExpressionAttributeValues={":p": "SCHEDULE"})["Items"]]
    assert schedule == [f"2026-11-01T10:00:00Z#{KEEP}#c1"]
    assert users(env) == {"owner-keep@example.se"}
    assert count(t["user"], "USER#owner-gone@example.se") == 0
    assert images(env, GONE) == 0 and images(env, KEEP) == 3

    # tombstone: company facts and dates, no personal data
    stone = t["tenant"].get_item(Key={"PK": f"TENANT#{GONE}", "SK": "PROFILE"})["Item"]
    assert stone["status"] == "purged" and stone["purgedAt"] == "2026-10-20T02:00:00Z"
    assert stone["orgNumber"] == "556677-8899" and stone["slug"] == "gone" and stone["GSI1PK"] == "TENANT"
    assert stone["stripe"] == {"accountId": "acct_gone"} and stone["locationCount"] == 0
    assert stone["entitlements"]["maxLocations"] == 3
    for field in ("contactEmail", "contactPhone", "address", "branding", "ownerEmail"):
        assert field not in stone
    assert count(t["tenant"], f"TENANT#{GONE}") == 1                      # audit rows gone
    assert count(t["tenant"], "SLUG#gone") == 1                           # slug stays reserved
    assert count(t["tenant"], "STRIPE_ACCOUNT#acct_gone") == 1
    assert t["tenant"].get_item(Key={"PK": f"TENANT#{KEEP}", "SK": "PROFILE"})["Item"]["status"] == "active"

    mail = env["sns"].calls
    assert len(mail) == 1 and "Deleted: Restaurang gone" in mail[0]["Message"]

    # nothing left to do: a second run is a no-op and sends nothing
    assert env["app"].handler({}, Context())["purged"] == []
    assert len(env["sns"].calls) == 1


def test_not_due_and_not_offboarded_are_left_alone(env):
    seed_tenant(env, GONE, LOC_GONE, status="offboarded", purge_at=NOW + timedelta(days=1), slug="gone")
    seed_tenant(env, KEEP, LOC_KEEP, status="suspended", purge_at=NOW - timedelta(days=1), slug="keep")
    assert env["app"].handler({}, Context())["purged"] == []
    assert count(env["t"]["test-reservation"], f"LOCATION#{LOC_GONE}") == 30
    assert count(env["t"]["test-reservation"], f"LOCATION#{LOC_KEEP}") == 30
    assert env["sns"].calls == []


def test_offboarded_without_date_gets_one_with_a_grace_period(env):
    seed_tenant(env, GONE, LOC_GONE, status="offboarded", offboarded_at=NOW - timedelta(days=200), slug="old")
    seed_tenant(env, KEEP, LOC_KEEP, status="offboarded", offboarded_at=NOW - timedelta(days=10), slug="new")
    result = env["app"].handler({}, Context())
    assert result["purged"] == [] and result["scheduled"] == 2
    get = lambda tid: env["t"]["tenant"].get_item(Key={"PK": f"TENANT#{tid}", "SK": "PROFILE"})["Item"]
    assert get(GONE)["purgeAt"] == "2026-10-27T02:00:00Z"                 # long ago: today + 7 days
    assert get(KEEP)["purgeAt"] == _iso(NOW - timedelta(days=10) + timedelta(days=90)).replace(".000", "")
    assert "Scheduled: Restaurang old" in env["sns"].calls[0]["Message"]
    assert count(env["t"]["test-reservation"], f"LOCATION#{LOC_GONE}") == 30


def test_domains_auto_renew_off_and_unused_zone_deleted(env, monkeypatch):
    seed_tenant(env, GONE, LOC_GONE, status="offboarded", purge_at=NOW - timedelta(hours=1), slug="gone")
    r53, t = env["r53"], env["t"]
    zones = {}
    for domain in ("bought.se", "connected.se"):
        z = r53.create_hosted_zone(Name=domain, CallerReference=domain)
        zones[domain] = (z["HostedZone"]["Id"].split("/")[-1], z["DelegationSet"]["NameServers"])
        r53.change_resource_record_sets(HostedZoneId=zones[domain][0], ChangeBatch={"Changes": [
            {"Action": "CREATE", "ResourceRecordSet": {"Name": f"www.{domain}", "Type": "CNAME", "TTL": 300,
                                                       "ResourceRecords": [{"Value": "d1.cloudfront.net"}]}}]})
    t["tenant"].put_item(Item={"PK": f"TENANT#{GONE}", "SK": "ZONE#bought.se", "domain": "bought.se",
                               "mode": "register", "hostedZoneId": zones["bought.se"][0],
                               "nameServers": zones["bought.se"][1]})
    t["tenant"].put_item(Item={"PK": f"TENANT#{GONE}", "SK": "ZONE#connected.se", "domain": "connected.se",
                               "mode": "connect", "hostedZoneId": zones["connected.se"][0],
                               "nameServers": zones["connected.se"][1]})
    t["tenant"].put_item(Item={"PK": "DOMAIN#bought.se", "SK": "TENANT", "tenantId": GONE})
    t["tenant"].put_item(Item={"PK": f"TENANT#{GONE}", "SK": "DOMAIN#www.stuck.se", "status": "failed"})
    # connected.se still delegates to our zone (their email lives there)
    live = {"connected.se": {ns.lower() for ns in zones["connected.se"][1]}}
    monkeypatch.setattr(env["app"], "_ns_lookup", lambda d: live.get(d, {"ns1.other-registrar.se"}))

    assert env["app"].handler({}, Context())["purged"] == [GONE]

    assert env["domains"].auto_renew_off == ["bought.se"]
    remaining = {z["Name"].rstrip(".") for z in r53.list_hosted_zones()["HostedZones"]}
    assert remaining == {"connected.se"}
    assert count(t["tenant"], "DOMAIN#bought.se") == 0
    rows = {i["SK"] for i in t["tenant"].query(KeyConditionExpression="PK = :p",
                                               ExpressionAttributeValues={":p": f"TENANT#{GONE}"})["Items"]}
    assert rows == {"PROFILE", "ZONE#connected.se", "DOMAIN#www.stuck.se"}
    message = env["sns"].calls[0]["Message"]
    assert "Hosted zones kept: connected.se (still points at it)" in message
    assert "www.stuck.se is still attached" in message


def test_protected_zone_is_never_deleted(env, monkeypatch):
    seed_tenant(env, GONE, LOC_GONE, status="offboarded", purge_at=NOW - timedelta(hours=1), slug="gone")
    z = env["r53"].create_hosted_zone(Name="booqy.se", CallerReference="p")["HostedZone"]["Id"].split("/")[-1]
    monkeypatch.setenv("PROTECTED_ZONE_IDS", f'["{z}"]')
    env["t"]["tenant"].put_item(Item={"PK": f"TENANT#{GONE}", "SK": "ZONE#booqy.se", "domain": "booqy.se",
                                      "mode": "connect", "hostedZoneId": f"/hostedzone/{z}"})
    env["app"].handler({}, Context())
    assert len(env["r53"].list_hosted_zones()["HostedZones"]) == 1


def test_running_out_of_time_continues_and_finishes(env):
    seed_tenant(env, GONE, LOC_GONE, status="offboarded", purge_at=NOW - timedelta(hours=1), slug="gone")
    # enough time for the first checks, then close to the deadline
    result = env["app"].handler({}, Context(remaining=[600_000, 600_000, 600_000, 1_000]))
    assert result["continued"] is True and result["purged"] == []
    invoke = env["lambda"].calls[0]
    assert invoke["FunctionName"].endswith(":live") and invoke["InvocationType"] == "Event"
    assert b'"round": 1' in invoke["Payload"]
    # the continuation finishes the job
    assert env["app"].handler({"sweep": True, "round": 1}, Context())["purged"] == [GONE]
    assert count(env["t"]["test-reservation"], f"LOCATION#{LOC_GONE}") == 0


def test_one_failure_does_not_stop_the_others(env, monkeypatch):
    seed_tenant(env, GONE, LOC_GONE, status="offboarded", purge_at=NOW - timedelta(hours=1), slug="gone")
    seed_tenant(env, KEEP, LOC_KEEP, status="offboarded", purge_at=NOW - timedelta(hours=1), slug="keep")
    original = env["app"]._purge_images

    def broken(tenant_id, budget, counts):
        if tenant_id == GONE:
            raise RuntimeError("S3 is down")
        return original(tenant_id, budget, counts)

    monkeypatch.setattr(env["app"], "_purge_images", broken)
    result = env["app"].handler({}, Context())
    assert result["purged"] == [KEEP] and result["failed"] == [GONE]
    assert "FAILED" in env["sns"].calls[0]["Subject"]
    assert env["t"]["tenant"].get_item(Key={"PK": f"TENANT#{GONE}", "SK": "PROFILE"})["Item"]["status"] == "offboarded"


def test_kassa_fiskaly_key_is_deleted_and_the_neighbours_kept(env, monkeypatch):
    monkeypatch.setenv("FISKALY_UNIT_PREFIX", "booqy/dev/fiskaly-unit/")
    sm = boto3.client("secretsmanager", region_name=REGION)
    for tenant in (GONE, KEEP):
        sm.create_secret(Name=f"booqy/dev/fiskaly-unit/{tenant}", SecretString='{"key": "k", "secret": "s"}')
    seed_tenant(env, GONE, LOC_GONE, status="offboarded", purge_at=NOW - timedelta(minutes=1),
                offboarded_at=NOW - timedelta(days=90), slug="gone")
    seed_tenant(env, KEEP, LOC_KEEP, slug="keep")
    assert env["app"].handler({}, Context())["purged"] == [GONE]
    assert "DeletedDate" in sm.describe_secret(SecretId=f"booqy/dev/fiskaly-unit/{GONE}")
    assert "DeletedDate" not in sm.describe_secret(SecretId=f"booqy/dev/fiskaly-unit/{KEEP}")
    stone = env["t"]["tenant"].get_item(Key={"PK": f"TENANT#{GONE}", "SK": "PROFILE"})["Item"]
    assert stone["purgeSummary"]["fiskalyKey"] == 1


def test_no_fiskaly_key_is_fine(env, monkeypatch):
    monkeypatch.setenv("FISKALY_UNIT_PREFIX", "booqy/dev/fiskaly-unit/")
    seed_tenant(env, GONE, LOC_GONE, status="offboarded", purge_at=NOW - timedelta(minutes=1),
                offboarded_at=NOW - timedelta(days=90), slug="gone")
    result = env["app"].handler({}, Context())
    assert result["purged"] == [GONE] and result["failed"] == []

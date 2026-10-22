"""insights-rollup (nightly rows) and insights (GET /insights): only the
caller's own tenant, staff without amounts, today live, a backfill for a
location that has no rows yet. DynamoDB is moto."""

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from shared import dynamo as shared_dynamo
from tenant_support import (LOC_A, LOC_A2, LOC_B, OWNER_A_SUB, REGION, STAFF_A_SUB, TENANT_A, TENANT_B,
                            TENANT_TABLE, LOCATION_TABLE_DEFAULT, location_row, seed_tenancy, tenant_claims,
                            tenant_row)

ROOT = Path(__file__).parents[1] / "functions"
NOW = datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)  # 12:00 in Stockholm, a Wednesday
TABLES = {
    "INSIGHTS_TABLE_NAME": "test-insights",
    "RESERVATION_TABLE_NAME": "test-reservation",
    "ORDER_TABLE_NAME": "test-order",
    "CATERING_REQUESTS_TABLE_NAME": "test-catering",
    "PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME": "test-layout",
    "MARKETING_TABLE_NAME": "test-marketing",
}
HOURS = {d: [{"opensAt": "11:00", "closesAt": "22:00"}] for d in
         ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")}


def _load(name, module_name):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / name / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _loc(tenant_id, location_id, **extra):
    return location_row(tenant_id, location_id, timezone="Europe/Stockholm", businessHours=HOURS,
                        createdAt="2026-10-01T08:00:00Z", **extra)


def booking(loc, day, rid, start="18:00", end="20:00", party=2, status="reserved", **extra):
    return {"PK": f"LOCATION#{loc}", "SK": f"RESERVATION#{day}#{rid}", "date": day, "startTime": start,
            "endTime": end, "partySize": party, "status": status, "tableIds": ["t1"], "source": "online",
            "createdAt": f"{day}T06:00:00Z", "customerPhone": "+46700000000", "customerName": "Gäst", **extra}


def paid_order(loc, day, oid, total, hour_utc=10):
    return {"PK": f"LOCATION#{loc}", "SK": f"ORDER#{day}#{oid}", "paymentStatus": "paid", "status": "completed",
            "mode": "pickup", "createdAt": f"{day}T{hour_utc:02d}:00:00Z", "totalAmount": total,
            "lines": [{"menuItemId": "m1", "name": "Pizza", "quantity": 1, "total": total}]}


@pytest.fixture
def env(monkeypatch):
    for key, value in {"ENVIRONMENT": "dev", "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                       "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": REGION,
                       "USER_TABLE_NAME": "test-user", "TENANT_LIST_INDEX_NAME": "GSI1", **TABLES}.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        res = boto3.resource("dynamodb", region_name=REGION)
        for name in TABLES.values():
            res.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                             KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                                        {"AttributeName": "SK", "KeyType": "RANGE"}],
                             AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                                   {"AttributeName": "SK", "AttributeType": "S"}])
        seed_tenancy(monkeypatch,
                     tenant_a=tenant_row(TENANT_A, GSI1PK="TENANT", GSI1SK=TENANT_A),
                     tenant_b=tenant_row(TENANT_B, GSI1PK="TENANT", GSI1SK=TENANT_B),
                     locations_a=[_loc(TENANT_A, LOC_A), _loc(TENANT_A, LOC_A2, lunchUntil="16:00")],
                     locations_b=[_loc(TENANT_B, LOC_B)])
        # The tenant table needs the GSI1 the rollup lists tenants with.
        boto3.client("dynamodb", region_name=REGION).update_table(
            TableName=TENANT_TABLE,
            AttributeDefinitions=[{"AttributeName": "GSI1PK", "AttributeType": "S"},
                                  {"AttributeName": "GSI1SK", "AttributeType": "S"}],
            GlobalSecondaryIndexUpdates=[{"Create": {
                "IndexName": "GSI1", "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"},
                                                   {"AttributeName": "GSI1SK", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"}}}])
        for loc in (LOC_A, LOC_A2, LOC_B):
            res.Table(TABLES["PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME"]).put_item(Item={
                "PK": f"LOCATION#{loc}", "SK": "LAYOUT#v1", "version": 1, "isCurrent": True,
                "elements": [{"elementId": "t1", "type": "table", "seats": 4}]})
        reservations = res.Table(TABLES["RESERVATION_TABLE_NAME"])
        reservations.put_item(Item=booking(LOC_A, "2026-10-19", "r1", party=4))
        reservations.put_item(Item=booking(LOC_A, "2026-10-20", "r2", party=2, status="no_show"))
        reservations.put_item(Item=booking(LOC_A, "2026-10-21", "r3", start="12:00", end="14:00", party=3))
        reservations.put_item(Item=booking(LOC_B, "2026-10-20", "rb", party=8))
        orders = res.Table(TABLES["ORDER_TABLE_NAME"])
        orders.put_item(Item=paid_order(LOC_A, "2026-10-20", "o1", 25000))
        orders.put_item(Item=paid_order(LOC_A, "2026-10-21", "o2", 12000, hour_utc=9))
        orders.put_item(Item=paid_order(LOC_B, "2026-10-20", "ob", 99000))
        yield res
        shared_dynamo._resource = None
        shared_dynamo._client = None


@pytest.fixture
def rollup(env):
    module = _load("insights-rollup", "insights_rollup_app")
    module.now_utc = lambda: NOW
    return module


@pytest.fixture
def api(env):
    module = _load("insights", "insights_app")
    module.now_utc = lambda: NOW
    return module


def event(query, *, tenant_id=TENANT_A, role="owner_user"):
    sub = OWNER_A_SUB if role == "owner_user" else STAFF_A_SUB
    return {"routeKey": "GET /insights", "queryStringParameters": query,
            "requestContext": {"http": {"method": "GET", "path": "/insights"},
                               "authorizer": {"jwt": {"claims": tenant_claims(sub, f'["{role}"]', tenant_id)}}}}


def body(response):
    return json.loads(response["body"])


# --- insights-rollup ------------------------------------------------------------------

def test_first_run_backfills_from_the_location_creation(rollup, env):
    summary = rollup.handler({}, None)
    rows = env.Table(TABLES["INSIGHTS_TABLE_NAME"]).query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("PK").eq(f"LOCATION#{LOC_A}")
        & boto3.dynamodb.conditions.Key("SK").begins_with("DAY#"))["Items"]
    days = sorted(r["date"] for r in rows)
    assert days[0] == "2026-10-01" and days[-1] == "2026-10-20"  # never today
    assert summary["failed"] == []
    job = env.Table(TABLES["INSIGHTS_TABLE_NAME"]).get_item(Key={"PK": f"LOCATION#{LOC_A}", "SK": "JOB#latest"})["Item"]
    assert (job["from"], job["to"]) == ("2026-10-01", "2026-10-20")
    row = next(r for r in rows if r["date"] == "2026-10-20")
    assert row["b"]["18|online"]["ns"] == 1
    assert "customerPhone" not in json.dumps(row, default=str) and "+4670" not in json.dumps(row, default=str)


def test_later_runs_recompute_only_the_last_days(rollup, env):
    rollup.handler({}, None)
    env.Table(TABLES["INSIGHTS_TABLE_NAME"]).delete_item(Key={"PK": f"LOCATION#{LOC_A}", "SK": "DAY#2026-10-02"})
    env.Table(TABLES["INSIGHTS_TABLE_NAME"]).delete_item(Key={"PK": f"LOCATION#{LOC_A}", "SK": "DAY#2026-10-19"})
    rollup.handler({}, None)
    table = env.Table(TABLES["INSIGHTS_TABLE_NAME"])
    assert "Item" not in table.get_item(Key={"PK": f"LOCATION#{LOC_A}", "SK": "DAY#2026-10-02"})
    assert "Item" in table.get_item(Key={"PK": f"LOCATION#{LOC_A}", "SK": "DAY#2026-10-19"})


def test_window_catches_up_after_missed_nights(rollup):
    location = {"timezone": "Europe/Stockholm", "createdAt": "2025-01-01T00:00:00Z"}
    assert rollup.window(location, {"to": "2026-10-01"}, NOW, {}) == ("2026-10-02", "2026-10-20")
    assert rollup.window(location, {"to": "2026-10-19"}, NOW, {}) == ("2026-10-13", "2026-10-20")
    assert rollup.window(location, None, NOW, {})[0] == "2025-09-21"  # 395 days
    assert rollup.window(location, {"to": "x"}, NOW, {"from": "2026-09-01", "to": "2026-12-01"}) \
        == ("2026-09-01", "2026-10-20")


# --- GET /insights -----------------------------------------------------------------------

def test_owner_gets_all_own_locations_with_today_live(rollup, api):
    rollup.handler({}, None)
    out = body(api.handler(event({"from": "2026-10-19", "to": "2026-10-21", "compare": "none"}), None))
    cur = out["current"]
    # LOC_A: 4 + 2 + 3 guests (today live), never tenant B's 8
    assert (cur["bookings"], cur["guests"]) == (3, 9)
    assert cur["orderRevenue"] == 37000
    assert {loc["locationId"] for loc in out["locations"]} == {LOC_A, LOC_A2}
    assert out["money"] is True and out["pending"] is False


def test_location_of_another_tenant_is_not_found(rollup, api):
    rollup.handler({}, None)
    response = api.handler(event({"from": "2026-10-19", "to": "2026-10-21", "locationId": LOC_B}), None)
    assert response["statusCode"] == 404


def test_compare_with_the_previous_period(rollup, api):
    rollup.handler({}, None)
    out = body(api.handler(event({"from": "2026-10-21", "to": "2026-10-21", "locationId": LOC_A}), None))
    assert out["compare"] == {"mode": "previous", "from": "2026-10-20", "to": "2026-10-20"}
    assert out["previous"]["noShows"] == 1 and out["current"]["guests"] == 3


def test_staff_see_their_location_without_amounts(rollup, api):
    rollup.handler({}, None)
    out = body(api.handler(event({"from": "2026-10-19", "to": "2026-10-21"}, role="staff_user"), None))
    assert out["money"] is False
    assert [loc["locationId"] for loc in out["locations"]] == [LOC_A]
    text = json.dumps(out["current"])
    assert '"orderRevenue"' not in text and '"revenue"' not in text and '"avgOrder"' not in text
    assert out["current"]["orders"] == 2
    other = api.handler(event({"from": "2026-10-19", "to": "2026-10-21", "locationId": LOC_A2},
                              role="staff_user"), None)
    assert other["statusCode"] == 404


def test_owner_can_switch_staff_access_off(api, env):
    env.Table(TENANT_TABLE).update_item(Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"},
                                        UpdateExpression="SET insights = :i",
                                        ExpressionAttributeValues={":i": {"staffAccess": "none"}})
    response = api.handler(event({"from": "2026-10-19", "to": "2026-10-21"}, role="staff_user"), None)
    assert response["statusCode"] == 403 and body(response)["error"] == "staff_no_access"


def test_not_rolled_up_yet_is_computed_live_and_flagged(api):
    out = body(api.handler(event({"from": "2026-10-19", "to": "2026-10-21", "locationId": LOC_A}), None))
    assert out["pending"] is True and out["current"]["guests"] == 9


def test_service_filter_uses_each_locations_split(rollup, api):
    rollup.handler({}, None)
    lunch = body(api.handler(event({"from": "2026-10-19", "to": "2026-10-21", "locationId": LOC_A,
                                    "service": "lunch", "compare": "none"}), None))["current"]
    assert (lunch["bookings"], lunch["guests"]) == (1, 3)
    assert lunch["orders"] == 2  # 11:00 and 12:00 local


@pytest.mark.parametrize("query", [
    {"from": "2026-10-21"},
    {"from": "2026-10-22", "to": "2026-10-21"},
    {"from": "2024-01-01", "to": "2026-10-21"},
    {"from": "2026-10-01", "to": "2026-10-21", "service": "brunch"},
    {"from": "2026-10-01", "to": "2026-10-21", "compare": "lastweek"},
])
def test_bad_queries_are_refused(api, query):
    assert api.handler(event(query), None)["statusCode"] == 400

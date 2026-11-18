"""functions/tenant-setup: the facts it reads, the one notice when Kom igång
is finished (audit, NOTICES row, event, email), reopening never notifies
again, skips, owner only, and the sweep."""

import json

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from moto import mock_aws

from tenant_support import (LOC_A, LOC_A2, OWNER_A_SUB, REGION, STAFF_A_SUB, TENANT_A, TENANT_TABLE, load_fn,
                            seed_tenancy, tenant_row, with_tenant_claims)

TABLES = {"MENU_TABLE_NAME": "test-menu", "PRINT_TABLE_NAME": "test-print",
          "PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME": "test-layout", "GOOGLE_BUSINESS_TABLE_NAME": "test-gbp",
          "PHONE_AGENT_TABLE_NAME": "test-phone", "USER_TABLE_NAME": "test-user"}
HOURS = {"monday": [{"opensAt": "11:00", "closesAt": "22:00"}], "tuesday": [], "wednesday": [], "thursday": [],
         "friday": [], "saturday": [], "sunday": []}
FEATURES = {"reservations": True, "ordering": True, "catering": False, "googleBusiness": True, "phoneAgent": True}


def _pk_sk(ddb, name, gsi=None):
    attrs = [{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}]
    kwargs = {}
    if gsi:
        attrs += [{"AttributeName": a, "AttributeType": "S"} for a in gsi[1:]]
        kwargs["GlobalSecondaryIndexes"] = [{"IndexName": gsi[0], "Projection": {"ProjectionType": "ALL"},
                                             "KeySchema": [{"AttributeName": gsi[1], "KeyType": "HASH"},
                                                           {"AttributeName": gsi[2], "KeyType": "RANGE"}]}]
    ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST", AttributeDefinitions=attrs,
                     KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                                {"AttributeName": "SK", "KeyType": "RANGE"}], **kwargs)


@pytest.fixture
def env(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
        monkeypatch.setenv("ENVIRONMENT", "dev")
        monkeypatch.setenv("CONSOLE_APP_URL", "https://ops.example")
        ddb = boto3.client("dynamodb", region_name=REGION)
        # the sweep reads GSI1 (GSI1PK = TENANT) like the console's list
        _pk_sk(ddb, TENANT_TABLE, ("GSI1", "GSI1PK", "GSI1SK"))
        seed_tenancy(monkeypatch, existing=(TENANT_TABLE,),
                     tenant_a=tenant_row(TENANT_A, features=FEATURES, name="Bistro A", GSI1PK="TENANT",
                                         GSI1SK=TENANT_A))
        for name, value in TABLES.items():
            monkeypatch.setenv(name, value)
            if name != "USER_TABLE_NAME":
                _pk_sk(ddb, value)
        sns = boto3.client("sns", region_name=REGION)
        topic = sns.create_topic(Name="alerts")["TopicArn"]
        sqs = boto3.client("sqs", region_name=REGION)
        queue = sqs.create_queue(QueueName="alerts-inbox")["QueueUrl"]
        arn = sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=arn)
        monkeypatch.setenv("ALERT_TOPIC_ARN", topic)
        events = boto3.client("events", region_name=REGION)
        events.create_event_bus(Name="test-bus")
        monkeypatch.setenv("EVENT_BUS_NAME", "test-bus")
        app = load_fn("tenant-setup", "tenant_setup_app")
        published = []
        monkeypatch.setattr(app.events, "publish", lambda *a, **k: published.append(a) or "evt-1")
        yield app, sqs, queue, published


def res():
    return boto3.resource("dynamodb", region_name=REGION)


def call(app, route, body=None, role="owner_user", sub=OWNER_A_SUB):
    event = with_tenant_claims({"routeKey": route, **({"body": json.dumps(body)} if body is not None else {})},
                               TENANT_A, role, sub)
    out = app.handler(event)
    return out["statusCode"], json.loads(out["body"])


def finish_everything():
    """Every step done at both of tenant A's locations."""
    t = res().Table(TENANT_TABLE)
    t.update_item(Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"},
                  UpdateExpression="SET dpa = :d, stripe = :s",
                  ExpressionAttributeValues={":d": {"version": "2026-10-v1", "acceptedAt": "2026-11-01T10:00:00Z"},
                                             ":s": {"accountId": "acct_1", "chargesEnabled": True}})
    for lid in (LOC_A, LOC_A2):
        res().Table("test-location").update_item(Key={"PK": f"TENANT#{TENANT_A}", "SK": f"LOCATION#{lid}"},
                                                 UpdateExpression="SET businessHours = :h",
                                                 ExpressionAttributeValues={":h": HOURS})
        res().Table("test-menu").put_item(Item={"PK": f"LOCATION#{lid}", "SK": "MENU#1", "active": True})
        res().Table("test-print").put_item(Item={"PK": f"LOCATION#{lid}", "SK": "DEVICE#d1", "name": "Kök"})
        res().Table("test-layout").put_item(Item={"PK": f"LOCATION#{lid}", "SK": "LAYOUT#ACTIVATION",
                                                  "currentVersion": 2})
        res().Table("test-gbp").put_item(Item={"PK": f"LOCATION#{lid}", "SK": "LINK", "tenantId": TENANT_A})
        res().Table("test-phone").put_item(Item={
            "PK": f"TENANT#{TENANT_A}", "SK": f"LOC#{lid}#CONFIG", "enabled": True, "mode": "always",
            "number": {"e164": "+46100000001"}, "setup": {"forwardedAt": "x", "testedAt": "y"}})
    res().Table("test-gbp").put_item(Item={"PK": f"TENANT#{TENANT_A}", "SK": "CONNECTION", "status": "connected"})
    res().Table("test-phone").put_item(Item={"PK": f"TENANT#{TENANT_A}", "SK": "CONFIG", "version": 3})


def profile():
    return res().Table(TENANT_TABLE).get_item(Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"})["Item"]


def notices():
    return res().Table(TENANT_TABLE).query(KeyConditionExpression=Key("PK").eq("NOTICES"))["Items"]


def audit(action):
    rows = res().Table(TENANT_TABLE).query(
        KeyConditionExpression=Key("PK").eq(f"TENANT#{TENANT_A}") & Key("SK").begins_with("AUDIT#"))["Items"]
    return [r for r in rows if r["action"] == action]


def emails(sqs, queue):
    msgs = sqs.receive_message(QueueUrl=queue, MaxNumberOfMessages=10).get("Messages", [])
    return [json.loads(m["Body"]) for m in msgs]


def test_new_restaurant_is_open_and_recorded(env):
    app, sqs, queue, published = env
    status, body = call(app, "GET /tenant/setup")
    assert status == 200
    assert body["complete"] is False and body["completedAt"] is None
    assert body["total"] == 9
    # the staff user seeded on tenant A counts
    assert next(s for s in body["steps"] if s["id"] == "staff")["state"] == "done"
    hours = next(s for s in body["steps"] if s["id"] == "hours")
    assert {p["locationId"] for p in hours["locations"]} == {LOC_A, LOC_A2}
    state = profile()["setupState"]
    assert state["total"] == 9 and state["complete"] is False
    assert "setupCompletedAt" not in profile()
    assert notices() == [] and published == [] and emails(sqs, queue) == []


def test_finishing_notifies_exactly_once(env):
    app, sqs, queue, published = env
    finish_everything()
    status, body = call(app, "GET /tenant/setup")
    assert status == 200 and body["complete"] is True and body["completedAt"]
    first = body["completedAt"]
    assert profile()["setupCompletedAt"] == first
    [notice] = notices()
    assert notice["kind"] == "setup_completed" and notice["tenantName"] == "Bistro A"
    assert len(audit("setup_completed")) == 1
    assert published == [("tenant-setup", "Tenant.SetupCompleted",
                          {"tenantId": TENANT_A, "completedAt": first, "stepsDone": 9, "stepsSkipped": 0})]
    [mail] = emails(sqs, queue)
    assert "Bistro A" in mail["Subject"] and "/tenants/" + TENANT_A in mail["Message"]
    # again, and once more by the sweep: nothing new
    call(app, "GET /tenant/setup")
    app.handler({"setupTask": "sweep"})
    assert len(notices()) == 1 and len(audit("setup_completed")) == 1 and len(published) == 1
    assert emails(sqs, queue) == []
    assert profile()["setupCompletedAt"] == first


def test_reopening_only_updates_the_state(env):
    app, sqs, queue, published = env
    finish_everything()
    call(app, "GET /tenant/setup")
    first = profile()["setupCompletedAt"]
    res().Table("test-menu").delete_item(Key={"PK": f"LOCATION#{LOC_A}", "SK": "MENU#1"})
    _, body = call(app, "GET /tenant/setup")
    assert body["complete"] is False and body["completedAt"] == first
    assert profile()["setupState"]["open"] == 1
    res().Table("test-menu").put_item(Item={"PK": f"LOCATION#{LOC_A}", "SK": "MENU#2", "active": True})
    _, body = call(app, "GET /tenant/setup")
    assert body["complete"] is True and profile()["setupCompletedAt"] == first
    assert len(notices()) == 1 and len(published) == 1


def test_skips_finish_steps_and_can_be_undone(env):
    app, *_ = env
    finish_everything()
    res().Table(TENANT_TABLE).update_item(Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"},
                                          UpdateExpression="REMOVE stripe")
    _, body = call(app, "GET /tenant/setup")
    assert body["complete"] is False
    status, body = call(app, "PUT /tenant/setup/skips", {"step": "stripe", "skipped": True})
    assert status == 200 and body["complete"] is True and body["skipped"] == 1
    assert next(s for s in body["steps"] if s["id"] == "stripe")["skippedHere"] is True
    assert len(audit("setup_step_skipped")) == 1
    status, body = call(app, "PUT /tenant/setup/skips", {"step": "stripe", "skipped": False})
    assert status == 200 and body["complete"] is False and body["completedAt"]
    assert len(notices()) == 1


def test_location_skip_needs_own_location(env):
    app, *_ = env
    status, body = call(app, "PUT /tenant/setup/skips", {"step": "menu", "locationId": LOC_A, "skipped": True})
    assert status == 200
    menu = next(s for s in body["steps"] if s["id"] == "menu")
    assert [p["state"] for p in menu["locations"] if p["locationId"] == LOC_A] == ["skipped"]
    assert call(app, "PUT /tenant/setup/skips", {"step": "menu", "locationId": "loc-b-1", "skipped": True})[0] == 404
    assert call(app, "PUT /tenant/setup/skips", {"step": "menu", "skipped": True})[0] == 400
    assert call(app, "PUT /tenant/setup/skips", {"step": "dpa", "skipped": True})[0] == 400
    assert call(app, "PUT /tenant/setup/skips", {"step": "nope", "skipped": True})[0] == 400
    assert call(app, "PUT /tenant/setup/skips", {"step": "stripe", "locationId": LOC_A, "skipped": True})[0] == 400


def test_owner_only(env):
    app, *_ = env
    assert call(app, "GET /tenant/setup", role="staff_user", sub=STAFF_A_SUB)[0] == 403
    assert call(app, "PUT /tenant/setup/skips", {"step": "staff", "skipped": True},
                role="staff_user", sub=STAFF_A_SUB)[0] == 403


def test_sweep_records_progress_and_completion(env):
    app, sqs, queue, published = env
    out = app.handler({"setupTask": "sweep"})
    assert out["checked"] == 1 and out["completed"] == 0
    assert profile()["setupState"]["complete"] is False
    finish_everything()
    out = app.handler({"setupTask": "sweep"})
    assert out["completed"] == 1
    [notice] = notices()
    assert notice["tenantId"] == TENANT_A
    assert audit("setup_completed")[0]["by"] == "system:setup-sweep"


def test_printers_and_google_own_skips(env):
    app, *_ = env
    finish_everything()
    for lid in (LOC_A, LOC_A2):
        res().Table("test-print").delete_item(Key={"PK": f"LOCATION#{lid}", "SK": "DEVICE#d1"})
        res().Table("test-print").put_item(Item={"PK": f"LOCATION#{lid}", "SK": "SETTINGS", "printerSetup": "skipped"})
        res().Table("test-gbp").delete_item(Key={"PK": f"LOCATION#{lid}", "SK": "LINK"})
        res().Table("test-gbp").put_item(Item={"PK": f"LOCATION#{lid}", "SK": "SKIP", "tenantId": TENANT_A})
    _, body = call(app, "GET /tenant/setup")
    states = {s["id"]: s["state"] for s in body["steps"]}
    assert states["printers"] == "skipped" and states["google"] == "skipped"
    assert body["complete"] is True

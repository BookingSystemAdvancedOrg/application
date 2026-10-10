"""reservation-reminders: marks due bookings with a reminder notice exactly once."""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from shared import dynamo as shared_dynamo
from shared import reservations as shared_r
from shared import tenant as shared_tenant
from tenant_support import (LOC_A, LOC_B, REGION, TENANT_A, TENANT_B, TENANT_TABLE, location_row,
                            seed_tenancy, tenant_row)

APP = Path(__file__).parents[1] / "functions" / "reservation-reminders" / "app.py"
RES_TABLE = "test-reservation"
NOW = datetime(2026, 9, 19, 16, 0, tzinfo=timezone.utc)  # 18:00 in Stockholm


@pytest.fixture
def env(monkeypatch):
    for key, value in {"ENVIRONMENT": "dev", "AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t",
                       "AWS_SESSION_TOKEN": "t", "AWS_DEFAULT_REGION": REGION,
                       "RESERVATION_TABLE_NAME": RES_TABLE}.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        res = boto3.resource("dynamodb", region_name=REGION)
        res.create_table(TableName=RES_TABLE, BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                                    {"AttributeName": "SK", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                               {"AttributeName": "SK", "AttributeType": "S"}])
        seed_tenancy(
            monkeypatch,
            locations_a=[location_row(TENANT_A, LOC_A, timezone="Europe/Stockholm")],
            locations_b=[location_row(TENANT_B, LOC_B, timezone="Europe/Stockholm")],
        )
        monkeypatch.setattr(shared_r, "now_utc", lambda: NOW)
        spec = importlib.util.spec_from_file_location("reminders_app", APP)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module, res.Table(RES_TABLE), res.Table(TENANT_TABLE)
        shared_dynamo._resource = None
        shared_dynamo._client = None
        shared_tenant.reset_caches()


def put(table, rid, *, starts, created=NOW - timedelta(days=3), location=LOC_A, tenant_id=TENANT_A, **extra):
    local = starts.astimezone(__import__("zoneinfo").ZoneInfo("Europe/Stockholm"))
    item = {
        "PK": f"LOCATION#{location}", "SK": f"RESERVATION#{local.date().isoformat()}#{rid}",
        "reservationId": rid, "tenantId": tenant_id, "locationId": location,
        "date": local.date().isoformat(), "startTime": local.strftime("%H:%M"),
        "bookedFor": shared_r.iso(starts), "createdAt": shared_r.iso(created),
        "status": "reserved", "customerEmail": "g@example.se", **extra,
    }
    table.put_item(Item={k: v for k, v in item.items() if v is not None})
    return item


def get(table, item):
    return table.get_item(Key={"PK": item["PK"], "SK": item["SK"]})["Item"]


def test_due_bookings_get_one_reminder_notice(env):
    module, table, _ = env
    tomorrow = put(table, "a" * 32, starts=NOW + timedelta(hours=20))          # next day, local date + 1
    soon = put(table, "b" * 32, starts=NOW + timedelta(minutes=30))           # inside 1 h: too late
    later = put(table, "c" * 32, starts=NOW + timedelta(hours=30))            # outside 24 h
    fresh = put(table, "d" * 32, starts=NOW + timedelta(hours=5), created=NOW - timedelta(hours=1))
    arrived = put(table, "e" * 32, starts=NOW + timedelta(hours=3), status="arrived")
    no_contact = put(table, "f" * 32, starts=NOW + timedelta(hours=3), customerEmail=None)
    phone_only = put(table, "0" * 32, starts=NOW + timedelta(hours=3), customerEmail=None,
                     customerPhone="+46701234567")

    assert module.handler({}, None) == {"reminders": 2}

    marked = get(table, tomorrow)
    assert marked["reminderSentAt"] == shared_r.iso(NOW) and marked["notice"]["type"] == "reminder"
    assert get(table, phone_only)["notice"]["type"] == "reminder"
    for item in (soon, later, fresh, arrived, no_contact):
        assert "reminderSentAt" not in get(table, item)

    first_id = marked["notice"]["id"]
    assert module.handler({}, None) == {"reminders": 0}
    assert get(table, tomorrow)["notice"]["id"] == first_id


def test_tenant_setting_changes_the_window_and_zero_turns_it_off(env):
    module, table, tenants = env
    tenants.put_item(Item=tenant_row(TENANT_A, notifications={"reminderHours": 2}))
    tenants.put_item(Item=tenant_row(TENANT_B, notifications={"reminderHours": 0}))
    shared_tenant.reset_caches()
    in_90_min = put(table, "a" * 32, starts=NOW + timedelta(minutes=90))
    in_3_h = put(table, "b" * 32, starts=NOW + timedelta(hours=3))
    other_tenant = put(table, "c" * 32, starts=NOW + timedelta(hours=3), location=LOC_B, tenant_id=TENANT_B)

    assert module.handler({}, None) == {"reminders": 1}
    assert "reminderSentAt" in get(table, in_90_min)
    assert "reminderSentAt" not in get(table, in_3_h) and "reminderSentAt" not in get(table, other_tenant)


def test_inactive_tenant_or_no_reservations_feature_is_skipped(env):
    module, table, tenants = env
    tenants.put_item(Item=tenant_row(TENANT_A, status="suspended"))
    tenants.put_item(Item=tenant_row(TENANT_B, features={"reservations": False}))
    shared_tenant.reset_caches()
    put(table, "a" * 32, starts=NOW + timedelta(hours=5))
    put(table, "b" * 32, starts=NOW + timedelta(hours=5), location=LOC_B, tenant_id=TENANT_B)
    assert module.handler({}, None) == {"reminders": 0}


def test_booking_moved_meanwhile_is_not_reminded_for_the_old_time(env, monkeypatch):
    module, table, _ = env
    item = put(table, "a" * 32, starts=NOW + timedelta(hours=5))
    stale = {**item}
    table.update_item(Key={"PK": item["PK"], "SK": item["SK"]},
                      UpdateExpression="SET bookedFor = :b",
                      ExpressionAttributeValues={":b": shared_r.iso(NOW + timedelta(hours=6))})
    assert module._mark(stale, NOW) is False


@pytest.mark.parametrize("raw, hours", [(None, 24), ({"reminderHours": 48}, 48), ({"reminderHours": 5}, 24),
                                        ({"reminderHours": "x"}, 24), ({"reminderHours": 0}, 0)])
def test_reminder_hours_setting(env, raw, hours):
    module, _, _ = env
    assert module.reminder_hours({"notifications": raw} if raw is not None else {}) == hours

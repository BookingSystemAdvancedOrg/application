"""Reservation core end to end: create (guest + staff), guest manage view and
cancel, staff list / status / edit / move - against moto DynamoDB with the
real availability engine, so a booking is validated exactly as the guest's
availability was computed. Includes double-booking and tenant isolation."""

from error_body import err
import importlib.util
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from shared import availability as shared_availability
from shared import dynamo as shared_dynamo
from shared import manage_link
from shared import reservations as shared_r
from shared import tenant as shared_tenant
from tenant_support import (LOC_A, LOC_A2, LOC_B, REGION, STAFF_A_SUB, TENANT_A, TENANT_B,
                            TENANT_TABLE, USER_TABLE_DEFAULT, create_tables, tenant_row,
                            with_tenant_claims)

ROOT = Path(__file__).parents[1] / "functions"
LOCATION_TABLE = "test-location"
OCCUPANCY_TABLE = "test-occupancy"
SNAPSHOT_TABLE = "test-layout-snapshot"
RESERVATION_TABLE = "test-reservation"
DAY = "2026-09-20"  # a Sunday; NOW is 12 days earlier
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
WEEK = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _table(resource, name):
    return resource.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                              {"AttributeName": "SK", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


def location_item(tenant_id, location_id, *, duration="2", **extra):
    item = {
        "PK": f"TENANT#{tenant_id}", "SK": f"LOCATION#{location_id}", "tenantId": tenant_id,
        "locationId": location_id, "name": f"Restaurang {location_id}", "address": "Gatan 1",
        "phoneNumber": "+46812345678", "timezone": "Europe/Stockholm",
        "businessHours": {d: [{"opensAt": "10:00", "closesAt": "22:00"}] for d in WEEK},
        "bookingDurationHours": Decimal(duration), "gracePeriodHours": Decimal("0.25"),
        "createdBy": "creator", "createdAt": "2026-08-20T10:00:00Z",
    }
    item.update(extra)
    return item


def table_element(table_id, seats):
    return {
        "elementId": table_id, "type": "table", "x": Decimal("1"), "y": Decimal("0"),
        "z": Decimal("2"), "width": Decimal("1.2"), "height": Decimal("0.75"),
        "depth": Decimal("0.8"), "rotationY": Decimal("0"), "shape": "rect",
        "seats": Decimal(seats), "zone": "main", "updatedBy": "editor",
        "updatedAt": "2026-09-01T09:00:00Z",
    }


def publish_layout(resource, location_id, tables=(("table-a", "2"), ("table-b", "4"), ("table-c", "6"))):
    snap = resource.Table(SNAPSHOT_TABLE)
    snap.put_item(Item={
        "PK": f"LOCATION#{location_id}", "SK": "LAYOUT#ACTIVATION",
        "recordType": "layoutActivationState", "currentVersion": Decimal("1"),
        "revision": Decimal("1"), "updatedBy": "owner", "updatedAt": "2026-09-01T10:00:00Z",
    })
    snap.put_item(Item={
        "PK": f"LOCATION#{location_id}", "SK": "LAYOUT#v1", "version": Decimal("1"),
        "label": "Version 1", "isCurrent": True, "effectiveFrom": "2026-09-01T10:00:00Z",
        "effectiveTo": None, "expiresAt": None,
        "elements": [table_element(t, s) for t, s in tables], "validPositions": [],
        "createdBy": "owner", "createdAt": "2026-09-01T09:00:00Z",
        "updatedBy": "owner", "updatedAt": "2026-09-01T10:00:00Z",
    })


def load(name, module_name):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / name / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


@pytest.fixture
def env(monkeypatch):
    for key, value in {
        "ENVIRONMENT": "dev", "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": REGION,
        "LOCATION_TABLE_NAME": LOCATION_TABLE, "SLOT_OCCUPANCY_TABLE_NAME": OCCUPANCY_TABLE,
        "PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME": SNAPSHOT_TABLE,
        "RESERVATION_TABLE_NAME": RESERVATION_TABLE, "USER_TABLE_NAME": USER_TABLE_DEFAULT,
        "PAYMENT_DELINQUENCY_TABLE_NAME": "test-delinquency",
    }.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        manage_link.reset_cache()
        secret = boto3.client("secretsmanager", region_name=REGION).create_secret(
            Name="test/reservations/link-signing-key", SecretString="k" * 64)
        monkeypatch.setenv("RESERVATION_LINK_KEY_SECRET_ARN", secret["ARN"])
        resource = boto3.resource("dynamodb", region_name=REGION)
        for name in (OCCUPANCY_TABLE, SNAPSHOT_TABLE, RESERVATION_TABLE):
            _table(resource, name)
        create_tables(monkeypatch, location_table=LOCATION_TABLE)
        resource.Table(TENANT_TABLE).put_item(Item=tenant_row(TENANT_A))
        resource.Table(TENANT_TABLE).put_item(Item=tenant_row(TENANT_B))
        for tenant_id, loc in ((TENANT_A, LOC_A), (TENANT_A, LOC_A2), (TENANT_B, LOC_B)):
            resource.Table(LOCATION_TABLE).put_item(Item=location_item(tenant_id, loc))
            publish_layout(resource, loc)
        users = resource.Table(USER_TABLE_DEFAULT)
        users.put_item(Item={"PK": f"USER#{STAFF_A_SUB}", "SK": "PROFILE", "tenantId": TENANT_A,
                             "role": "staff", "locationId": LOC_A, "status": "active"})
        clock = Clock(NOW)
        monkeypatch.setattr(shared_r, "now_utc", clock)
        monkeypatch.setattr(shared_availability, "_now_epoch", lambda: clock().timestamp())
        mods = {
            "create": load("create-pending-reservation", "res_create"),
            "get": load("get-reservation", "res_get"),
            "cancel": load("cancel-reservation", "res_cancel"),
            "staff": load("mark-arrived", "res_staff"),
            "avail": load("get-availability", "res_avail"),
        }
        monkeypatch.setattr(mods["avail"], "_utc_now", clock)
        yield {"m": mods, "res": resource, "clock": clock}
        shared_dynamo._resource = None
        shared_dynamo._client = None
        shared_tenant.reset_caches()
        manage_link.reset_cache()


# --- event helpers ---------------------------------------------------------------

def event(method, path, *, location=LOC_A, reservation=None, body=None, headers=None, query=None,
          staff=None):
    params = {"locationId": location}
    if reservation is not None:
        params["reservationId"] = reservation
    e = {"routeKey": f"{method} {path}", "pathParameters": params,
         "requestContext": {"http": {"method": method}}, "headers": headers or {}}
    if body is not None:
        e["body"] = json.dumps(body)
    if query is not None:
        e["queryStringParameters"] = query
    if staff:
        tenant_id, role = staff
        with_tenant_claims(e, tenant_id, role, STAFF_A_SUB if role == "staff_user" else None)
    return e


OWNER_A = (TENANT_A, "owner_user")
OWNER_B = (TENANT_B, "owner_user")
STAFF_A = (TENANT_A, "staff_user")


def body_of(response):
    return json.loads(response["body"])


def guest_booking(env, *, location=LOC_A, start="18:00", tables=("table-b",), party=3, **extra):
    payload = {"date": DAY, "startTime": start, "tableIds": list(tables), "partySize": party,
               "name": "Anna Svensson", "email": "Anna@Example.se", "phone": "070-123 45 67",
               "acceptTerms": True, **extra}
    return env["m"]["create"].handler(
        event("POST", "/locations/{locationId}/reservations", location=location, body=payload), None)


def free_tables_at(env, start, location=LOC_A, day=DAY):
    response = env["m"]["avail"].handler(
        {"requestContext": {"http": {"method": "GET"}}, "pathParameters": {"locationId": location},
         "queryStringParameters": {"date": day}}, None)
    assert response["statusCode"] == 200, response
    for slot in body_of(response)["slots"]:
        if slot["startTime"] == start:
            return sorted(t["tableId"] for t in slot["tables"])
    return []


def guest_get(env, rid, token, location=LOC_A):
    return env["m"]["get"].handler(event(
        "GET", "/locations/{locationId}/reservations/{reservationId}/guest", location=location,
        reservation=rid, headers={"X-Manage-Token": token}), None)


def guest_cancel(env, rid, token, location=LOC_A):
    return env["m"]["cancel"].handler(event(
        "POST", "/locations/{locationId}/reservations/{reservationId}/cancel", location=location,
        reservation=rid, headers={"x-manage-token": token}), None)


def set_status(env, rid, status, who=OWNER_A, location=LOC_A):
    return env["m"]["staff"].handler(event(
        "POST", "/locations/{locationId}/reservations/{reservationId}/status", location=location,
        reservation=rid, body={"status": status}, staff=who), None)


def edit(env, rid, body, who=OWNER_A, location=LOC_A):
    return env["m"]["staff"].handler(event(
        "PATCH", "/locations/{locationId}/reservations/{reservationId}", location=location,
        reservation=rid, body=body, staff=who), None)


def staff_list(env, who=OWNER_A, location=LOC_A, day=DAY):
    return env["m"]["get"].handler(event(
        "GET", "/locations/{locationId}/reservations", location=location, query={"date": day},
        staff=who), None)


# --- create (guest) ---------------------------------------------------------------

def test_guest_books_a_table_and_it_disappears_from_availability(env):
    assert "table-b" in free_tables_at(env, "18:00")

    response = guest_booking(env)

    assert response["statusCode"] == 201, response
    body = body_of(response)
    assert body["status"] == "reserved" and body["startTime"] == "18:00" and body["endTime"] == "20:00"
    assert body["tableIds"] == ["table-b"] and body["partySize"] == 3 and body["cancellable"] is True
    assert body["location"]["name"] == f"Restaurang {LOC_A}"
    assert len(body["manageToken"]) >= 32
    assert free_tables_at(env, "18:00") == ["table-a", "table-c"]
    assert "table-b" in free_tables_at(env, "16:00") and "table-b" in free_tables_at(env, "20:00")

    stored = env["res"].Table(RESERVATION_TABLE).get_item(
        Key={"PK": f"LOCATION#{LOC_A}", "SK": f"RESERVATION#{DAY}#{body['reservationId']}"})["Item"]
    assert stored["customerEmail"] == "anna@example.se" and stored["customerPhone"] == "+46701234567"
    assert stored["tenantId"] == TENANT_A and stored["bookedFor"] == "2026-09-20T16:00:00Z"
    # The token is never stored - it is rebuilt from the booking with the key.
    assert "manageToken" not in stored and "manageTokenHash" not in stored
    assert body["manageToken"] not in json.dumps(stored, default=str)
    assert manage_link.token_for(stored) == body["manageToken"]
    assert stored["notice"]["type"] == "confirmed" and len(stored["notice"]["id"]) == 32
    assert stored["termsAcceptedAt"] and stored["ttl"] > int(datetime(2027, 9, 1).timestamp())


def test_same_table_twice_is_refused(env):
    assert guest_booking(env)["statusCode"] == 201
    second = guest_booking(env, party=2)
    assert second["statusCode"] == 409 and body_of(second) == err("table_taken")


def test_concurrent_bookings_cannot_both_win(env, monkeypatch):
    """Both writers read the same (empty) state; only one commits."""
    m = env["m"]["create"]
    real = shared_r.read_lock_versions
    snapshots = []

    def stale(location_id, date_str, table_ids):
        if not snapshots:
            snapshots.append(real(location_id, date_str, table_ids))
        return dict(snapshots[0])

    monkeypatch.setattr(shared_r, "read_lock_versions", stale)
    monkeypatch.setattr(m.availability, "query_occupancies", lambda *a: [])
    first = guest_booking(env, tables=("table-c",), party=4)
    # Different duration -> different hold keys; only the lock can catch it.
    env["res"].Table(LOCATION_TABLE).update_item(
        Key={"PK": f"TENANT#{TENANT_A}", "SK": f"LOCATION#{LOC_A}"},
        UpdateExpression="SET bookingDurationHours = :d", ExpressionAttributeValues={":d": Decimal("3")})
    second = guest_booking(env, start="16:00", tables=("table-c",), party=4)

    assert first["statusCode"] == 201
    assert second["statusCode"] == 409 and body_of(second) == err("table_taken")


@pytest.mark.parametrize("change, message", [
    ({"startTime": "18:15"}, "startTime is not a bookable time"),
    ({"tableIds": ["table-x"]}, "isn't bookable"),
    ({"partySize": 5}, "seat 4"),
    ({"tableIds": []}, "tableIds must be a non-empty list"),
    ({"acceptTerms": False}, "acceptTerms must be true"),
    ({"email": "nope"}, "email must be a valid"),
    ({"phone": "123"}, "phone must be a valid"),
    ({"date": "2026-02-30"}, "real calendar date"),
    ({"date": "2026-08-01"}, "past"),
    ({"partySize": True}, "partySize"),
    ({"extra": 1}, "unsupported fields: extra"),
])
def test_invalid_bookings_are_rejected(env, change, message):
    payload = {"date": DAY, "startTime": "18:00", "tableIds": ["table-b"], "partySize": 3,
               "name": "Anna", "email": "a@b.se", "phone": "+46701234567", "acceptTerms": True}
    payload.update(change)
    response = env["m"]["create"].handler(
        event("POST", "/locations/{locationId}/reservations", body=payload), None)
    assert response["statusCode"] == 400, response
    assert message in body_of(response)["error"]


def set_location(env, location=LOC_A, tenant_id=TENANT_A, **fields):
    """Stores booking rules on a location the way PUT /locations does."""
    table = env["res"].Table(LOCATION_TABLE)
    key = {"PK": f"TENANT#{tenant_id}", "SK": f"LOCATION#{location}"}
    item = table.get_item(Key=key)["Item"]
    for name, value in fields.items():
        if value is None:
            item.pop(name, None)
        else:
            item[name] = value
    table.put_item(Item=item)
    shared_tenant.reset_caches()


def test_two_tables_for_a_big_party(env):
    set_location(env, tableCombinations=[
        {"tableIds": ["table-b", "table-c"], "minParty": Decimal(7), "maxParty": Decimal(10)}])
    response = guest_booking(env, tables=("table-b", "table-c"), party=9)
    assert response["statusCode"] == 201
    assert body_of(response)["tableIds"] == ["table-b", "table-c"]
    assert free_tables_at(env, "18:00") == ["table-a"]


def test_guests_combine_tables_only_as_the_restaurant_set_up(env):
    # No combination set up: two tables online are refused...
    refused = guest_booking(env, tables=("table-b", "table-c"), party=9)
    assert refused["statusCode"] == 400
    assert body_of(refused)["error"] == "tableIds are not offered online for this party size"
    # ...a combination's own party range counts...
    set_location(env, tableCombinations=[
        {"tableIds": ["table-b", "table-c"], "minParty": Decimal(8), "maxParty": Decimal(10)}])
    assert guest_booking(env, tables=("table-b", "table-c"), party=7)["statusCode"] == 400
    assert guest_booking(env, tables=("table-c", "table-b"), party=8)["statusCode"] == 201
    # ...and staff may still combine any free tables with enough seats.
    staff = staff_booking(env, tableIds=["table-a", "table-b"], partySize=5, startTime="20:00")
    assert staff["statusCode"] == 201, staff


def test_one_guest_cannot_take_a_big_table_online(env):
    # Default online range of a table: half its seats (rounded up) to all
    # of them - the 6-seat table-c is for 3-6 guests online.
    small = guest_booking(env, tables=("table-c",), party=2)
    assert small["statusCode"] == 400
    assert body_of(small)["error"] == "tableIds are not offered online for this party size"
    assert guest_booking(env, tables=("table-c",), party=3)["statusCode"] == 201
    # Staff may seat two people at any table.
    staff = staff_booking(env, tableIds=["table-c"], partySize=2, startTime="20:00")
    assert staff["statusCode"] == 201, staff


def test_table_rules_set_who_may_book_a_table_online(env):
    set_location(env, tableRules={
        "table-c": {"minParty": Decimal(1), "maxParty": Decimal(4), "online": True},
        "table-b": {"minParty": Decimal(1), "maxParty": Decimal(4), "online": False},
    })
    assert guest_booking(env, tables=("table-c",), party=1)["statusCode"] == 201
    # Max 4 online even though the table seats 6.
    assert guest_booking(env, tables=("table-c",), party=5, start="20:00")["statusCode"] == 400
    # Not bookable online at all.
    off = guest_booking(env, tables=("table-b",), party=2)
    assert off["statusCode"] == 400
    assert staff_booking(env, tableIds=["table-b"], partySize=2)["statusCode"] == 201


def guest_view(env, party, day=DAY, location=LOC_A):
    response = env["m"]["avail"].handler(
        {"requestContext": {"http": {"method": "GET"}}, "pathParameters": {"locationId": location},
         "queryStringParameters": {"date": day, "partySize": str(party)}}, None)
    assert response["statusCode"] == 200, response
    return body_of(response)


def test_availability_offers_a_party_only_its_online_options(env):
    set_location(env, tableCombinations=[
        {"tableIds": ["table-b", "table-c"], "minParty": Decimal(5), "maxParty": Decimal(10)}])
    slot = next(s for s in guest_view(env, 5)["slots"] if s["startTime"] == "18:00")
    # 5 guests: table-c alone (6 seats, 3-6) first, then the combination.
    assert slot["options"] == [
        {"tableIds": ["table-c"], "seats": 6},
        {"tableIds": ["table-b", "table-c"], "seats": 10},
    ]
    assert [t["tableId"] for t in slot["tables"]] == ["table-b", "table-c"]
    two = next(s for s in guest_view(env, 2)["slots"] if s["startTime"] == "18:00")
    assert two["options"] == [{"tableIds": ["table-a"], "seats": 2}, {"tableIds": ["table-b"], "seats": 4}]
    # Nothing fits 11 online: no slots at all.
    assert guest_view(env, 11)["slots"] == []
    # The staff view (no partySize) is unchanged: every free table, no options.
    staff_slot = next(s for s in body_of(env["m"]["avail"].handler(
        {"requestContext": {"http": {"method": "GET"}}, "pathParameters": {"locationId": LOC_A},
         "queryStringParameters": {"date": DAY}}, None))["slots"] if s["startTime"] == "18:00")
    assert "options" not in staff_slot and len(staff_slot["tables"]) == 3


def test_guest_view_explains_every_table_it_does_not_offer(env):
    # table-a 2 seats (1-2 online), table-b 4 (2-4), table-c 6 (3-6).
    set_location(env, tableRules={
        "table-a": {"minParty": Decimal(1), "maxParty": Decimal(2), "online": False}})
    slot = next(s for s in guest_view(env, 5)["slots"] if s["startTime"] == "18:00")
    assert slot["options"] == [{"tableIds": ["table-c"], "seats": 6}]
    assert slot["unavailable"] == [
        {"tableId": "table-a", "reason": "phone"},
        {"tableId": "table-b", "minParty": 2, "maxParty": 4, "reason": "too_small"},
    ]
    two = next(s for s in guest_view(env, 2)["slots"] if s["startTime"] == "18:00")
    assert two["options"] == [{"tableIds": ["table-b"], "seats": 4}]
    assert two["unavailable"] == [
        {"tableId": "table-a", "reason": "phone"},
        {"tableId": "table-c", "minParty": 3, "maxParty": 6, "reason": "too_large"},
    ]
    # A party nothing fits gets no slot at all (nothing to explain on a table).
    assert guest_view(env, 1)["slots"] == []

    # A guest's booking shows as booked - nothing else about it.
    assert guest_booking(env, tables=("table-c",), party=4)["statusCode"] == 201
    three = next(s for s in guest_view(env, 3)["slots"] if s["startTime"] == "18:00")
    assert {"tableId": "table-c", "reason": "booked"} in three["unavailable"]


def test_guest_view_tells_a_staff_block_from_a_booking(env):
    env["res"].Table(OCCUPANCY_TABLE).put_item(Item={
        "PK": f"LOCATION#{LOC_A}", "SK": f"SLOT#{DAY}#18:00-20:00#table-c",
        "reservationId": "MANUAL_BLOCK#0b1c2d3e-4f50-4a6b-8c7d-8e9fa0b1c2d3",
        "source": "manual_block", "createdBy": "staff", "createdAt": "2026-09-08T10:00:00Z",
        "ttl": 4102444800})
    slot = next(s for s in guest_view(env, 4)["slots"] if s["startTime"] == "18:00")
    assert {"tableId": "table-c", "reason": "blocked"} in slot["unavailable"]
    # The staff view has no reasons at all.
    staff_slot = next(s for s in body_of(env["m"]["avail"].handler(
        {"requestContext": {"http": {"method": "GET"}}, "pathParameters": {"locationId": LOC_A},
         "queryStringParameters": {"date": DAY}}, None))["slots"] if s["startTime"] == "18:00")
    assert "unavailable" not in staff_slot


@pytest.mark.parametrize("query", [
    {"partySize": "0"}, {"partySize": "51"}, {"partySize": "two"}, {"partySize": "²"},
    {"partySize": "2", "walkIn": "true"},
])
def test_availability_party_size_is_validated(env, query):
    response = env["m"]["avail"].handler(
        {"requestContext": {"http": {"method": "GET"}}, "pathParameters": {"locationId": LOC_A},
         "queryStringParameters": {"date": DAY, **query}}, None)
    assert response["statusCode"] == 400


def test_online_party_limit_empties_the_guest_view(env):
    set_location(env, maxPartySizeOnline=Decimal(4))
    assert guest_view(env, 5)["slots"] == []
    assert guest_view(env, 4)["slots"]


def test_online_party_size_limit_of_the_location(env):
    env["res"].Table(LOCATION_TABLE).update_item(
        Key={"PK": f"TENANT#{TENANT_A}", "SK": f"LOCATION#{LOC_A}"},
        UpdateExpression="SET maxPartySizeOnline = :m", ExpressionAttributeValues={":m": Decimal(4)})
    shared_tenant.reset_caches()
    response = guest_booking(env, tables=("table-c",), party=6)
    assert response["statusCode"] == 400 and "at most 4" in body_of(response)["error"]


def test_tenant_without_reservations_feature_gets_404(env):
    env["res"].Table(TENANT_TABLE).put_item(Item=tenant_row(TENANT_A, features={"reservations": False}))
    shared_tenant.reset_caches()
    assert guest_booking(env)["statusCode"] == 404


def test_suspended_tenant_gets_404(env):
    env["res"].Table(TENANT_TABLE).put_item(Item=tenant_row(TENANT_A, status="suspended"))
    shared_tenant.reset_caches()
    assert guest_booking(env)["statusCode"] == 404


# --- guest manage link ---------------------------------------------------------------

def test_guest_sees_own_booking_only_with_the_token(env):
    created = body_of(guest_booking(env))
    rid, token = created["reservationId"], created["manageToken"]

    ok = guest_get(env, rid, token)
    assert ok["statusCode"] == 200
    view = body_of(ok)
    assert view["status"] == "reserved" and "customerEmail" not in view and "customerPhone" not in view

    assert guest_get(env, rid, "wrong")["statusCode"] == 404
    assert guest_get(env, rid, "")["statusCode"] == 404
    assert guest_get(env, "f" * 32, token)["statusCode"] == 404
    # Same booking id through another tenant's location: not found.
    assert guest_get(env, rid, token, location=LOC_B)["statusCode"] == 404


def test_guest_cancels_and_the_table_is_free_again(env):
    created = body_of(guest_booking(env))
    rid, token = created["reservationId"], created["manageToken"]

    response = guest_cancel(env, rid, token)

    assert response["statusCode"] == 200
    assert body_of(response)["status"] == "cancelled_no_charge" and body_of(response)["cancellable"] is False
    assert "table-b" in free_tables_at(env, "18:00")
    again = guest_cancel(env, rid, token)
    assert again["statusCode"] == 200 and body_of(again)["status"] == "cancelled_no_charge"
    assert guest_cancel(env, rid, "wrong")["statusCode"] == 404
    # The table can be booked again.
    assert guest_booking(env)["statusCode"] == 201


def test_guest_cannot_cancel_after_the_start(env):
    created = body_of(guest_booking(env))
    env["clock"].value = datetime(2026, 9, 20, 16, 5, tzinfo=timezone.utc)
    response = guest_cancel(env, created["reservationId"], created["manageToken"])
    assert response["statusCode"] == 409 and body_of(response) == err("already_started")


# --- staff -----------------------------------------------------------------------------

def test_staff_list_shows_the_day_with_contact_details(env):
    guest_booking(env, start="20:00")
    guest_booking(env, start="12:00", tables=("table-a",), party=2)

    response = staff_list(env)

    assert response["statusCode"] == 200
    items = body_of(response)["items"]
    assert [i["startTime"] for i in items] == ["12:00", "20:00"]
    assert items[0]["customerPhone"] == "+46701234567" and "manageTokenHash" not in items[0]
    assert body_of(staff_list(env, day="2026-09-21"))["items"] == []


def test_other_tenant_and_other_location_staff_see_nothing(env):
    rid = body_of(guest_booking(env))["reservationId"]
    assert staff_list(env, who=OWNER_B)["statusCode"] == 404
    assert edit(env, rid, {"notes": "x"}, who=OWNER_B)["statusCode"] == 404
    assert set_status(env, rid, "cancelled", who=OWNER_B)["statusCode"] == 404
    # Staff of tenant A assigned to LOC_A can't work on LOC_A2.
    assert staff_list(env, who=STAFF_A, location=LOC_A2)["statusCode"] == 404
    assert staff_list(env, who=STAFF_A)["statusCode"] == 200


def test_staff_phone_booking_without_email(env):
    response = env["m"]["create"].handler(event(
        "POST", "/locations/{locationId}/reservations/manual", staff=STAFF_A,
        body={"date": DAY, "startTime": "18:00", "tableIds": ["table-a"], "partySize": 2,
              "name": "Telefonbokning", "source": "phone"}), None)
    assert response["statusCode"] == 201, response
    body = body_of(response)
    assert body["source"] == "phone" and body["createdBy"] == STAFF_A_SUB and "manageToken" not in body
    assert "table-a" not in free_tables_at(env, "18:00")


def test_walk_in_can_take_the_running_slot(env):
    env["clock"].value = datetime(2026, 9, 20, 16, 30, tzinfo=timezone.utc)  # 18:30 local
    payload = {"date": DAY, "startTime": "18:00", "tableIds": ["table-a"], "partySize": 2,
               "name": "Walk-in", "source": "walk_in"}
    staff = env["m"]["create"].handler(event(
        "POST", "/locations/{locationId}/reservations/manual", staff=OWNER_A, body=payload), None)
    assert staff["statusCode"] == 201, staff
    # Guests can't book a slot that already started.
    assert guest_booking(env, tables=("table-b",))["statusCode"] == 400


def test_bookings_need_thirty_minutes_lead_time(env):
    # 17:32 local: 18:00 is 28 minutes away - not bookable online or by phone.
    env["clock"].value = datetime(2026, 9, 20, 15, 32, tzinfo=timezone.utc)
    too_soon = guest_booking(env, start="18:00")
    assert too_soon["statusCode"] == 400 and "bookable" in body_of(too_soon)["error"]
    phone = env["m"]["create"].handler(event(
        "POST", "/locations/{locationId}/reservations/manual", staff=STAFF_A,
        body={"date": DAY, "startTime": "18:00", "tableIds": ["table-a"], "partySize": 2,
              "name": "Telefon", "source": "phone"}), None)
    assert phone["statusCode"] == 400
    # 18:30 is the first start - and the half hour grid has no 18:02.
    assert guest_booking(env, start="18:30")["statusCode"] == 201
    assert guest_booking(env, start="18:02", tables=("table-a",))["statusCode"] == 400


def test_walk_in_may_take_a_slot_inside_the_lead_time(env):
    env["clock"].value = datetime(2026, 9, 20, 15, 32, tzinfo=timezone.utc)  # 17:32 local
    walk = env["m"]["create"].handler(event(
        "POST", "/locations/{locationId}/reservations/manual", staff=STAFF_A,
        body={"date": DAY, "startTime": "18:00", "tableIds": ["table-a"], "partySize": 2,
              "name": "Walk-in", "source": "walk_in"}), None)
    assert walk["statusCode"] == 201, walk


def test_moves_need_the_lead_time_but_table_swaps_do_not(env):
    rid = body_of(guest_booking(env))["reservationId"]  # 18:00 on table-b
    env["clock"].value = datetime(2026, 9, 20, 15, 45, tzinfo=timezone.utc)  # 17:45 local
    # Same time, other table: fine right up to the booking.
    swapped = edit(env, rid, {"tableIds": ["table-c"]})
    assert swapped["statusCode"] == 200, swapped
    # Another time inside the lead time: refused; one beyond it: fine.
    assert edit(env, rid, {"startTime": "17:30"})["statusCode"] == 400
    assert edit(env, rid, {"startTime": "18:30"})["statusCode"] == 200


def test_a_walk_in_can_be_moved_to_a_running_or_imminent_time(env):
    env["clock"].value = datetime(2026, 9, 20, 15, 32, tzinfo=timezone.utc)  # 17:32 local
    walk = env["m"]["create"].handler(event(
        "POST", "/locations/{locationId}/reservations/manual", staff=STAFF_A,
        body={"date": DAY, "startTime": "17:30", "tableIds": ["table-a"], "partySize": 2,
              "name": "Walk-in", "source": "walk_in"}), None)
    assert walk["statusCode"] == 201, walk
    rid = body_of(walk)["reservationId"]
    # The walk-in rule follows the booking: 18:00 is inside the 30 min.
    moved = edit(env, rid, {"startTime": "18:00"})
    assert moved["statusCode"] == 200, moved
    assert body_of(moved)["startTime"] == "18:00"
    # Off the half-hour grid is never a start - walk-in or not.
    assert edit(env, rid, {"startTime": "17:45"})["statusCode"] == 400


def test_a_walk_in_is_booked_for_today_only(env):
    # NOW is 12 days before DAY: a "walk-in" for DAY is refused, a phone booking is fine.
    later = env["m"]["create"].handler(event(
        "POST", "/locations/{locationId}/reservations/manual", staff=STAFF_A,
        body={"date": DAY, "startTime": "18:00", "tableIds": ["table-a"], "partySize": 2,
              "name": "Walk-in", "source": "walk_in"}), None)
    assert later["statusCode"] == 400
    assert "today" in body_of(later)["error"]
    phone = env["m"]["create"].handler(event(
        "POST", "/locations/{locationId}/reservations/manual", staff=STAFF_A,
        body={"date": DAY, "startTime": "18:00", "tableIds": ["table-a"], "partySize": 2,
              "name": "Telefon", "source": "phone"}), None)
    assert phone["statusCode"] == 201, phone


def test_today_follows_the_location_time_zone_for_walk_ins(env):
    # 23:10 UTC on the 19th is 01:10 on the 20th in Stockholm.
    env["clock"].value = datetime(2026, 9, 19, 23, 10, tzinfo=timezone.utc)
    walk = env["m"]["create"].handler(event(
        "POST", "/locations/{locationId}/reservations/manual", staff=STAFF_A,
        body={"date": DAY, "startTime": "18:00", "tableIds": ["table-a"], "partySize": 2,
              "name": "Walk-in", "source": "walk_in"}), None)
    assert walk["statusCode"] == 201, walk


def test_a_walk_in_is_never_moved_to_another_day(env):
    env["clock"].value = datetime(2026, 9, 20, 15, 32, tzinfo=timezone.utc)  # 17:32 local
    walk = env["m"]["create"].handler(event(
        "POST", "/locations/{locationId}/reservations/manual", staff=STAFF_A,
        body={"date": DAY, "startTime": "18:00", "tableIds": ["table-a"], "partySize": 2,
              "name": "Walk-in", "source": "walk_in"}), None)
    rid = body_of(walk)["reservationId"]
    moved = edit(env, rid, {"date": "2026-09-21", "startTime": "18:00"})
    assert moved["statusCode"] == 400
    assert "its day" in body_of(moved)["error"]
    assert stored_item(env, rid)["date"] == DAY


def test_a_phone_booking_cannot_be_moved_inside_the_lead_time(env):
    phone = env["m"]["create"].handler(event(
        "POST", "/locations/{locationId}/reservations/manual", staff=STAFF_A,
        body={"date": DAY, "startTime": "20:00", "tableIds": ["table-a"], "partySize": 2,
              "name": "Telefon", "source": "phone"}), None)
    rid = body_of(phone)["reservationId"]
    env["clock"].value = datetime(2026, 9, 20, 15, 32, tzinfo=timezone.utc)  # 17:32 local
    assert edit(env, rid, {"startTime": "18:00"})["statusCode"] == 400
    assert edit(env, rid, {"startTime": "18:30"})["statusCode"] == 200


def test_a_booking_from_before_the_half_hour_grid_can_still_change_tables(env, monkeypatch):
    # Made when the location offered 18:15 (a quarter-hour grid)...
    monkeypatch.setattr(shared_availability, "SLOT_STEP_MINUTES", 15)
    created = guest_booking(env, start="18:15")
    assert created["statusCode"] == 201, created
    rid = body_of(created)["reservationId"]
    # ...the grid is half hours again: its own time still works for edits,
    # but nobody can book or move to 18:15.
    monkeypatch.setattr(shared_availability, "SLOT_STEP_MINUTES", 30)
    swapped = edit(env, rid, {"tableIds": ["table-c"]})
    assert swapped["statusCode"] == 200, swapped
    assert body_of(swapped)["startTime"] == "18:15"
    assert guest_booking(env, start="18:15", tables=("table-a",), party=2)["statusCode"] == 400


def test_arrive_no_show_and_undo_rules(env):
    rid = body_of(guest_booking(env))["reservationId"]
    env["clock"].value = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)  # 4 h before
    assert body_of(set_status(env, rid, "arrived")) == err("too_early")
    assert body_of(set_status(env, rid, "no_show")) == err("grace_period_not_over")

    env["clock"].value = datetime(2026, 9, 20, 16, 0, tzinfo=timezone.utc)
    arrived = set_status(env, rid, "arrived", who=STAFF_A)
    assert arrived["statusCode"] == 200 and body_of(arrived)["status"] == "arrived"
    assert body_of(set_status(env, rid, "reserved"))["status"] == "reserved"

    env["clock"].value = datetime(2026, 9, 20, 16, 20, tzinfo=timezone.utc)  # past 15 min grace
    no_show = set_status(env, rid, "no_show")
    assert body_of(no_show)["status"] == "no_show"
    assert "table-b" in free_tables_at(env, "20:00")
    history = [h["status"] for h in body_of(no_show)["history"]]
    assert history == ["reserved", "arrived", "reserved", "no_show"]
    assert body_of(set_status(env, rid, "arrived"))["status"] == "arrived"  # came late after all
    assert set_status(env, rid, "dancing")["statusCode"] == 400


def test_restaurant_cancels_and_frees_the_table(env):
    rid = body_of(guest_booking(env))["reservationId"]
    response = set_status(env, rid, "cancelled")
    assert body_of(response)["status"] == "cancelled_by_restaurant"
    assert "table-b" in free_tables_at(env, "18:00")
    assert body_of(set_status(env, rid, "cancelled")) == err("not_cancellable")


def test_move_to_another_table_and_time(env):
    rid = body_of(guest_booking(env))["reservationId"]

    moved = edit(env, rid, {"startTime": "20:00", "tableIds": ["table-c"], "partySize": 5})

    assert moved["statusCode"] == 200, moved
    body = body_of(moved)
    assert (body["startTime"], body["tableIds"], body["partySize"], body["seats"]) == ("20:00", ["table-c"], 5, 6)
    assert "table-b" in free_tables_at(env, "18:00")
    assert "table-c" not in free_tables_at(env, "20:00")


def test_move_to_another_day_keeps_the_booking_reachable(env):
    created = body_of(guest_booking(env))
    rid = created["reservationId"]

    moved = edit(env, rid, {"date": "2026-09-21"})

    assert moved["statusCode"] == 200 and body_of(moved)["date"] == "2026-09-21"
    assert [i["reservationId"] for i in body_of(staff_list(env, day="2026-09-21"))["items"]] == [rid]
    assert body_of(staff_list(env))["items"] == []
    assert "table-b" in free_tables_at(env, "18:00")
    assert "table-b" not in free_tables_at(env, "18:00", day="2026-09-21")
    assert body_of(guest_get(env, rid, created["manageToken"]))["date"] == "2026-09-21"


def test_move_onto_a_taken_table_is_refused(env):
    first = body_of(guest_booking(env))["reservationId"]
    guest_booking(env, tables=("table-c",), party=4)
    response = edit(env, first, {"tableIds": ["table-c"]})
    assert response["statusCode"] == 409 and body_of(response) == err("table_taken")
    assert "table-b" not in free_tables_at(env, "18:00")  # original hold untouched


def test_bigger_party_needs_more_seats(env):
    rid = body_of(guest_booking(env))["reservationId"]
    response = edit(env, rid, {"partySize": 6})
    assert response["statusCode"] == 400 and "seat 4" in body_of(response)["error"]
    assert body_of(edit(env, rid, {"partySize": 4}))["partySize"] == 4


def test_edit_contact_details_and_history(env):
    rid = body_of(guest_booking(env))["reservationId"]
    response = edit(env, rid, {"phone": "0709999999", "notes": "Allergi: nötter"})
    body = body_of(response)
    assert body["customerPhone"] == "+46709999999" and body["notes"] == "Allergi: nötter"
    assert body["history"][-1]["fields"] == ["customerPhone", "notes"]
    assert edit(env, rid, {"email": None})["statusCode"] == 400  # online bookings keep an email


def test_unknown_routes_and_methods(env):
    m = env["m"]
    assert m["create"].handler(event("GET", "/locations/{locationId}/reservations"), None)["statusCode"] == 405
    assert m["get"].handler(event("POST", "/locations/{locationId}/reservations"), None)["statusCode"] == 405
    assert m["staff"].handler(event("GET", "/whatever"), None)["statusCode"] == 404


# --- review fixes ------------------------------------------------------------------

def walk_in(env, start="18:00", table="table-b"):
    return env["m"]["create"].handler(event(
        "POST", "/locations/{locationId}/reservations/manual", staff=OWNER_A,
        body={"date": DAY, "startTime": start, "tableIds": [table], "partySize": 2,
              "name": "Walk-in", "source": "walk_in"}), None)


def test_late_arrival_after_no_show_takes_the_table_back(env):
    rid = body_of(guest_booking(env))["reservationId"]
    env["clock"].value = datetime(2026, 9, 20, 16, 20, tzinfo=timezone.utc)
    set_status(env, rid, "no_show")

    arrived = set_status(env, rid, "arrived")

    assert arrived["statusCode"] == 200 and body_of(arrived)["status"] == "arrived"
    assert walk_in(env)["statusCode"] == 409  # the table is held again
    # Undo -> cancel still frees the table (holds are owned again).
    assert body_of(set_status(env, rid, "reserved"))["status"] == "reserved"
    assert body_of(set_status(env, rid, "cancelled"))["status"] == "cancelled_by_restaurant"
    assert walk_in(env)["statusCode"] == 201


def test_late_arrival_is_refused_when_the_table_was_given_away(env):
    rid = body_of(guest_booking(env))["reservationId"]
    env["clock"].value = datetime(2026, 9, 20, 16, 20, tzinfo=timezone.utc)
    set_status(env, rid, "no_show")
    assert walk_in(env)["statusCode"] == 201

    response = set_status(env, rid, "arrived")

    assert response["statusCode"] == 409 and body_of(response) == err("table_taken")
    listed = {i["reservationId"]: i["status"] for i in body_of(staff_list(env))["items"]}
    assert listed[rid] == "no_show"
    # Cancelling the no-show doesn't touch the walk-in's hold.
    assert body_of(set_status(env, rid, "cancelled")) == err("not_cancellable")


def test_late_arrival_after_the_end_is_refused(env):
    rid = body_of(guest_booking(env))["reservationId"]
    env["clock"].value = datetime(2026, 9, 20, 16, 20, tzinfo=timezone.utc)
    set_status(env, rid, "no_show")
    env["clock"].value = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)
    assert body_of(set_status(env, rid, "arrived")) == err("already_ended")


def test_concurrent_transaction_is_a_conflict_not_an_outage(monkeypatch):
    from botocore.exceptions import ClientError

    class FakeClient:
        def __init__(self, reason):
            self.reason = reason

        def transact_write_items(self, **_):
            raise ClientError({"Error": {"Code": "TransactionCanceledException"},
                               "CancellationReasons": [{"Code": "None"}, {"Code": self.reason}]},
                              "TransactWriteItems")

    monkeypatch.setenv("SLOT_OCCUPANCY_TABLE_NAME", OCCUPANCY_TABLE)
    monkeypatch.setenv("RESERVATION_TABLE_NAME", RESERVATION_TABLE)
    items = [{"Put": {"TableName": RESERVATION_TABLE}}, {"Update": {"TableName": OCCUPANCY_TABLE}}]
    for reason in ("TransactionConflict", "ConditionalCheckFailed"):
        monkeypatch.setattr(shared_r.dynamo, "client", lambda reason=reason: FakeClient(reason))
        with pytest.raises(shared_r.Conflict) as exc:
            shared_r.transact(items)
        assert exc.value.code == "table_taken"
    monkeypatch.setattr(shared_r.dynamo, "client", lambda: FakeClient("ConditionalCheckFailed"))
    with pytest.raises(shared_r.Conflict) as exc:
        shared_r.transact(list(reversed(items)))  # failure on the reservation row
    assert exc.value.code == "changed_retry"


@pytest.mark.parametrize("raw, expected", [
    ("070-123 45 67", "+46701234567"),
    ("+46 (0)70 123 45 67", "+46701234567"),
    ("0046701234567", "+46701234567"),
    ("08-123 456 78", "+46812345678"),
    ("+47 412 34 567", "+4741234567"),
])
def test_phone_normalization(raw, expected):
    assert shared_r.normalize_phone(raw) == expected


def test_same_day_move_keeps_the_pointer_expiry_in_step(env):
    rid = body_of(guest_booking(env))["reservationId"]
    edit(env, rid, {"startTime": "20:00"})
    tbl = env["res"].Table(RESERVATION_TABLE)
    item = tbl.get_item(Key={"PK": f"LOCATION#{LOC_A}", "SK": f"RESERVATION#{DAY}#{rid}"})["Item"]
    pointer = tbl.get_item(Key={"PK": f"LOCATION#{LOC_A}", "SK": f"RID#{rid}"})["Item"]
    assert pointer["ttl"] == item["ttl"]


# --- signed manage links and guest notices (M3) -------------------------------------

def stored_item(env, rid, day=DAY, location=LOC_A):
    return env["res"].Table(RESERVATION_TABLE).get_item(
        Key={"PK": f"LOCATION#{location}", "SK": f"RESERVATION#{day}#{rid}"})["Item"]


def test_link_from_before_signed_tokens_still_works(env):
    created = body_of(guest_booking(env))
    rid = created["reservationId"]
    table = env["res"].Table(RESERVATION_TABLE)
    table.update_item(Key={"PK": f"LOCATION#{LOC_A}", "SK": f"RESERVATION#{DAY}#{rid}"},
                      UpdateExpression="SET manageTokenHash = :h",
                      ExpressionAttributeValues={":h": __import__("hashlib").sha256(b"old-random").hexdigest()})

    assert guest_get(env, rid, "old-random")["statusCode"] == 200
    assert guest_get(env, rid, created["manageToken"])["statusCode"] == 404


def test_bumping_link_version_revokes_old_links(env):
    created = body_of(guest_booking(env))
    rid = created["reservationId"]
    env["res"].Table(RESERVATION_TABLE).update_item(
        Key={"PK": f"LOCATION#{LOC_A}", "SK": f"RESERVATION#{DAY}#{rid}"},
        UpdateExpression="SET linkVersion = :v", ExpressionAttributeValues={":v": 2})
    assert guest_get(env, rid, created["manageToken"])["statusCode"] == 404
    assert guest_get(env, rid, manage_link.token_for(stored_item(env, rid)))["statusCode"] == 200


def test_token_for_another_booking_or_tenant_never_matches(env):
    a = body_of(guest_booking(env, tables=("table-a",), party=2))
    b = body_of(guest_booking(env, tables=("table-c",), party=3))
    assert a["manageToken"] != b["manageToken"]
    assert guest_get(env, a["reservationId"], b["manageToken"])["statusCode"] == 404
    assert guest_cancel(env, a["reservationId"], "x" * 300)["statusCode"] == 404


def test_missing_signing_key_fails_the_booking_cleanly(env, monkeypatch):
    monkeypatch.delenv("RESERVATION_LINK_KEY_SECRET_ARN")
    manage_link.reset_cache()

    response = guest_booking(env)

    assert response["statusCode"] == 503
    assert "table-b" in free_tables_at(env, "18:00")  # nothing half-written


def test_guest_cancel_sets_a_cancellation_notice(env):
    created = body_of(guest_booking(env))
    first = stored_item(env, created["reservationId"])["notice"]

    guest_cancel(env, created["reservationId"], created["manageToken"])

    notice = stored_item(env, created["reservationId"])["notice"]
    assert notice["type"] == "cancelled_by_guest" and notice["id"] != first["id"]


def staff_booking(env, **extra):
    payload = {"date": DAY, "startTime": "18:00", "tableIds": ["table-b"], "partySize": 2,
               "name": "Per", "phone": "0701112233", **extra}
    return env["m"]["create"].handler(event(
        "POST", "/locations/{locationId}/reservations/manual", body=payload, staff=OWNER_A), None)


@pytest.mark.parametrize("extra, notified", [
    ({}, True),                                  # phone booking: confirmation by SMS
    ({"notifyGuest": False}, False),
    ({"source": "walk_in"}, False),              # standing in the restaurant
    ({"source": "walk_in", "notifyGuest": True}, True),
    ({"phone": None}, False),                    # nothing to send to
])
def test_staff_booking_notice(env, extra, notified):
    if extra.get("source") == "walk_in":
        env["clock"].value = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)  # 17:00 local, same day
    response = staff_booking(env, **extra)
    assert response["statusCode"] == 201, response
    assert ("notice" in stored_item(env, body_of(response)["reservationId"])) is notified


def test_staff_booking_rejects_a_non_boolean_notify_flag(env):
    assert staff_booking(env, notifyGuest="yes")["statusCode"] == 400


def test_restaurant_cancel_notifies_unless_told_not_to(env):
    a = body_of(guest_booking(env, tables=("table-a",), party=2))["reservationId"]
    b = body_of(guest_booking(env, tables=("table-c",), party=3))["reservationId"]
    before_b = stored_item(env, b)["notice"]["id"]
    ok = env["m"]["staff"].handler(event(
        "POST", "/locations/{locationId}/reservations/{reservationId}/status", reservation=a,
        body={"status": "cancelled", "reason": "Kök stängt"}, staff=OWNER_A), None)
    quiet = env["m"]["staff"].handler(event(
        "POST", "/locations/{locationId}/reservations/{reservationId}/status", reservation=b,
        body={"status": "cancelled", "notifyGuest": False}, staff=OWNER_A), None)
    assert ok["statusCode"] == 200 and quiet["statusCode"] == 200
    assert stored_item(env, a)["notice"]["type"] == "cancelled_by_restaurant"
    assert stored_item(env, b)["notice"]["id"] == before_b  # untouched: no new notice


def test_move_to_new_time_notifies_but_table_swap_does_not(env):
    rid = body_of(guest_booking(env))["reservationId"]
    confirmed = stored_item(env, rid)["notice"]["id"]

    assert edit(env, rid, {"tableIds": ["table-c"]})["statusCode"] == 200
    assert "notice" not in stored_item(env, rid)          # one-shot: old notice dropped

    assert edit(env, rid, {"startTime": "20:00"})["statusCode"] == 200
    moved = stored_item(env, rid)["notice"]
    assert moved["type"] == "changed" and moved["id"] != confirmed

    assert edit(env, rid, {"date": "2026-09-21", "notifyGuest": False})["statusCode"] == 200
    assert "notice" not in stored_item(env, rid, day="2026-09-21")  # INSERT carries no stale notice


def test_notify_flag_alone_is_not_an_edit(env):
    rid = body_of(guest_booking(env))["reservationId"]
    assert edit(env, rid, {"notifyGuest": False})["statusCode"] == 400


# --- special days ------------------------------------------------------------------

def staff_view(env, day=DAY, location=LOC_A):
    response = env["m"]["avail"].handler(
        {"requestContext": {"http": {"method": "GET"}}, "pathParameters": {"locationId": location},
         "queryStringParameters": {"date": day}}, None)
    assert response["statusCode"] == 200, response
    return body_of(response)


def test_a_closed_day_has_no_times_for_anyone(env):
    set_location(env, specialDays=[{"date": DAY, "kind": "closed", "note": "Privat fest"}])
    for view in (staff_view(env), guest_view(env, 2)):
        assert view["slots"] == []
        assert view["day"] == {"kind": "closed", "note": "Privat fest"}
    assert guest_booking(env)["statusCode"] == 400
    assert staff_booking(env)["statusCode"] == 400
    # Other days are untouched (and say nothing about a special day).
    other = staff_view(env, day="2026-09-21")
    assert other["slots"] and "day" not in other


def test_special_hours_replace_the_weekday_hours(env):
    set_location(env, specialDays=[{"date": DAY, "kind": "hours",
                                    "intervals": [{"opensAt": "17:00", "closesAt": "23:00"}]}])
    view = staff_view(env)
    assert [s["startTime"] for s in view["slots"]] == ["17:00", "17:30", "18:00", "18:30", "19:00",
                                                       "19:30", "20:00", "20:30", "21:00"]
    assert view["day"] == {"kind": "hours", "intervals": [{"opensAt": "17:00", "closesAt": "23:00"}]}
    assert guest_booking(env, start="12:00")["statusCode"] == 400
    assert guest_booking(env, start="21:00")["statusCode"] == 201


def test_a_day_without_online_booking_is_still_bookable_by_staff(env):
    set_location(env, specialDays=[{"date": DAY, "kind": "no_online"}])
    assert guest_view(env, 3)["slots"] == []
    assert guest_view(env, 3)["day"] == {"kind": "no_online"}
    assert staff_view(env)["slots"]
    refused = guest_booking(env)
    assert refused["statusCode"] == 400
    assert body_of(refused)["error"] == "online booking is closed for this date"
    assert staff_booking(env)["statusCode"] == 201


def test_a_booking_cannot_be_moved_to_a_closed_day(env):
    rid = body_of(guest_booking(env))["reservationId"]
    set_location(env, specialDays=[{"date": "2026-09-21", "kind": "closed"}])
    moved = edit(env, rid, {"date": "2026-09-21"})
    assert moved["statusCode"] == 400
    assert stored_item(env, rid)["date"] == DAY


# --- abuse protection -----------------------------------------------------------------

from shared import request_guard as shared_guard  # noqa: E402
from shared import turnstile as shared_turnstile  # noqa: E402

GUARD_TABLE = "test-request-guard"


def online_attempt(env, *, ip="203.0.113.5", phone="070-123 45 67", email="anna@example.se",
                   start="18:00", tables=("table-b",), party=3, **extra):
    payload = {"date": DAY, "startTime": start, "tableIds": list(tables), "partySize": party,
               "name": "Anna", "email": email, "phone": phone, "acceptTerms": True, **extra}
    e = event("POST", "/locations/{locationId}/reservations", body=payload)
    e["requestContext"]["http"]["sourceIp"] = ip
    return env["m"]["create"].handler(e, None)


@pytest.fixture
def guard(env, monkeypatch):
    env["res"].create_table(
        TableName=GUARD_TABLE, KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST")
    monkeypatch.setenv("REQUEST_GUARD_TABLE_NAME", GUARD_TABLE)
    monkeypatch.setattr(env["m"]["create"], "IP_LIMIT", (2, 600))
    monkeypatch.setattr(env["m"]["create"], "CONTACT_LIMIT", (2, 86400))
    return env


def test_one_ip_address_gets_a_few_booking_attempts(guard):
    starts = ("10:00", "12:00", "14:00")
    results = [online_attempt(guard, start=s, phone=f"07012345{i}0", email=f"g{i}@example.se")
               for i, s in enumerate(starts)]
    assert [r["statusCode"] for r in results] == [201, 201, 429]
    assert body_of(results[2]) == err("too_many_requests")
    assert 0 < int(results[2]["headers"]["Retry-After"]) <= 600
    # Another address is not affected.
    assert online_attempt(guard, ip="198.51.100.7", start="16:00", phone="0709999999",
                          email="other@example.se")["statusCode"] == 201


def test_one_phone_number_gets_a_few_bookings_a_day(guard):
    codes = [online_attempt(guard, ip=f"203.0.113.{i}", start=s, email=f"g{i}@example.se")["statusCode"]
             for i, s in enumerate(("10:00", "12:00", "14:00"))]
    assert codes == [201, 201, 429]


def test_attempts_that_book_nothing_dont_count_against_the_guest(guard):
    assert staff_booking(guard)["statusCode"] == 201  # table-b 18:00 is taken
    lost = [online_attempt(guard, ip=f"198.51.100.{i}")["statusCode"] for i in range(3)]
    assert lost == [409, 409, 409]
    codes = [online_attempt(guard, ip=f"203.0.113.{i}", start=s)["statusCode"]
             for i, s in enumerate(("10:00", "12:00", "14:00"))]
    assert codes == [201, 201, 429]


def test_the_guard_stores_no_raw_identifiers(guard):
    online_attempt(guard)
    rows = guard["res"].Table(GUARD_TABLE).scan()["Items"]
    assert len(rows) == 3  # ip, phone, email
    text = json.dumps(rows, default=str)
    assert "203.0.113.5" not in text and "46701234567" not in text and "anna@" not in text
    assert all(int(row["ttl"]) > 0 for row in rows)


def test_staff_bookings_are_not_limited(guard):
    codes = [staff_booking(guard, startTime=s)["statusCode"] for s in ("10:00", "12:00", "14:00")]
    assert codes == [201, 201, 201]


@pytest.fixture
def bot_check(env, monkeypatch):
    secret = boto3.client("secretsmanager", region_name=REGION).create_secret(
        Name="test/turnstile", SecretString=json.dumps({"secretKey": "1x0000000000000000000000000000000AA"}))
    monkeypatch.setenv("TURNSTILE_SECRET_ARN", secret["ARN"])
    monkeypatch.setenv("TURNSTILE_REQUIRED", "true")
    shared_turnstile.reset_cache()
    calls = []

    def answer(result):
        def post(payload):
            calls.append(payload)
            if isinstance(result, Exception):
                raise result
            return result
        monkeypatch.setattr(shared_turnstile, "_post", post)

    answer({"success": True, "action": "booking", "hostname": "roma.example"})
    yield {"env": env, "calls": calls, "answer": answer}
    shared_turnstile.reset_cache()


def test_online_booking_needs_the_bot_check(bot_check):
    env = bot_check["env"]
    missing = online_attempt(env)
    assert missing["statusCode"] == 400
    assert body_of(missing)["error"] == "turnstile token is required"
    assert bot_check["calls"] == []
    assert "table-b" in free_tables_at(env, "18:00")

    ok = online_attempt(env, turnstileToken="XXXX.DUMMY.TOKEN.XXXX")
    assert ok["statusCode"] == 201, ok
    sent = bot_check["calls"][0]
    assert sent["secret"] == "1x0000000000000000000000000000000AA"
    assert sent["response"] == "XXXX.DUMMY.TOKEN.XXXX" and sent["remoteip"] == "203.0.113.5"
    assert sent["idempotency_key"]


@pytest.mark.parametrize("result, status, error", [
    ({"success": False, "error-codes": ["invalid-input-response"]}, 400, "turnstile verification failed"),
    ({"success": False, "error-codes": ["timeout-or-duplicate"]}, 400, "turnstile token expired"),
    ({"success": True, "action": "catering"}, 400, "turnstile verification failed"),
    ({"success": False, "error-codes": ["invalid-input-secret"]}, 503, "verification_unavailable"),
])
def test_a_failed_bot_check_books_nothing(bot_check, result, status, error):
    env = bot_check["env"]
    bot_check["answer"](result)
    response = online_attempt(env, turnstileToken="token")
    assert response["statusCode"] == status
    assert body_of(response)["error"] == error
    assert "table-b" in free_tables_at(env, "18:00")


def test_cloudflare_unreachable_is_retried_once_then_unavailable(bot_check):
    import urllib.error
    env = bot_check["env"]
    bot_check["answer"](urllib.error.URLError("down"))
    response = online_attempt(env, turnstileToken="token")
    assert response["statusCode"] == 503
    assert len(bot_check["calls"]) == 2
    assert bot_check["calls"][0]["idempotency_key"] == bot_check["calls"][1]["idempotency_key"]


def test_staff_bookings_need_no_bot_check(bot_check):
    assert staff_booking(bot_check["env"])["statusCode"] == 201
    assert bot_check["calls"] == []


def test_without_the_widget_no_token_is_asked_for(env, monkeypatch):
    monkeypatch.delenv("TURNSTILE_REQUIRED", raising=False)
    assert online_attempt(env)["statusCode"] == 201

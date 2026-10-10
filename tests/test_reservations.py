"""Reservation core end to end: create (guest + staff), guest manage view and
cancel, staff list / status / edit / move - against moto DynamoDB with the
real availability engine, so a booking is validated exactly as the guest's
availability was computed. Includes double-booking and tenant isolation."""

import importlib.util
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from shared import dynamo as shared_dynamo
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
    assert stored["manageTokenHash"] != body["manageToken"] and "manageToken" not in stored
    assert stored["termsAcceptedAt"] and stored["ttl"] > int(datetime(2027, 9, 1).timestamp())


def test_same_table_twice_is_refused(env):
    assert guest_booking(env)["statusCode"] == 201
    second = guest_booking(env, party=2)
    assert second["statusCode"] == 409 and body_of(second) == {"error": "table_taken"}


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
    assert second["statusCode"] == 409 and body_of(second) == {"error": "table_taken"}


@pytest.mark.parametrize("change, message", [
    ({"startTime": "18:30"}, "startTime is not a bookable time"),
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


def test_two_tables_for_a_big_party(env):
    response = guest_booking(env, tables=("table-b", "table-c"), party=9)
    assert response["statusCode"] == 201
    assert body_of(response)["tableIds"] == ["table-b", "table-c"]
    assert free_tables_at(env, "18:00") == ["table-a"]


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
    assert response["statusCode"] == 409 and body_of(response) == {"error": "already_started"}


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


def test_arrive_no_show_and_undo_rules(env):
    rid = body_of(guest_booking(env))["reservationId"]
    env["clock"].value = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)  # 4 h before
    assert body_of(set_status(env, rid, "arrived")) == {"error": "too_early"}
    assert body_of(set_status(env, rid, "no_show")) == {"error": "grace_period_not_over"}

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
    assert body_of(set_status(env, rid, "cancelled")) == {"error": "not_cancellable"}


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
    assert response["statusCode"] == 409 and body_of(response) == {"error": "table_taken"}
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

    assert response["statusCode"] == 409 and body_of(response) == {"error": "table_taken"}
    listed = {i["reservationId"]: i["status"] for i in body_of(staff_list(env))["items"]}
    assert listed[rid] == "no_show"
    # Cancelling the no-show doesn't touch the walk-in's hold.
    assert body_of(set_status(env, rid, "cancelled")) == {"error": "not_cancellable"}


def test_late_arrival_after_the_end_is_refused(env):
    rid = body_of(guest_booking(env))["reservationId"]
    env["clock"].value = datetime(2026, 9, 20, 16, 20, tzinfo=timezone.utc)
    set_status(env, rid, "no_show")
    env["clock"].value = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)
    assert body_of(set_status(env, rid, "arrived")) == {"error": "already_ended"}


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

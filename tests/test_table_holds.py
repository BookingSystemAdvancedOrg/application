"""Table holds: the guest confirms their table(s), the tables are claimed for
15 minutes while they fill in their details, then the hold becomes the
booking - or runs out / is let go and the tables are free again. Same moto
setup as test_reservations (real availability engine, real locks)."""

from error_body import err
from datetime import timedelta

from boto3.dynamodb.conditions import Key

from test_guarantee import ACCOUNT, g, markers, stored  # noqa: F401
from test_reservations import (DAY, LOC_A, OCCUPANCY_TABLE, OWNER_A, body_of, env, event,  # noqa: F401
                               free_tables_at, guest_booking, load, staff_list)


def hold(env, tables=("table-b",), party=3, start="18:00"):
    return env["m"]["create"].handler(event(
        "POST", "/locations/{locationId}/holds",
        body={"date": DAY, "startTime": start, "tableIds": list(tables), "partySize": party}), None)


def release(env, hold_id, token):
    e = event("DELETE", "/locations/{locationId}/holds/{holdId}", headers={"x-manage-token": token})
    e["pathParameters"]["holdId"] = hold_id
    return env["m"]["create"].handler(e, None)


def book_held(env, held, **extra):
    payload = {"date": DAY, "startTime": held["startTime"], "tableIds": held["tableIds"],
               "partySize": held["partySize"], "name": "Anna Svensson", "email": "anna@example.se",
               "phone": "070-123 45 67", "acceptTerms": True, "holdId": held["holdId"], **extra}
    return env["m"]["create"].handler(event(
        "POST", "/locations/{locationId}/reservations", body=payload,
        headers={"x-manage-token": held["manageToken"]}), None)


def rows(env):
    return env["res"].Table(OCCUPANCY_TABLE).query(
        KeyConditionExpression=Key("PK").eq(f"LOCATION#{LOC_A}") & Key("SK").begins_with(f"SLOT#{DAY}#"))["Items"]


def later(env, minutes):
    env["clock"].value = env["clock"].value + timedelta(minutes=minutes)


# --- holding -----------------------------------------------------------------------------

def test_confirming_tables_holds_them_for_15_minutes(env):
    response = hold(env)

    assert response["statusCode"] == 201, response
    held = body_of(response)
    assert held["expiresAt"] == "2026-09-08T12:15:00Z" and held["holdMinutes"] == 15
    assert held["tableIds"] == ["table-b"] and held["manageToken"] and held["cardRequired"] is False
    assert "table-b" not in free_tables_at(env, "18:00")
    # Someone else can neither hold nor book it meanwhile.
    assert body_of(hold(env)) == err("table_taken")
    assert body_of(guest_booking(env)) == err("table_taken")
    # Staff don't see a hold as a booking.
    assert body_of(staff_list(env))["items"] == []


def test_the_held_tables_become_the_booking(env):
    held = body_of(hold(env))
    later(env, 10)

    response = book_held(env, held)

    assert response["statusCode"] == 201, response
    booking = body_of(response)
    assert booking["reservationId"] == held["holdId"] and booking["status"] == "reserved"
    assert booking["manageToken"] == held["manageToken"]
    item = stored(env, held["holdId"])
    assert item["customerName"] == "Anna Svensson" and item["notice"]["type"] == "confirmed"
    assert "pendingExpiresAt" not in item and "holdOnly" not in item and markers(env) == []
    assert all("holdExpiresAt" not in row for row in rows(env))  # no longer runs out
    later(env, 60)
    assert "table-b" not in free_tables_at(env, "18:00")
    assert [i["reservationId"] for i in body_of(staff_list(env))["items"]] == [held["holdId"]]


def test_a_hold_that_ran_out_frees_the_tables_at_once(env):
    held = body_of(hold(env))
    later(env, 15)

    assert "table-b" in free_tables_at(env, "18:00")  # before any sweep
    late = book_held(env, held)
    assert late["statusCode"] == 409 and body_of(late) == err("hold_expired")
    # Another guest takes the table over.
    taken = guest_booking(env, name="Bo Berg", email="bo@example.se", phone="0709999999")
    assert taken["statusCode"] == 201, taken
    assert body_of(book_held(env, held)) == err("hold_expired")

    # The sweep tidies the old hold without touching the new booking's row.
    reminders = load("reservation-reminders", "res_reminders_holds")
    assert reminders.handler({}, None)["expired"] == 1
    assert stored(env, held["holdId"])["status"] == "expired" and markers(env) == []
    assert [row["reservationId"] for row in rows(env)] == [body_of(taken)["reservationId"]]
    assert "table-b" not in free_tables_at(env, "18:00")


def test_letting_go_frees_the_tables_and_is_idempotent(env):
    held = body_of(hold(env))

    first = release(env, held["holdId"], held["manageToken"])
    second = release(env, held["holdId"], held["manageToken"])

    assert first["statusCode"] == 200 and body_of(first) == {"released": True}
    assert second["statusCode"] == 200 and body_of(second) == {"released": False}
    assert "table-b" in free_tables_at(env, "18:00") and rows(env) == []
    assert stored(env, held["holdId"])["status"] == "expired"
    assert body_of(book_held(env, held)) == err("hold_expired")


def test_letting_go_never_cancels_a_booking(env):
    held = body_of(hold(env))
    assert book_held(env, held)["statusCode"] == 201
    assert body_of(release(env, held["holdId"], held["manageToken"])) == {"released": False}
    assert stored(env, held["holdId"])["status"] == "reserved"


def test_hold_needs_its_token_and_the_same_tables(env):
    held = body_of(hold(env))
    assert release(env, held["holdId"], "wrong")["statusCode"] == 404
    wrong = book_held(env, {**held, "manageToken": "wrong"})
    assert wrong["statusCode"] == 404
    other = book_held(env, held, partySize=4)
    assert other["statusCode"] == 400 and "held date, time, tables" in body_of(other)["error"]
    assert stored(env, held["holdId"])["status"] == "held"


def test_hold_follows_the_online_rules(env):
    assert body_of(hold(env, tables=("table-c",), party=1))["error"].startswith("tableIds are not offered")
    response = hold(env, start="03:00")
    assert response["statusCode"] == 400


def test_card_step_keeps_the_hold_clock(g):
    held = body_of(hold(g))
    assert held["cardRequired"] is True
    g["stripe"].rid = held["holdId"]
    later(g, 5)

    response = book_held(g, held)

    assert response["statusCode"] == 201, response
    body = body_of(response)
    assert body["status"] == "pending" and body["setup"]["expiresAt"] == "2026-09-08T12:15:00Z"
    assert body["setup"]["stripeAccount"] == ACCOUNT
    assert len(markers(g)) == 1

    # Confirming in time keeps the table for good.
    confirmed = g["m"]["create"].handler(event(
        "POST", "/locations/{locationId}/reservations/{reservationId}/confirm",
        reservation=held["holdId"], headers={"x-manage-token": held["manageToken"]}), None)
    assert confirmed["statusCode"] == 200 and body_of(confirmed)["status"] == "reserved"
    assert all("holdExpiresAt" not in row for row in rows(g)) and markers(g) == []
    later(g, 60)
    assert "table-b" not in free_tables_at(g, "18:00")


def test_card_not_confirmed_before_the_hold_ran_out(g):
    held = body_of(hold(g))
    g["stripe"].rid = held["holdId"]
    assert book_held(g, held)["statusCode"] == 201
    later(g, 16)

    late = g["m"]["create"].handler(event(
        "POST", "/locations/{locationId}/reservations/{reservationId}/confirm",
        reservation=held["holdId"], headers={"x-manage-token": held["manageToken"]}), None)

    assert late["statusCode"] == 409 and body_of(late) == err("expired")
    assert "table-b" in free_tables_at(g, "18:00")


def test_hold_routes_and_methods(env):
    e = event("GET", "/locations/{locationId}/holds")
    assert env["m"]["create"].handler(e, None)["statusCode"] == 405
    e = event("POST", "/locations/{locationId}/holds/{holdId}")
    e["pathParameters"]["holdId"] = "a" * 32
    assert env["m"]["create"].handler(e, None)["statusCode"] == 405


def test_staff_block_sees_a_live_hold_and_may_take_over_an_expired_one(env, monkeypatch):
    block = load("block-table", "res_block_holds")
    monkeypatch.setattr(block, "_utc_now", env["clock"])
    hold(env)
    pk, prefix = f"LOCATION#{LOC_A}", f"SLOT#{DAY}#"
    (row,) = block._query_occupancies(pk, prefix)
    assert row["reservationId"] and row["holdExpiresAt"]

    later(env, 15)
    assert block._query_occupancies(pk, prefix) == []
    block._put_manual_item({"PK": row["PK"], "SK": row["SK"], "reservationId": "MANUAL_BLOCK#x", "ttl": 1}, {})
    assert [r["reservationId"] for r in rows(env)] == ["MANUAL_BLOCK#x"]

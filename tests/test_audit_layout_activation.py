"""Audit fixes for layout activation (#19, #25, #26).

#19: a 'scheduling' intent whose cutover passed without a schedule can be
cancelled, resumed (re-reserved at the earliest safe cutover) or moved.
#25: the default cutover lands on a closed time (local midnight after the
booking horizon), not on the publishing minute; the same in dev and prod.
#26: activation is refused while bookings after the cutover sit on tables
the new layout removes (only when RESERVATION_TABLE_NAME is set).
"""

from datetime import datetime, timezone
from decimal import Decimal

import boto3
import pytest

from error_body import err  # noqa: F401
from test_activate_layout_version import (  # noqa: F401
    EARLIEST_CUTOVER,
    NOW,
    activation_state,
    app_and_table,
    assert_empty_response,
    assert_response,
    make_event,
    make_pending_event,
    make_reschedule_event,
    pending_activation_state,
    response_body,
    snapshot_item,
    state_key,
    successful_scheduler,
)

OVERDUE_CUTOVER = "2026-09-07T10:00:00Z"  # NOW is 10:30Z
# A booking after the earliest replacement cutover at NOW (2026-09-29 00:00).
AFTER_CUTOVER = {"date": "2026-10-01", "ends_at": "2026-10-01T18:00:00Z"}
RESERVATION_TABLE = "test-reservations"


def put_overdue_scheduling(app, snapshot_table):
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    state = pending_activation_state(
        app,
        status="scheduling",
        cutover_at=OVERDUE_CUTOVER,
    )
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    return current, target, state


# --- #19 ---------------------------------------------------------------------


def test_overdue_scheduling_intent_can_be_cancelled(app_and_table, monkeypatch):
    app, snapshot_table = app_and_table
    put_overdue_scheduling(app, snapshot_table)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_pending_event(), None)

    assert_empty_response(response)
    stored = snapshot_table.get_item(Key=state_key())["Item"]
    assert "pendingVersion" not in stored
    assert "pendingStatus" not in stored


def test_overdue_scheduled_intent_still_cannot_be_cancelled(
    app_and_table, monkeypatch
):
    app, snapshot_table = app_and_table
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=OVERDUE_CUTOVER,
        expiresAt=OVERDUE_CUTOVER,
    )
    target = snapshot_item(2, effectiveFrom=OVERDUE_CUTOVER, expiresAt=None)
    state = pending_activation_state(app, cutover_at=OVERDUE_CUTOVER)
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    monkeypatch.setattr(
        app, "_get_scheduler_client", lambda: successful_scheduler()
    )

    response = app.handler(make_pending_event(), None)

    assert response["statusCode"] == 409
    assert snapshot_table.get_item(Key=state_key())["Item"] == state


def test_overdue_scheduling_intent_resumes_with_new_safe_cutover(
    app_and_table, monkeypatch
):
    app, snapshot_table = app_and_table
    put_overdue_scheduling(app, snapshot_table)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(
        response,
        202,
        {
            "status": "pending",
            "version": 2,
            "currentVersion": 1,
            "cutoverAt": EARLIEST_CUTOVER,
        },
    )
    stored = snapshot_table.get_item(Key=state_key())["Item"]
    assert stored["pendingStatus"] == "scheduled"
    assert stored["cutoverAt"] == EARLIEST_CUTOVER
    assert scheduler.create_schedule.call_args.kwargs[
        "ScheduleExpression"
    ] == "at(2026-09-28T22:00:00)"
    # the never-created schedule of the old intent is cleaned up first
    scheduler.delete_schedule.assert_called_once()


def test_overdue_scheduling_intent_can_be_rescheduled(
    app_and_table, monkeypatch
):
    app, snapshot_table = app_and_table
    put_overdue_scheduling(app, snapshot_table)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(
        make_reschedule_event(effective_from="2026-09-29T22:00:00Z"),
        None,
    )

    assert_response(response, 202)
    stored = snapshot_table.get_item(Key=state_key())["Item"]
    assert stored["pendingStatus"] == "scheduled"
    assert stored["cutoverAt"] == "2026-09-29T22:00:00Z"


# --- #25 ---------------------------------------------------------------------


def test_default_prod_cutover_is_next_local_midnight(app_and_table, monkeypatch):
    app, snapshot_table = app_and_table
    monkeypatch.setattr(app, "ENVIRONMENT", "prod")
    current = snapshot_item(1, is_current=True)
    # published long ago: the earliest switch still counts from today
    target = snapshot_item(2, createdAt="2026-08-10T17:10:00Z")
    for item in (current, target, activation_state()):
        snapshot_table.put_item(Item=item)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    # today 2026-09-07 + 21 bookable days + 1: 2026-09-29 00:00 CEST
    assert_response(
        response,
        202,
        {
            "status": "pending",
            "version": 2,
            "currentVersion": 1,
            "cutoverAt": "2026-09-28T22:00:00Z",
        },
    )


@pytest.mark.parametrize("environment", ["dev", "prod"])
def test_default_cutover_follows_location_timezone(
    app_and_table, monkeypatch, environment
):
    app, _ = app_and_table
    monkeypatch.setattr(app, "ENVIRONMENT", environment)

    london = app._replacement_transition({"mode": "default"}, snapshot_item(2), NOW, location={"timezone": "Europe/London"}
    )
    unknown = app._replacement_transition({"mode": "default"}, snapshot_item(2), NOW, location={"timezone": "Not/AZone"}
    )

    assert london == {"mode": "future", "effectiveFrom": "2026-09-28T23:00:00Z"}
    assert unknown == {"mode": "future", "effectiveFrom": "2026-09-28T22:00:00Z"}


def test_default_cutover_counts_from_the_local_date_at_midnight(app_and_table):
    app, _ = app_and_table
    # 2026-09-08 00:00 CEST exactly: today is 2026-09-08, earliest 2026-09-30
    midnight = datetime(2026, 9, 7, 22, 0, tzinfo=timezone.utc)
    # one microsecond earlier it is still 2026-09-07: earliest 2026-09-29
    before = datetime(2026, 9, 7, 21, 59, 59, 999999, tzinfo=timezone.utc)

    assert app._replacement_transition({"mode": "default"}, snapshot_item(2), midnight) == {
        "mode": "future",
        "effectiveFrom": "2026-09-29T22:00:00Z",
    }
    assert app._replacement_transition({"mode": "default"}, snapshot_item(2), before) == {
        "mode": "future",
        "effectiveFrom": "2026-09-28T22:00:00Z",
    }


def test_default_dev_cutover_matches_prod(app_and_table):
    app, _ = app_and_table
    assert app.ENVIRONMENT == "dev"
    assert app._replacement_transition({"mode": "default"}, snapshot_item(2), NOW) == {
        "mode": "future",
        "effectiveFrom": EARLIEST_CUTOVER,
    }
    with pytest.raises(app._TooEarly):
        app._replacement_transition({"mode": "immediate"}, snapshot_item(2), NOW)


# --- #26 ---------------------------------------------------------------------


def table_element(element_id):
    return {"elementId": element_id, "type": "table", "seats": 4}


@pytest.fixture
def reservations(app_and_table, monkeypatch):
    monkeypatch.setenv("RESERVATION_TABLE_NAME", RESERVATION_TABLE)
    resource = boto3.resource("dynamodb", region_name="eu-north-1")
    return resource.create_table(
        TableName=RESERVATION_TABLE,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def booking(reservation_id, *, date="2026-09-20", status="reserved",
            table_ids=("T7",), ends_at="2026-09-20T18:00:00Z",
            location_id="location-id", **extra):
    item = {
        "PK": f"LOCATION#{location_id}",
        "SK": f"RESERVATION#{date}#{reservation_id}",
        "reservationId": reservation_id,
        "date": date,
        "startTime": "18:00",
        "endsAt": ends_at,
        "tableIds": list(table_ids),
        "status": status,
        "customerName": "Gäst",
    }
    item.update(extra)
    return item


def put_layouts(snapshot_table):
    current = snapshot_item(
        1, is_current=True, elements=[table_element("T1"), table_element("T7")]
    )
    target = snapshot_item(2, elements=[table_element("T1")])
    for item in (current, target, activation_state()):
        snapshot_table.put_item(Item=item)
    return current, target


def test_activation_refused_for_bookings_on_removed_tables(
    app_and_table, reservations, monkeypatch
):
    app, snapshot_table = app_and_table
    put_layouts(snapshot_table)
    reservations.put_item(Item=booking("a" * 32, **AFTER_CUTOVER))
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    assert response["statusCode"] == 409
    body = response_body(response)
    assert body["code"] == "bookings_on_removed_tables"
    assert body["missingFrom"] == "new"
    assert body["reservations"] == [
        {
            "reservationId": "a" * 32,
            "date": "2026-10-01",
            "startTime": "18:00",
            "tableIds": ["T7"],
        }
    ]
    assert "Flytta" in body["error"]
    assert "customerName" not in str(body)
    assert "pendingVersion" not in snapshot_table.get_item(Key=state_key())["Item"]
    scheduler.create_schedule.assert_not_called()


def test_activation_ignores_irrelevant_bookings(
    app_and_table, reservations, monkeypatch
):
    app, snapshot_table = app_and_table
    put_layouts(snapshot_table)
    for item in (
        booking("b" * 32, status="cancelled_by_restaurant", **AFTER_CUTOVER),
        booking("c" * 32, table_ids=("T1",), **AFTER_CUTOVER),
        booking("d" * 32, status="held", holdOnly=True, **AFTER_CUTOVER),
        # ends at the cutover (2026-09-29 00:00 CEST)
        booking("e" * 32, date="2026-09-28", ends_at=EARLIEST_CUTOVER),
        # another location's booking on a T7 of its own
        booking("f" * 32, location_id="second-location", **AFTER_CUTOVER),
    ):
        reservations.put_item(Item=item)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(response, 202)
    assert response_body(response)["cutoverAt"] == EARLIEST_CUTOVER


def test_activation_check_skipped_without_reservation_table_env(
    app_and_table, monkeypatch
):
    app, snapshot_table = app_and_table
    monkeypatch.delenv("RESERVATION_TABLE_NAME", raising=False)
    put_layouts(snapshot_table)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(response, 202)
    assert response_body(response)["cutoverAt"] == EARLIEST_CUTOVER


def test_explicit_cutover_also_checks_bookings(
    app_and_table, reservations, monkeypatch
):
    app, snapshot_table = app_and_table
    current, _ = put_layouts(snapshot_table)
    reservations.put_item(Item=booking("a" * 32, **AFTER_CUTOVER))
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    # a replacement is never immediate: now is too early, whatever is booked
    response = app.handler(
        make_event(version_id="2", body='{"effectiveFrom": "2026-09-07T10:30:00Z"}'),
        None,
    )

    assert response["statusCode"] == 409
    assert response_body(response)["code"] == "too_early"
    assert response_body(response)["earliestCutoverAt"] == EARLIEST_CUTOVER

    # an explicit later cutover still has the booking after it
    response = app.handler(
        make_event(version_id="2", body='{"effectiveDate": "2026-09-30"}'),
        None,
    )

    assert response["statusCode"] == 409
    assert response_body(response)["code"] == "bookings_on_removed_tables"
    assert snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"]["isCurrent"] is True
    assert "pendingVersion" not in snapshot_table.get_item(Key=state_key())["Item"]
    scheduler.create_schedule.assert_not_called()


# --- review follow-ups -------------------------------------------------------


def test_overdue_scheduling_renewal_in_prod_lands_on_local_midnight(
    app_and_table, monkeypatch
):
    app, snapshot_table = app_and_table
    monkeypatch.setattr(app, "ENVIRONMENT", "prod")
    put_overdue_scheduling(app, snapshot_table)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    # NOW is 2026-09-07 10:30Z: bookings kept landing on the current
    # layout, so the renewal also waits out the booking horizon
    assert_response(
        response,
        202,
        {
            "status": "pending",
            "version": 2,
            "currentVersion": 1,
            "cutoverAt": "2026-09-28T22:00:00Z",
        },
    )


def _conflict_schema_validator(response_name):
    from pathlib import Path

    from openapi_schema_validator import OAS30Validator
    from openapi_spec_validator.readers import read_from_filename

    spec_path = Path(__file__).resolve().parents[1] / "docs" / "openapi.yaml"
    document, _ = read_from_filename(str(spec_path))
    media = document["components"]["responses"][response_name]["content"][
        "application/json"
    ]
    # local '#/components/...' refs resolve against the schema root
    schema = {
        "allOf": [media["schema"]],
        "components": document["components"],
    }
    return OAS30Validator(schema), media


@pytest.mark.parametrize(
    "response_name",
    ["LayoutActivationConflict", "PendingLayoutActivationConflict"],
)
def test_openapi_conflict_documents_bookings_on_removed_tables(response_name):
    validator, media = _conflict_schema_validator(response_name)
    body = {
        "error": "Det finns bokningar efter bytet.",
        "code": "bookings_on_removed_tables",
        "missingFrom": "new",
        "reservations": [
            {
                "reservationId": "a" * 32,
                "date": "2026-09-20",
                "startTime": "18:00",
                "tableIds": ["T7"],
            }
        ],
    }
    validator.validate(body)
    validator.validate({"error": "layout activation cutover is overdue"})
    example = media["examples"]["bookingsOnRemovedTables"]["value"]
    validator.validate(example)
    assert example["code"] == "bookings_on_removed_tables"

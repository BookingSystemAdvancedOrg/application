"""A new floor plan takes effect only after the guest booking horizon plus
one day, so no booking that exists when the switch is booked lands on a
layout that changed under it."""

import json
from datetime import date

from shared.availability import BOOKING_HORIZON_DAYS, earliest_layout_change_date
from test_activate_layout_version import (  # noqa: F401
    activation_state,
    app_and_table,
    assert_response,
    make_event,
    response_body,
    snapshot_item,
    successful_scheduler,
    too_early,
)

# NOW in the activation tests: 2026-09-07 10:30Z (12:30 in Stockholm).
EARLIEST_PROD = "2026-09-28T22:00:00Z"  # 2026-09-29 00:00 CEST


def test_earliest_change_date_is_day_after_last_bookable_date():
    assert BOOKING_HORIZON_DAYS == 21
    assert earliest_layout_change_date(date(2026, 11, 18)) == date(2026, 12, 10)


def _replace(app, snapshot_table, monkeypatch, body, environment="prod"):
    monkeypatch.setattr(app, "ENVIRONMENT", environment)
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2, createdAt="2026-08-01T10:00:00Z")
    for item in (current, target, activation_state()):
        snapshot_table.put_item(Item=item)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)
    return app.handler(make_event(version_id="2", body=json.dumps(body)), None)


def test_prod_replacement_is_never_immediate(app_and_table, monkeypatch):
    app, snapshot_table = app_and_table
    response = _replace(app, snapshot_table, monkeypatch, {})
    assert response["statusCode"] == 202
    assert response_body(response)["cutoverAt"] == EARLIEST_PROD


def test_prod_effective_date_on_earliest_day_is_accepted(app_and_table, monkeypatch):
    app, snapshot_table = app_and_table
    response = _replace(app, snapshot_table, monkeypatch, {"effectiveDate": "2026-09-29"})
    assert response["statusCode"] == 202
    assert response_body(response)["cutoverAt"] == EARLIEST_PROD


def test_prod_effective_date_later_is_local_midnight(app_and_table, monkeypatch):
    app, snapshot_table = app_and_table
    # 2026-10-26 is after the switch to CET (UTC+1)
    response = _replace(app, snapshot_table, monkeypatch, {"effectiveDate": "2026-10-26"})
    assert response_body(response)["cutoverAt"] == "2026-10-25T23:00:00Z"


def test_prod_effective_date_inside_horizon_is_too_early(app_and_table, monkeypatch):
    app, snapshot_table = app_and_table
    response = _replace(app, snapshot_table, monkeypatch, {"effectiveDate": "2026-09-28"})
    assert_response(response, 409, too_early(EARLIEST_PROD))


def test_prod_effective_from_inside_horizon_is_too_early(app_and_table, monkeypatch):
    app, snapshot_table = app_and_table
    response = _replace(
        app, snapshot_table, monkeypatch, {"effectiveFrom": "2026-09-20T22:00:00Z"}
    )
    assert_response(response, 409, too_early(EARLIEST_PROD))


def test_dev_effective_date_today_means_earliest(app_and_table, monkeypatch):
    app, snapshot_table = app_and_table
    response = _replace(app, snapshot_table, monkeypatch, {"effectiveDate": "2026-09-07"}, "dev")
    # published long ago, still never immediate: dev follows the prod rule
    assert response["statusCode"] == 202
    assert response_body(response)["cutoverAt"] == EARLIEST_PROD


def test_effective_date_and_from_together_is_400(app_and_table, monkeypatch):
    app, snapshot_table = app_and_table
    response = _replace(
        app,
        snapshot_table,
        monkeypatch,
        {"effectiveDate": "2026-09-29", "effectiveFrom": "2026-09-28T22:00:00Z"},
    )
    assert response["statusCode"] == 400


def test_bad_effective_date_is_400(app_and_table, monkeypatch):
    app, snapshot_table = app_and_table
    for bad in ("29/09/2026", "2026-13-01", 20260929, "2026-09-01"):
        snapshot_table.delete_item(Key={"PK": activation_state()["PK"], "SK": activation_state()["SK"]})
        response = _replace(app, snapshot_table, monkeypatch, {"effectiveDate": bad})
        assert response["statusCode"] == 400, bad

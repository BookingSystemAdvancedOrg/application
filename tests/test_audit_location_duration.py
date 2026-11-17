"""Audit #29: a booking length the availability engine rejects must be
refused on save (400), never stored."""
import json

import pytest

from test_create_location import (  # noqa: F401  (fixture import)
    app_and_table,
    get_location,
    make_event,
    put_location,
    table_items,
    valid_body,
)


@pytest.mark.parametrize("value", [0.33, 24, 9, 0.1, 1.1])
def test_create_rejects_booking_length_the_engine_cannot_use(app_and_table, value):
    app, location_table = app_and_table
    body = valid_body()
    body["bookingDurationHours"] = value
    body["gracePeriodHours"] = 0.25
    response = app.handler(make_event(body), None)
    assert response["statusCode"] == 400, response
    assert "bookingDurationHours" in json.loads(response["body"])["error"]
    assert table_items(location_table) == []


@pytest.mark.parametrize("value", [0.33, 24])
def test_update_rejects_booking_length_the_engine_cannot_use(app_and_table, value):
    app, location_table = app_and_table
    put_location(location_table)
    response = app.handler(
        make_event({"bookingDurationHours": value, "gracePeriodHours": 0.25},
                   method="PUT", location_id="location-id"),
        None,
    )
    assert response["statusCode"] == 400, response
    assert get_location(location_table)["bookingDurationHours"] == 2


@pytest.mark.parametrize("value", [0.5, 1.25, 1.5, 4, 8])
def test_quarter_hour_lengths_up_to_eight_hours_are_accepted(app_and_table, value):
    app, location_table = app_and_table
    body = valid_body()
    body["bookingDurationHours"] = value
    body["gracePeriodHours"] = 0.25
    response = app.handler(make_event(body), None)
    assert response["statusCode"] == 201, response

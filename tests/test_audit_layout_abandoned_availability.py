"""Audit #19: availability serves the current layout once a 'scheduling'
layout intent is abandoned (15 minutes past its cutover with no schedule)."""

from error_body import err
from test_get_availability import (  # noqa: F401
    app_and_tables,
    assert_response,
    location_item,
    make_event,
    pending_activation_state,
    response_body,
    snapshot_item,
)


def put_scheduling(app, tables, cutover_at):
    tables["location"].put_item(Item=location_item())
    tables["snapshot"].put_item(
        Item=pending_activation_state(
            app, status="scheduling", cutover_at=cutover_at
        )
    )
    tables["snapshot"].put_item(Item=snapshot_item())


def test_abandoned_scheduling_intent_serves_current_layout(app_and_tables):
    app, tables = app_and_tables
    put_scheduling(app, tables, "2026-09-08T11:40:00Z")  # NOW is 12:00Z

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 200
    slots = response_body(response)["slots"]
    assert slots
    assert {slot["layoutVersion"] for slot in slots} == {1}


def test_recent_scheduling_intent_still_conflicts(app_and_tables):
    app, tables = app_and_tables
    put_scheduling(app, tables, "2026-09-08T11:50:00Z")

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        err("layout activation is still being scheduled; retry request"),
    )

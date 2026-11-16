"""Audit #19: block-table keeps working on the current layout once a
'scheduling' layout intent is abandoned (15 minutes past its cutover with no
schedule), instead of answering 409 until someone edits DynamoDB."""

from decimal import Decimal

from error_body import err
from test_block_table import (  # noqa: F401
    app_and_tables,
    assert_response,
    make_event,
    manual_item,
    put_scheduled_layout_transition,
    slot_key,
    table_element,
)


def test_abandoned_scheduling_intent_no_longer_blocks(app_and_tables):
    app, tables = app_and_tables
    put_scheduled_layout_transition(
        app,
        tables,
        status="scheduling",
        cutover_at="2026-09-08T11:40:00Z",  # NOW is 12:00Z
        current_elements=[table_element()],
    )

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 201
    assert tables["occupancy"].get_item(
        Key=slot_key(),
        ConsistentRead=True,
    )["Item"] == manual_item(layoutVersion=Decimal("1"))


def test_recent_scheduling_intent_still_conflicts(app_and_tables):
    app, tables = app_and_tables
    put_scheduled_layout_transition(
        app,
        tables,
        status="scheduling",
        cutover_at="2026-09-08T11:50:00Z",
        current_elements=[table_element()],
    )

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        err("layout activation is still being scheduled; retry request"),
    )

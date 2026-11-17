"""Follow-up to audit #19/#26: renewing an overdue 'scheduling' intent picks
a new cutover, so it must run the same booking check as a new activation.
Bookings made after the intent looked abandoned may sit on tables the
target layout drops.
"""

from error_body import err  # noqa: F401
from test_activate_layout_version import (  # noqa: F401
    activation_state,
    app_and_table,
    make_event,
    pending_activation_state,
    response_body,
    snapshot_item,
    state_key,
    successful_scheduler,
)
from test_audit_layout_activation import (  # noqa: F401
    OVERDUE_CUTOVER,
    booking,
    reservations,
    table_element,
)


def put_overdue_scheduling_dropping_t7(app, snapshot_table):
    current = snapshot_item(
        1, is_current=True, elements=[table_element("T1"), table_element("T7")]
    )
    target = snapshot_item(2, elements=[table_element("T1")])
    state = pending_activation_state(
        app,
        status="scheduling",
        cutover_at=OVERDUE_CUTOVER,
    )
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    return state


def test_overdue_scheduling_renewal_refused_for_bookings_on_removed_tables(
    app_and_table, reservations, monkeypatch
):
    app, snapshot_table = app_and_table
    state = put_overdue_scheduling_dropping_t7(app, snapshot_table)
    reservations.put_item(Item=booking("a" * 32))
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    assert response["statusCode"] == 409
    body = response_body(response)
    assert body["code"] == "bookings_on_removed_tables"
    assert body["reservations"][0]["tableIds"] == ["T7"]
    # nothing renewed: state untouched, no schedule created
    assert snapshot_table.get_item(Key=state_key())["Item"] == state
    scheduler.create_schedule.assert_not_called()


def test_overdue_scheduling_renewal_ignores_bookings_on_kept_tables(
    app_and_table, reservations, monkeypatch
):
    app, snapshot_table = app_and_table
    put_overdue_scheduling_dropping_t7(app, snapshot_table)
    reservations.put_item(Item=booking("c" * 32, table_ids=("T1",)))
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    assert response["statusCode"] == 202
    assert response_body(response)["cutoverAt"] == "2026-09-07T10:31:00Z"

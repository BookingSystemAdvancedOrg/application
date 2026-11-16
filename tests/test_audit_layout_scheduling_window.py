"""Audit #19 follow-up: a 'scheduling' intent that availability already
treats as abandoned must never be activated later by a retried schedule.

The worker gives up on a due 'scheduling' intent once it is more than
_SCHEDULING_RECOVERY_WINDOW (10 min) past its cutover, which is shorter than
the 15 minutes after which availability and block-table serve the current
layout again. So bookings made on the current layout after that point can
never be moved under a later layout flip.
"""

import copy
from datetime import timedelta

import pytest

from test_expire_layout_version import (  # noqa: F401
    NOW,
    app_and_table,
    event_for_state,
    put_ready_cutover,
    put_scheduling_cutover,
)


def stored(snapshot_table, item):
    return snapshot_table.get_item(
        Key={"PK": item["PK"], "SK": item["SK"]}
    )["Item"]


def test_late_scheduling_recovery_is_a_noop(app_and_table, monkeypatch):
    app, snapshot_table = app_and_table
    items = put_scheduling_cutover(snapshot_table, app)
    originals = [copy.deepcopy(item) for item in items]
    monkeypatch.setattr(app, "_utc_now", lambda: NOW + timedelta(minutes=10))

    assert app.handler(event_for_state(items[2]), None) is None

    for original in originals:
        assert stored(snapshot_table, original) == original


def test_scheduling_recovery_inside_window_still_completes(
    app_and_table, monkeypatch
):
    app, snapshot_table = app_and_table
    outgoing, target, state = put_scheduling_cutover(snapshot_table, app)
    monkeypatch.setattr(
        app, "_utc_now", lambda: NOW + timedelta(minutes=9, seconds=59)
    )

    assert app.handler(event_for_state(state), None) is None

    assert stored(snapshot_table, target)["isCurrent"] is True


def test_recovery_window_is_shorter_than_availability_abandon_rule(
    app_and_table,
):
    import shared.availability as availability

    app, _ = app_and_table
    assert (
        app._SCHEDULING_RECOVERY_WINDOW
        < availability._ABANDONED_SCHEDULING_AFTER
    )


@pytest.mark.parametrize("late", [timedelta(minutes=30), timedelta(hours=7)])
def test_late_scheduled_cutover_still_completes(app_and_table, monkeypatch, late):
    app, snapshot_table = app_and_table
    outgoing, target, state = put_ready_cutover(snapshot_table, app)
    monkeypatch.setattr(app, "_utc_now", lambda: NOW + late)

    assert app.handler(event_for_state(state), None) is None

    assert stored(snapshot_table, target)["isCurrent"] is True

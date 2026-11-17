"""Audit #46: queue entries from before midnight stay reachable while the
opening hours run past midnight. Audit #64: a Scheduler failure after the
offer commit neither fails the offer nor loses the guest's SMS."""
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from shared import reservations as shared_r
from shared import tenant as shared_tenant
from shared.features import waitlist as wl
from tenant_support import LOC_A, TENANT_A
from test_waitlist import (DAY, E1, E2, LOCATION_TABLE, OCCUPANCY_TABLE, Q, RESERVATION_TABLE,  # noqa: F401
                           WAITLIST_TABLE, call, entry_row, env, offer, walk_in)

NEXT_DAY = "2026-09-21"
AFTER_MIDNIGHT = datetime(2026, 9, 20, 22, 10, tzinfo=timezone.utc)  # 00:10 Stockholm on Monday


def open_past_midnight(e):
    e.res.Table(LOCATION_TABLE).update_item(
        Key={"PK": f"TENANT#{TENANT_A}", "SK": f"LOCATION#{LOC_A}"},
        UpdateExpression="SET businessHours = :h",
        ExpressionAttributeValues={":h": {d: [{"opensAt": "10:00", "closesAt": "02:00"}] for d in (
            "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")}})
    shared_tenant.reset_caches()


def test_queue_from_before_midnight_is_listed_after_midnight(env):
    open_past_midnight(env)
    walk_in(env)
    env.clock.value = AFTER_MIDNIGHT
    status, body = call(env, "GET", Q)
    assert status == 200
    assert [x["entryId"] for x in body["entries"]] == [E1]
    assert body["suggestions"] and body["suggestions"][0]["entryId"] == E1


def test_queue_from_before_midnight_is_not_listed_after_closing(env):
    walk_in(env)  # the fixture location closes at 22:00
    env.clock.value = AFTER_MIDNIGHT
    status, body = call(env, "GET", Q)
    assert status == 200 and body["entries"] == []


def test_offer_after_midnight_holds_a_table_on_the_new_date(env):
    open_past_midnight(env)
    walk_in(env)
    env.clock.value = AFTER_MIDNIGHT
    status, body = offer(env, start="00:00")
    assert status == 200, body
    assert body["status"] == "offered" and body["date"] == DAY
    held = shared_r.load(LOC_A, body["offer"]["reservationId"])
    assert held["date"] == NEXT_DAY and held["status"] == "held"
    status, seated = call(env, "POST", Q + "/{entryId}/seat", entry=E1, body={"version": int(body["version"])})
    assert status == 200 and seated["entry"]["status"] == "seated"
    assert shared_r.load(LOC_A, seated["reservationId"])["status"] == "arrived"


def test_seat_and_remove_after_midnight(env):
    open_past_midnight(env)
    walk_in(env)
    walk_in(env, entry=E2, phone="0702222222")
    env.clock.value = AFTER_MIDNIGHT
    status, body = call(env, "POST", Q + "/{entryId}/seat", entry=E1, body={"tableIds": ["table-b"], "version": 1})
    assert status == 200, body
    assert shared_r.load(LOC_A, body["reservationId"])["date"] == NEXT_DAY
    status, body = call(env, "POST", Q + "/{entryId}/remove", entry=E2, body={"version": 1})
    assert status == 200 and body["status"] == "removed"


class BrokenScheduler:
    def delete_schedule(self, **kwargs):
        raise ClientError({"Error": {"Code": "ThrottlingException"}}, "DeleteSchedule")

    def create_schedule(self, **kwargs):
        raise ClientError({"Error": {"Code": "ThrottlingException"}}, "CreateSchedule")


def test_scheduler_failure_still_offers_and_texts_the_guest(env):
    walk_in(env)
    env.api._clients["scheduler"] = BrokenScheduler()
    try:
        status, body = offer(env)
    finally:
        env.api._clients.pop("scheduler", None)
    assert status == 200 and body["status"] == "offered"
    offered = [n for n in env.sms.calls if n["notice"]["kind"] == "waitlist.offered"]
    assert len(offered) == 1
    assert entry_row(env)["notice"]["sentAt"]


def test_retry_sends_the_offer_sms_that_was_never_sent(env):
    walk_in(env)
    env.sms.fail = True
    status, body = offer(env)
    assert status == 200 and "sentAt" not in entry_row(env)["notice"]
    env.sms.fail = False
    again = offer(env, version=body["version"])
    assert again[0] == 200
    offered = [n for n in env.sms.calls if n["notice"]["kind"] == "waitlist.offered"]
    assert len(offered) == 1
    assert entry_row(env)["notice"]["sentAt"]
    # a further retry does not text twice
    offer(env, version=entry_row(env)["version"])
    assert len([n for n in env.sms.calls if n["notice"]["kind"] == "waitlist.offered"]) == 1

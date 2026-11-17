"""Audit findings 40, 41, 51 and 60 (personalliggare): corrections move
presence, late registrations can be corrected, the chain verifies after the
oldest rows expired by TTL, and an export too large for one response is
refused with a clear error instead of failing in Lambda."""

from datetime import datetime, timedelta, timezone

from shared.features import personalliggare as pl
from test_personalliggare import (BASE, DAY, LEDGER, LOC_A, NOW, P_HELPER, TENANT_A, TENANT_B, call, check,  # noqa: F401
                                  correction, env, ledger, platform)


def present_ids(e):
    return [p["personId"] for p in call(e, "GET", BASE, query={"day": DAY})[1]["present"]]


# --- 40: a correction that changes the kind moves presence ----------------------------------------

def test_correcting_a_check_in_to_a_check_out_clears_presence(env):
    _, rec = check(env, "check-in", person=P_HELPER)
    assert present_ids(env) == [P_HELPER]
    status, _ = correction(env, correctsSeq=rec["seq"], kind="check_out", eventTime="2026-11-05T07:30:00Z",
                           reason="Felaktig incheckning")
    assert status == 201
    assert present_ids(env) == []
    _, inspection = call(env, "GET", BASE + "/inspection", query={"day": DAY})
    assert inspection["present"] == []
    assert pl.verify(ledger(env), pl.get_head(LOC_A)) is None


def test_correcting_a_check_out_to_a_check_in_opens_presence(env):
    check(env, "check-in", person=P_HELPER)
    env.clock.value = NOW + timedelta(minutes=5)
    _, out = check(env, "check-out", person=P_HELPER)
    assert present_ids(env) == []
    status, _ = correction(env, correctsSeq=out["seq"], kind="check_in", eventTime="2026-11-05T07:00:00Z",
                           reason="Var aldrig utcheckad")
    assert status == 201
    assert present_ids(env) == [P_HELPER]


def test_kind_change_back_and_forth_follows_the_latest_fix(env):
    _, rec = check(env, "check-in", person=P_HELPER)
    env.clock.value = NOW + timedelta(minutes=1)
    assert correction(env, correctsSeq=rec["seq"], kind="check_out", eventTime="2026-11-05T07:00:00Z")[0] == 201
    assert present_ids(env) == []
    env.clock.value = NOW + timedelta(minutes=2)
    assert correction(env, correctsSeq=rec["seq"], kind="check_in", eventTime="2026-11-05T07:00:00Z")[0] == 201
    assert present_ids(env) == [P_HELPER]
    env.clock.value = NOW + timedelta(minutes=3)
    assert correction(env, correctsSeq=rec["seq"], kind="check_out", eventTime="2026-11-05T07:00:00Z")[0] == 201
    assert present_ids(env) == []


def test_kind_change_leaves_a_later_shift_alone(env):
    check(env, "check-in", person=P_HELPER)
    env.clock.value = NOW + timedelta(minutes=1)
    _, out = check(env, "check-out", person=P_HELPER)
    env.clock.value = NOW + timedelta(minutes=2)
    check(env, "check-in", person=P_HELPER)
    env.clock.value = NOW + timedelta(minutes=3)
    # Present again through seq 3: correcting seq 2 to a check-in neither fails nor opens twice.
    assert correction(env, correctsSeq=out["seq"], kind="check_in", eventTime="2026-11-05T07:00:00Z")[0] == 201
    assert present_ids(env) == [P_HELPER]
    # seq 1 is not the open shift: turning it into a check-out does not close seq 3.
    assert correction(env, correctsSeq=1, kind="check_out", eventTime="2026-11-05T07:00:00Z")[0] == 201
    assert present_ids(env) == [P_HELPER]


def test_a_shift_opened_after_the_presence_read_is_not_closed_by_the_correction(env, monkeypatch):
    _, rec = check(env, "check-in", person=P_HELPER)
    read, raced = pl.get_present, []

    def get_present_then_race(location_id, person_id):
        row = read(location_id, person_id)
        if not raced:  # between the read and the write the person checks out and in again
            raced.append(1)
            env.clock.value = NOW + timedelta(minutes=1)
            check(env, "check-out", person=P_HELPER)
            env.clock.value = NOW + timedelta(minutes=2)
            check(env, "check-in", person=P_HELPER)
        return row

    monkeypatch.setattr(pl, "get_present", get_present_then_race)
    status, body = correction(env, correctsSeq=rec["seq"], kind="check_out", eventTime="2026-11-05T07:00:00Z")
    assert (status, body["code"]) == (409, "not_checked_in")
    assert present_ids(env) == [P_HELPER]
    assert int(read(LOC_A, P_HELPER)["checkInSeq"]) == 3


def test_same_kind_correction_does_not_touch_presence(env):
    _, rec = check(env, "check-in", person=P_HELPER)
    assert correction(env, correctsSeq=rec["seq"], eventTime="2026-11-05T07:15:00Z")[0] == 201
    assert present_ids(env) == [P_HELPER]


# --- 41: a late registration can be corrected ------------------------------------------------------

def test_a_late_registration_can_be_corrected(env):
    _, late = correction(env, eventTime="2026-11-05T05:00:00Z", reason="Glömde checka in")
    status, fix = correction(env, correctsSeq=late["seq"], eventTime="2026-11-05T07:00:00Z",
                             reason="Fel tid i efterregistreringen")
    assert (status, fix["correctsSeq"]) == (201, late["seq"])
    _, day = call(env, "GET", BASE, query={"day": DAY})
    assert len(day["records"]) == 1
    row = day["records"][0]
    assert (row["seq"], row["eventTime"], row["late"], row["correctedBySeq"]) == (
        late["seq"], "2026-11-05T07:00:00Z", True, fix["seq"])
    assert row["original"]["eventTime"] == "2026-11-05T05:00:00Z"
    assert present_ids(env) == [P_HELPER]


def test_a_late_check_in_corrected_to_a_check_out_clears_presence(env):
    _, late = correction(env)
    assert present_ids(env) == [P_HELPER]
    assert correction(env, correctsSeq=late["seq"], kind="check_out")[0] == 201
    assert present_ids(env) == []


def test_a_correction_of_a_correction_is_still_not_found(env):
    _, rec = check(env, "check-in", person=P_HELPER)
    _, fix = correction(env, correctsSeq=rec["seq"], eventTime="2026-11-05T07:15:00Z")
    assert correction(env, correctsSeq=fix["seq"])[0] == 404


# --- 51: verification after TTL expiry -----------------------------------------------------------------

def _expire(e, *seqs):
    for seq in seqs:  # what DynamoDB TTL does (moto does not run TTL)
        e.res.Table(LEDGER).delete_item(Key={"PK": f"LOCATION#{LOC_A}", "SK": pl.seq_sk(seq)})


# Rows written on NOW (2026) expire on 1 January 2029, rows of 2027 a year later.
LATER = datetime(2027, 6, 1, 8, 0, tzinfo=timezone.utc)
LATER_DAY = "2027-06-01"
AFTER_2026_EXPIRED = datetime(2029, 1, 2, 12, 0, tzinfo=timezone.utc)
AFTER_ALL_EXPIRED = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)


def _two_years(e):
    """Seq 1-2 in 2026, seq 3-4 in 2027."""
    check(e, "check-in", person=P_HELPER)
    check(e, "check-out", person=P_HELPER)
    e.clock.value = LATER
    check(e, "check-in", person=P_HELPER)
    check(e, "check-out", person=P_HELPER)


def _export(e, first=LATER_DAY, last=LATER_DAY):
    return call(e, "GET", BASE + "/export", query={"from": first, "to": last})


def test_verify_accepts_an_expired_prefix_only_when_asked(env):
    _two_years(env)
    for _ in range(2):
        check(env, "check-in", person=P_HELPER)
        check(env, "check-out", person=P_HELPER)
    at = AFTER_2026_EXPIRED
    records, head = pl.unexpired(ledger(env), at), pl.get_head(LOC_A)
    assert [int(r["seq"]) for r in records] == [3, 4, 5, 6, 7, 8]
    assert pl.verify(records, head, at=at) == 1
    assert pl.verify(records, head, expired_prefix=True, at=at) is None
    assert pl.verify([r for r in records if int(r["seq"]) != 5], head, expired_prefix=True, at=at) == 5
    tampered = [dict(r) for r in records]
    tampered[2]["name"] = "Någon annan"
    assert pl.verify(tampered, head, expired_prefix=True, at=at) == 5


def test_a_missing_prefix_before_anything_can_expire_is_a_deletion(env):
    for _ in range(2):
        check(env, "check-in", person=P_HELPER)
        check(env, "check-out", person=P_HELPER)
    records, head = ledger(env), pl.get_head(LOC_A)
    assert head["ttlStarts"] == [[pl.ttl_for(NOW), 1]] and head["lastTtl"] == pl.ttl_for(NOW)
    assert pl.verify(records[2:], head, expired_prefix=True, at=NOW) == 1
    assert pl.verify([], head, expired_prefix=True, at=NOW) == 1
    _expire(env, 1, 2)
    status, out = call(env, "GET", BASE + "/export", query={"from": DAY, "to": DAY})
    assert status == 200 and out["chainOk"] is False and out["firstBadSeq"] == 1
    assert "expiredThroughSeq" not in out


def test_an_old_head_without_ttl_starts_allows_expiry_from_2027(env):
    check(env, "check-in", person=P_HELPER)
    head = {k: v for k, v in pl.get_head(LOC_A).items() if k not in ("ttlStarts", "lastTtl")}
    assert pl.verify([], head, expired_prefix=True, at=NOW) == 1
    assert pl.verify([], head, expired_prefix=True, at=datetime(2027, 1, 2, tzinfo=timezone.utc)) is None


def test_export_after_ttl_expiry_reports_chain_ok_and_the_expired_prefix(env):
    _two_years(env)
    _expire(env, 1, 2)
    env.clock.value = AFTER_2026_EXPIRED
    status, out = _export(env)
    assert status == 200 and out["chainOk"] is True and out["expiredThroughSeq"] == 2 and out["count"] == 2
    hook = platform(env, TENANT_A, {"locationId": LOC_A, "from": LATER_DAY, "to": LATER_DAY})
    assert hook["ok"] and hook["body"]["chainOk"] is True and hook["body"]["expiredThroughSeq"] == 2


def test_rows_past_their_ttl_count_as_expired_while_dynamodb_deletes_them_unordered(env):
    # TTL deletes a whole year lazily and in no set order: seq 1 gone, seq 2 still there.
    _two_years(env)
    _expire(env, 1)
    env.clock.value = AFTER_2026_EXPIRED
    status, out = _export(env)
    assert status == 200 and out["chainOk"] is True and out["expiredThroughSeq"] == 2 and out["count"] == 2
    assert all(int(r["seq"]) > 2 for r in pl.export_rows(pl.unexpired(ledger(env), AFTER_2026_EXPIRED),
                                                          LATER_DAY, LATER_DAY))


def test_an_unexpired_row_missing_after_the_expired_prefix_is_still_a_gap(env):
    _two_years(env)
    _expire(env, 1, 2, 3)
    env.clock.value = AFTER_2026_EXPIRED
    status, out = _export(env)
    assert status == 200 and out["chainOk"] is False and out["firstBadSeq"] == 3
    assert "expiredThroughSeq" not in out


def test_head_keeps_where_each_ttl_starts_and_ttl_never_goes_back(env):
    _two_years(env)
    head = pl.get_head(LOC_A)
    assert head["ttlStarts"] == [[pl.ttl_for(NOW), 1], [pl.ttl_for(LATER), 3]]
    # A late registration backdated into 2026 keeps the 2027 ttl of the records before it.
    assert correction(env, eventTime="2026-11-05T06:00:00Z")[0] == 201
    assert int(ledger(env)[-1]["ttl"]) == pl.ttl_for(LATER)
    assert pl.get_head(LOC_A)["ttlStarts"] == head["ttlStarts"]


def test_an_old_head_learns_ttl_starts_on_its_next_record(env):
    check(env, "check-in", person=P_HELPER)
    env.res.Table("test-personalliggare").update_item(Key=pl.head_key(LOC_A),
                                                      UpdateExpression="REMOVE ttlStarts, lastTtl")
    env.clock.value = LATER
    check(env, "check-out", person=P_HELPER)
    assert pl.get_head(LOC_A)["ttlStarts"] == [[pl.ttl_for(LATER), 2]]


def test_a_fully_expired_chain_is_ok_and_reports_the_whole_prefix(env):
    _two_years(env)
    _expire(env, 1, 3)  # seq 2 and 4 are past their ttl but not deleted yet
    env.clock.value = AFTER_ALL_EXPIRED
    status, out = _export(env, "2029-06-01", "2029-06-01")
    assert status == 200 and out["chainOk"] is True and out["expiredThroughSeq"] == 4 and out["count"] == 0
    _expire(env, 2, 4)
    hook = platform(env, TENANT_A, {"locationId": LOC_A, "from": "2029-06-01", "to": "2029-06-01"})
    assert hook["ok"] and hook["body"]["chainOk"] is True and hook["body"]["expiredThroughSeq"] == 4
    assert platform(env, TENANT_B, {"locationId": LOC_A, "from": "2029-06-01", "to": "2029-06-01"})["status"] == 404


def test_export_without_expiry_has_no_expired_prefix(env):
    check(env, "check-in", person=P_HELPER)
    out = call(env, "GET", BASE + "/export", query={"from": DAY, "to": DAY})[1]
    assert out["chainOk"] is True and "expiredThroughSeq" not in out


def test_backdated_record_expires_with_its_neighbours(env):
    # A late registration recorded in 2026 for an event in 2025 must not leave a hole in
    # the chain a year before the records around it expire.
    status, _ = correction(env, eventTime="2025-03-01T08:00:00Z")
    assert status == 201
    assert int(ledger(env)[0]["ttl"]) == pl.ttl_for(NOW)


# --- 60: an export too large for one response -------------------------------------------------------

def test_export_too_large_is_refused_with_a_clear_error(env, monkeypatch):
    for _ in range(3):
        check(env, "check-in", person=P_HELPER)
        check(env, "check-out", person=P_HELPER)
    monkeypatch.setattr(env.api, "EXPORT_MAX_BYTES", 2000)
    status, body = call(env, "GET", BASE + "/export", query={"from": DAY, "to": DAY})
    assert (status, body["code"], body["field"]) == (400, "export_too_large", "from")
    assert "message" not in body  # the documented ErrorResponse: clients map the code
    hook = platform(env, TENANT_A, {"locationId": LOC_A, "from": DAY, "to": DAY})
    assert (hook["ok"], hook["status"], hook["code"]) == (False, 400, "export_too_large")
    monkeypatch.setattr(env.api, "EXPORT_MAX_BYTES", 5_000_000)
    assert call(env, "GET", BASE + "/export", query={"from": DAY, "to": DAY})[0] == 200


def test_export_size_limit_leaves_room_under_the_lambda_limit(env):
    assert env.api.EXPORT_MAX_BYTES <= 5_500_000

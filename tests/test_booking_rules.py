"""Booking rules (special days, online table rules, combinations), the
Turnstile client and the request guard - unit level. End-to-end behaviour
(availability, bookings, moves) is in test_reservations.py."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from botocore.exceptions import ClientError

from shared import booking_rules as rules
from shared import request_guard, turnstile

TODAY = date(2026, 10, 12)


# --- special days ----------------------------------------------------------------------

def test_special_days_are_sorted_normalized_and_past_days_dropped():
    days = rules.validate_special_days([
        {"date": "2026-12-31", "kind": "hours", "note": "  Nyårsafton  ",
         "intervals": [{"opensAt": "19:00", "closesAt": "23:59"}, {"opensAt": "11:00", "closesAt": "14:00"}]},
        {"date": "2026-12-24", "kind": "closed", "note": "Julafton"},
        {"date": "2026-10-11", "kind": "closed"},
        {"date": "2026-10-12", "kind": "no_online", "note": ""},
    ], today=TODAY)
    assert days == [
        {"date": "2026-10-12", "kind": "no_online"},
        {"date": "2026-12-24", "kind": "closed", "note": "Julafton"},
        {"date": "2026-12-31", "kind": "hours", "note": "Nyårsafton",
         "intervals": [{"opensAt": "11:00", "closesAt": "14:00"}, {"opensAt": "19:00", "closesAt": "23:59"}]},
    ]


def test_no_special_days_is_none():
    assert rules.validate_special_days(None) is None
    assert rules.validate_special_days([]) is None
    assert rules.validate_special_days([{"date": "2026-10-01", "kind": "closed"}], today=TODAY) is None


@pytest.mark.parametrize("value, message", [
    ("2026-12-24", "must be a list"),
    ([{"date": "2026-12-24"}], "kind must be closed, hours or no_online"),
    ([{"date": "24/12", "kind": "closed"}], "YYYY-MM-DD"),
    ([{"date": "2026-02-30", "kind": "closed"}], "real calendar dates"),
    ([{"date": "2026-12-24", "kind": "closed"}, {"date": "2026-12-24", "kind": "hours"}], "more than once"),
    ([{"date": "2026-12-24", "kind": "hours"}], "at least one opening interval"),
    ([{"date": "2026-12-24", "kind": "hours", "intervals": [{"opensAt": "20:00", "closesAt": "18:00"}]}],
     "must precede"),
    ([{"date": "2026-12-24", "kind": "hours", "intervals": [
        {"opensAt": "10:00", "closesAt": "15:00"}, {"opensAt": "14:00", "closesAt": "18:00"}]}], "overlap"),
    ([{"date": "2026-12-24", "kind": "hours", "intervals": [{"opensAt": "24:00", "closesAt": "25:00"}]}],
     "HH:MM"),
    ([{"date": "2026-12-24", "kind": "closed", "intervals": [{"opensAt": "10:00", "closesAt": "12:00"}]}],
     "only kind hours"),
    ([{"date": "2026-12-24", "kind": "closed", "note": "x" * 81}], "at most 80"),
    ([{"date": "2026-12-24", "kind": "closed", "note": 5}], "must be text"),
    ([{"date": "2026-12-24", "kind": "closed", "reason": "x"}], "unsupported fields: reason"),
    ([{"date": "2029-01-01", "kind": "closed"}], "within two years"),
])
def test_invalid_special_days_are_refused(value, message):
    with pytest.raises(ValueError, match=message):
        rules.validate_special_days(value, today=TODAY)


def test_the_day_intervals_follow_a_special_day():
    week = {"friday": [{"opensAt": "11:00", "closesAt": "22:00"}]}
    special = {"date": "2026-12-25", "kind": "hours", "intervals": [{"opensAt": "17:00", "closesAt": "21:00"}]}
    assert rules.day_intervals(week, "friday", None) == week["friday"]
    assert rules.day_intervals(week, "friday", {"kind": "closed"}) == []
    assert rules.day_intervals(week, "friday", special) == special["intervals"]
    assert rules.day_intervals(week, "friday", {"kind": "no_online"}) == week["friday"]
    assert rules.day_status(None) is None
    assert rules.day_status({"date": "2026-12-24", "kind": "closed", "note": "Jul"}) == {
        "kind": "closed", "note": "Jul"}


def test_the_guest_site_gets_the_next_60_days():
    days = rules.validate_special_days([
        {"date": "2026-10-12", "kind": "closed"},
        {"date": "2026-12-11", "kind": "no_online"},
        {"date": "2026-12-12", "kind": "closed"},
    ])
    assert rules.public_special_days(days, TODAY) == [
        {"date": "2026-10-12", "kind": "closed"},
        {"date": "2026-12-11", "kind": "no_online"},
    ]
    assert rules.public_special_days(None, TODAY) == []


# --- online table rules ---------------------------------------------------------------------

def test_table_rules_accept_numbers_from_json_and_dynamodb():
    assert rules.validate_table_rules({
        "t1": {"minParty": 1, "maxParty": Decimal(4), "online": True},
        "t2": {"minParty": Decimal("2"), "maxParty": 2, "online": False},
    }) == {"t1": {"minParty": 1, "maxParty": 4, "online": True},
           "t2": {"minParty": 2, "maxParty": 2, "online": False}}
    assert rules.validate_table_rules({}) is None
    assert rules.validate_table_rules(None) is None


@pytest.mark.parametrize("value, message", [
    ([], "object keyed by table id"),
    ({"t1": {"minParty": 3, "maxParty": 2, "online": True}}, "must not be larger"),
    ({"t1": {"minParty": 0, "maxParty": 2, "online": True}}, "between 1 and 50"),
    ({"t1": {"minParty": True, "maxParty": 2, "online": True}}, "between 1 and 50"),
    ({"t1": {"minParty": Decimal("1.5"), "maxParty": 2, "online": True}}, "between 1 and 50"),
    ({"t1": {"minParty": 1, "maxParty": 2, "online": "yes"}}, "true or false"),
    ({"t1": {"minParty": 1, "maxParty": 2}}, "need minParty, maxParty and online"),
    ({"t#1": {"minParty": 1, "maxParty": 2, "online": True}}, "table ids"),
])
def test_invalid_table_rules_are_refused(value, message):
    with pytest.raises(ValueError, match=message):
        rules.validate_table_rules(value)


def test_combinations_are_sorted_and_unique():
    assert rules.validate_table_combinations([
        {"tableIds": ["t4", "t3"], "minParty": 5, "maxParty": 8},
        {"tableIds": ["t1", "t2", "t5"], "minParty": 7, "maxParty": 12},
    ]) == [
        {"tableIds": ["t1", "t2", "t5"], "minParty": 7, "maxParty": 12},
        {"tableIds": ["t3", "t4"], "minParty": 5, "maxParty": 8},
    ]


@pytest.mark.parametrize("value, message", [
    ({}, "must be a list"),
    ([{"tableIds": ["t1"], "minParty": 1, "maxParty": 2}], "2 to 6 tables"),
    ([{"tableIds": [f"t{i}" for i in range(7)], "minParty": 1, "maxParty": 2}], "2 to 6 tables"),
    ([{"tableIds": ["t1", "t1"], "minParty": 1, "maxParty": 2}], "more than once"),
    ([{"tableIds": ["t1", "t2"], "minParty": 1, "maxParty": 4},
      {"tableIds": ["t2", "t1"], "minParty": 3, "maxParty": 4}], "same tables more than once"),
    ([{"tableIds": ["t1", "t2"], "minParty": 1}], "need tableIds, minParty and maxParty"),
])
def test_invalid_combinations_are_refused(value, message):
    with pytest.raises(ValueError, match=message):
        rules.validate_table_combinations(value)


@pytest.mark.parametrize("seats, smallest", [(1, 1), (2, 1), (3, 2), (4, 2), (6, 3), (8, 4), (10, 5)])
def test_default_online_range_is_half_the_seats_up_to_all(seats, smallest):
    assert rules.table_rule(None, "t", seats) == {"minParty": smallest, "maxParty": seats, "online": True}


def test_a_rule_never_offers_more_guests_than_seats():
    rule = rules.table_rule({"t": {"minParty": 1, "maxParty": 8, "online": True}}, "t", 4)
    assert rule == {"minParty": 1, "maxParty": 4, "online": True}


def test_online_options_best_fit_first():
    free = {"a": 2, "b": 4, "c": 6, "d": 4}
    combos = [{"tableIds": ["b", "d"], "minParty": 5, "maxParty": 8},
              {"tableIds": ["a", "x"], "minParty": 3, "maxParty": 4}]
    assert rules.online_options(free, 4, None, combos) == [
        {"tableIds": ["b"], "seats": 4}, {"tableIds": ["d"], "seats": 4}, {"tableIds": ["c"], "seats": 6}]
    # A combination whose tables aren't all free isn't offered (x is taken).
    assert rules.online_options(free, 5, None, combos) == [
        {"tableIds": ["c"], "seats": 6}, {"tableIds": ["b", "d"], "seats": 8}]
    # Combination ranges are capped by their seats too.
    assert rules.online_options({"b": 4, "d": 4}, 9, None,
                                [{"tableIds": ["b", "d"], "minParty": 5, "maxParty": 12}]) == []
    # A table switched off online is only bookable through a combination.
    off = {"b": {"minParty": 1, "maxParty": 4, "online": False}}
    assert rules.online_options({"b": 4}, 2, off, []) == []


# --- turnstile client ---------------------------------------------------------------------------

@pytest.fixture
def secret(monkeypatch):
    turnstile.reset_cache()
    turnstile._cache["secret"] = "secret-key"
    yield
    turnstile.reset_cache()


def test_turnstile_retries_an_internal_error_once(secret, monkeypatch):
    answers = [{"success": False, "error-codes": ["internal-error"]}, {"success": True, "action": "booking"}]
    keys = []

    def post(payload):
        keys.append(payload["idempotency_key"])
        return answers.pop(0)

    monkeypatch.setattr(turnstile, "_post", post)
    turnstile.verify("token", "203.0.113.5")
    assert len(keys) == 2 and keys[0] == keys[1]


def test_turnstile_refuses_huge_or_blank_tokens_without_calling(secret, monkeypatch):
    monkeypatch.setattr(turnstile, "_post", lambda payload: pytest.fail("called"))
    with pytest.raises(ValueError, match="required"):
        turnstile.verify("   ")
    with pytest.raises(ValueError, match="failed"):
        turnstile.verify("x" * 2049)


def test_turnstile_without_a_secret_is_unavailable(monkeypatch):
    turnstile.reset_cache()
    monkeypatch.delenv("TURNSTILE_SECRET_ARN", raising=False)
    with pytest.raises(turnstile.Unavailable):
        turnstile.verify("token")


def test_turnstile_is_required_only_when_switched_on(monkeypatch):
    monkeypatch.delenv("TURNSTILE_REQUIRED", raising=False)
    assert turnstile.required() is False
    monkeypatch.setenv("TURNSTILE_REQUIRED", "true")
    assert turnstile.required() is True


# --- request guard ---------------------------------------------------------------------------

class _BrokenTable:
    def update_item(self, **kwargs):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem")


def test_the_guard_fails_open_when_its_table_is_unavailable(monkeypatch):
    monkeypatch.setenv("REQUEST_GUARD_TABLE_NAME", "guard")
    monkeypatch.setattr(request_guard, "table", lambda name: _BrokenTable())
    request_guard.check([("ip", "203.0.113.5", 1, 600)], datetime(2026, 10, 12, tzinfo=timezone.utc))


def test_the_guard_is_off_without_a_table(monkeypatch):
    monkeypatch.delenv("REQUEST_GUARD_TABLE_NAME", raising=False)
    monkeypatch.setattr(request_guard, "table", lambda name: pytest.fail("used"))
    request_guard.check([("ip", "203.0.113.5", 1, 600)], datetime(2026, 10, 12, tzinfo=timezone.utc))


class _CountingTable:
    """update_item with the guard's condition; ``full`` keys are at their limit."""

    def __init__(self, full=()):
        self.full = set(full)
        self.rows = {}

    def update_item(self, Key, UpdateExpression, ConditionExpression, ExpressionAttributeValues, **kwargs):
        key = Key["PK"]
        if ":minus" in ExpressionAttributeValues:
            if self.rows.get(key, 0) <= 0:
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
            self.rows[key] -= 1
            return
        if key.split("#")[0] in self.full:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        self.rows[key] = self.rows.get(key, 0) + 1


NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
CONTACT = [("phone", "+46701234567", 5, 86400), ("email", "anna@example.se", 5, 86400)]


def test_the_guard_gives_back_what_it_counted(monkeypatch):
    monkeypatch.setenv("REQUEST_GUARD_TABLE_NAME", "guard")
    counters = _CountingTable()
    monkeypatch.setattr(request_guard, "table", lambda name: counters)
    counted = request_guard.check(CONTACT, NOW)
    assert len(counted) == 2 and sorted(counters.rows.values()) == [1, 1]
    request_guard.refund(counted)
    assert sorted(counters.rows.values()) == [0, 0]
    # A counter that has expired (or is at zero) is left alone.
    request_guard.refund(counted)
    assert sorted(counters.rows.values()) == [0, 0]


def test_a_limit_gives_back_the_attempts_counted_before_it(monkeypatch):
    monkeypatch.setenv("REQUEST_GUARD_TABLE_NAME", "guard")
    counters = _CountingTable(full={"email"})
    monkeypatch.setattr(request_guard, "table", lambda name: counters)
    with pytest.raises(request_guard.Limited) as limited:
        request_guard.check(CONTACT, NOW)
    assert limited.value.kind == "email"
    assert 0 < limited.value.retry_after <= 86400
    assert list(counters.rows.values()) == [0]  # the phone's attempt was given back


def test_the_guard_counts_nothing_without_a_table(monkeypatch):
    monkeypatch.delenv("REQUEST_GUARD_TABLE_NAME", raising=False)
    assert request_guard.check(CONTACT, NOW) == []
    request_guard.refund(["phone#x#86400#1"])

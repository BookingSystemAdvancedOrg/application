"""Audit #23 and #30: a failed stream record must not make later records in
the batch go out twice, and a replayed record (DLQ replay, stream retry)
must not send a notice that already went out."""

import boto3
import pytest

from test_notification import booking, env, record, run  # noqa: F401 - env is a fixture
from tenant_support import REGION

GUARD_TABLE = "test-request-guard"


@pytest.fixture
def guarded(env, monkeypatch):  # noqa: F811 - the imported fixture
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=GUARD_TABLE, BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}],
    )
    monkeypatch.setenv("REQUEST_GUARD_TABLE_NAME", GUARD_TABLE)
    return env


def _guard_rows():
    return boto3.resource("dynamodb", region_name=REGION).Table(GUARD_TABLE).scan()["Items"]


def test_retryable_failure_stops_the_batch_so_later_records_are_not_sent_twice(env):  # noqa: F811
    module, ses, sns = env
    ses.fail = ["Throttling"]
    first = record(booking(), event_name="INSERT", seq="11")
    second = record(booking(reservationId="b" * 32, customerEmail="bo@example.se"), event_name="INSERT", seq="12")

    result = run(module, first, second)

    assert result == {"batchItemFailures": [{"itemIdentifier": "11"}]}
    # Lambda redelivers from sequence 11, so record 12 must not have gone out yet.
    assert ses.calls == [] and sns.calls == []


def test_unexpected_failure_stops_the_batch_too(env, monkeypatch):  # noqa: F811
    module, ses, sns = env

    def boom(*args, **kwargs):
        raise RuntimeError("template bug")

    monkeypatch.setattr(module.messages, "guest_email", boom)
    first = record(booking(), event_name="INSERT", seq="21")
    second = record(booking(reservationId="b" * 32), event_name="INSERT", seq="22")

    result = run(module, first, second)

    assert result == {"batchItemFailures": [{"itemIdentifier": "21"}]}
    assert sns.calls == []


def test_replayed_record_is_not_sent_again(guarded):
    module, ses, sns = guarded
    item = booking()

    run(module, record(item, event_name="INSERT", seq="31"))
    sent_mail, sent_sms = len(ses.calls), len(sns.calls)
    assert sent_mail >= 1 and sent_sms == 1

    # The same stream record again (DLQ replay of the whole failed range).
    assert run(module, record(item, event_name="INSERT", seq="31")) == {"batchItemFailures": []}
    assert len(ses.calls) == sent_mail and len(sns.calls) == sent_sms

    rows = _guard_rows()
    assert len(rows) == 1 and rows[0]["PK"].startswith("sent#") and int(rows[0]["ttl"]) > 0
    # Only hashes: no reservation id, tenant id or address in the key.
    assert item["reservationId"] not in rows[0]["PK"] and item["tenantId"] not in rows[0]["PK"]


def test_a_new_notice_on_the_same_booking_is_still_sent(guarded):
    module, ses, sns = guarded
    run(module, record(booking(), event_name="INSERT", seq="41"))
    before = len(sns.calls)

    changed = booking(notice={"id": "m" * 32, "type": "changed", "at": module._now().isoformat()})
    run(module, record(changed, old=booking(), seq="42"))

    assert len(sns.calls) == before + 1


def test_marker_is_released_when_nothing_went_out_so_the_retry_sends(guarded):
    module, ses, sns = guarded
    ses.fail = ["Throttling"]
    item = booking()

    assert run(module, record(item, event_name="INSERT", seq="51")) == {"batchItemFailures": [{"itemIdentifier": "51"}]}
    assert ses.calls == [] and sns.calls == [] and _guard_rows() == []

    assert run(module, record(item, event_name="INSERT", seq="51")) == {"batchItemFailures": []}
    assert len(sns.calls) == 1


def test_guard_table_unreachable_still_sends(env, monkeypatch):  # noqa: F811
    module, ses, sns = env
    monkeypatch.setenv("REQUEST_GUARD_TABLE_NAME", "no-such-table")

    assert run(module, record(booking(), event_name="INSERT", seq="61")) == {"batchItemFailures": []}
    assert len(sns.calls) == 1

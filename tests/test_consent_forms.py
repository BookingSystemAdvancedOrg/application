"""Guest forms and the consent log: bookings work without consent, a ticked
box becomes grant records (only with marketing on), and the old
marketingOptIn bool still works."""

import boto3
import pytest

from shared import consent, tenant as shared_tenant
from tenant_support import ALL_FEATURES, TENANT_A, TENANT_TABLE, tenant_row
from test_consent import consent_table
from test_reservations import REGION, RESERVATION_TABLE, body_of, env, online_attempt  # noqa: F401

CURRENT = {"channels": ["email", "sms"], "textVersion": consent.CURRENT}


@pytest.fixture
def log(env, monkeypatch):  # noqa: F811 - env is the booking fixture
    monkeypatch.setenv("CONSENT_TABLE_NAME", "test-consent")
    table = consent_table(boto3.resource("dynamodb", region_name=REGION))
    env["res"].Table(TENANT_TABLE).put_item(Item=tenant_row(TENANT_A, name="Roma",
                                                            features={**ALL_FEATURES, "marketing": True}))
    shared_tenant.reset_caches()
    return table


def records(table):
    return sorted(table.scan()["Items"], key=lambda i: i["channel"])


def booking(env):  # noqa: F811
    items = env["res"].Table(RESERVATION_TABLE).scan()["Items"]
    return next(i for i in items if i["SK"].startswith("RESERVATION#"))


def test_booking_without_consent_works_and_logs_nothing(env, log):  # noqa: F811
    assert online_attempt(env)["statusCode"] == 201
    assert log.scan()["Items"] == []
    assert booking(env)["marketingOptIn"] is False


def test_ticked_box_writes_one_email_grant(env, log):  # noqa: F811
    response = online_attempt(env, marketingConsent=CURRENT)
    assert response["statusCode"] == 201
    (email,) = records(log)  # SMS is not offered (M3), even if an old page sends it
    assert email["identifier"] == "anna@example.se"
    assert email["ref"] == body_of(response)["reservationId"]
    assert email["source"] == "booking" and email["locationId"]
    assert email["text"] == consent.render(consent.CURRENT, "Roma")
    assert not consent.may_send(log, TENANT_A, "sms", "+46701234567")
    assert booking(env)["marketingOptIn"] is True  # the subscriber list follows the e-mail channel


def test_sms_only_consent_records_nothing(env, log):  # noqa: F811
    online_attempt(env, marketingConsent={"channels": ["sms"], "textVersion": consent.CURRENT})
    assert records(log) == []
    assert booking(env)["marketingOptIn"] is False


def test_old_bool_still_works_as_email_consent(env, log):  # noqa: F811
    assert online_attempt(env, marketingOptIn=True)["statusCode"] == 201
    (record,) = records(log)
    assert record["textVersion"] == "offers_booking_v0" and record["channel"] == "email"


def test_unknown_wording_version_is_refused(env, log):  # noqa: F811
    response = online_attempt(env, marketingConsent={"channels": ["email"], "textVersion": "old"})
    assert response["statusCode"] == 400
    assert log.scan()["Items"] == []


def test_no_log_without_the_marketing_feature(env, log):  # noqa: F811
    env["res"].Table(TENANT_TABLE).put_item(Item=tenant_row(TENANT_A, name="Roma"))
    shared_tenant.reset_caches()
    assert online_attempt(env, marketingConsent=CURRENT)["statusCode"] == 201
    assert log.scan()["Items"] == []


def test_failed_booking_logs_no_consent(env, log):  # noqa: F811
    online_attempt(env, marketingConsent=CURRENT)
    before = len(log.scan()["Items"])
    taken = online_attempt(env, marketingConsent=CURRENT, email="other@example.se", phone="0709999999")
    assert taken["statusCode"] == 409
    assert len(log.scan()["Items"]) == before

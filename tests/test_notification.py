"""notification: Reservation stream records with a notice -> guest email /
SMS and restaurant email. SES/SNS are fakes; DynamoDB and Secrets Manager
are moto, so tenant/location/domain lookups and the manage link are real."""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from moto import mock_aws

from shared import dynamo as shared_dynamo
from shared import manage_link
from shared import tenant as shared_tenant
from tenant_support import LOC_A, REGION, TENANT_A, TENANT_TABLE, location_row, seed_tenancy, tenant_row

APP = Path(__file__).parents[1] / "functions" / "notification" / "app.py"
RES_TABLE = "test-reservation"
STREAM = f"arn:aws:dynamodb:{REGION}:123456789012:table/{RES_TABLE}/stream/2026-01-01T00:00:00.000"
NOW = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)
_ser = TypeSerializer()


class FakeClient:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = list(fail or [])

    def _record(self, kind, kwargs):
        if self.fail:
            code = self.fail.pop(0)
            if code:
                raise ClientError({"Error": {"Code": code, "Message": code}}, kind)
        self.calls.append(kwargs)
        return {"MessageId": "m"}

    def send_email(self, **kwargs):
        return self._record("SendEmail", kwargs)

    def publish(self, **kwargs):
        return self._record("Publish", kwargs)


@pytest.fixture
def env(monkeypatch):
    for key, value in {
        "ENVIRONMENT": "dev", "AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t",
        "AWS_SESSION_TOKEN": "t", "AWS_DEFAULT_REGION": REGION, "RESERVATION_TABLE_NAME": RES_TABLE,
        "NO_REPLY_EMAIL_ADDRESS": "no-reply@mail.example.se",
        "ADMIN_DASHBOARD_URL": "https://admin.example.se",
        "CUSTOMER_SITE_URL": "https://dev123.cloudfront.net",
    }.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        manage_link.reset_cache()
        secret = boto3.client("secretsmanager", region_name=REGION).create_secret(
            Name="link-key", SecretString="s" * 64)
        monkeypatch.setenv("RESERVATION_LINK_KEY_SECRET_ARN", secret["ARN"])
        seed_tenancy(
            monkeypatch,
            tenant_a=tenant_row(TENANT_A, name="Roma", slug="roma", senderName="Roma",
                                notifications={"sms": True}, replyToEmail="hej@roma.se"),
            locations_a=[location_row(TENANT_A, LOC_A, name="Södermalm", email="soder@roma.se",
                                      phoneNumber="+46812345678", address="Götgatan 1, Stockholm")],
        )
        spec = importlib.util.spec_from_file_location("notification_app", APP)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ses, sns = FakeClient(), FakeClient()
        module._clients.update(ses=ses, sns=sns)
        monkeypatch.setattr(module, "_now", lambda: NOW)
        yield module, ses, sns
        shared_dynamo._resource = None
        shared_dynamo._client = None
        shared_tenant.reset_caches()
        manage_link.reset_cache()


def booking(**extra):
    item = {
        "PK": f"LOCATION#{LOC_A}", "SK": "RESERVATION#2026-09-20#" + "a" * 32,
        "reservationId": "a" * 32, "tenantId": TENANT_A, "locationId": LOC_A,
        "date": "2026-09-20", "startTime": "18:00", "endTime": "20:00", "partySize": 3,
        "tableIds": ["t1"], "customerName": "Anna Svensson", "customerEmail": "anna@example.se",
        "customerPhone": "+46701234567", "language": "sv", "source": "online",
        "status": "reserved", "linkVersion": 1,
        "notice": {"id": "n" * 32, "type": "confirmed", "at": (NOW - timedelta(seconds=5)).isoformat()},
    }
    item.update(extra)
    return {k: v for k, v in item.items() if v is not None}


def record(new, old=None, event_name="MODIFY", seq="1", arn=STREAM):
    data = {"NewImage": {k: _ser.serialize(v) for k, v in new.items()}, "SequenceNumber": seq}
    if old is not None:
        data["OldImage"] = {k: _ser.serialize(v) for k, v in old.items()}
    return {"eventName": event_name, "eventSourceARN": arn, "dynamodb": data}


def run(module, *records):
    return module.handler({"Records": list(records)}, None)


def test_online_booking_confirms_to_guest_and_tells_the_restaurant(env):
    module, ses, sns = env
    item = booking()

    result = run(module, record(item, event_name="INSERT"))

    assert result == {"batchItemFailures": []}
    guest, staff = ses.calls
    assert guest["Destination"] == {"ToAddresses": ["anna@example.se"]}
    assert guest["Message"]["Subject"]["Data"] == "Bokningsbekräftelse - Roma Södermalm"
    assert "no-reply@mail.example.se" in guest["Source"] and guest["ReplyToAddresses"] == ["soder@roma.se"]
    text = guest["Message"]["Body"]["Text"]["Data"]
    link = f"https://dev123.cloudfront.net/bokning/{LOC_A}/{'a' * 32}?restaurang=roma#{manage_link.token_for(item)}"
    assert "Söndag 20 september 2026" in text and "18:00-20:00" in text and link in text
    assert "3 personer" in text and "Götgatan 1" in text
    assert staff["Destination"] == {"ToAddresses": ["soder@roma.se"]}
    assert staff["Message"]["Subject"]["Data"].startswith("Ny bokning 2026-09-20 18:00")
    assert staff["ReplyToAddresses"] == ["anna@example.se"]
    assert "https://admin.example.se/bokningar?date=2026-09-20" in staff["Message"]["Body"]["Text"]["Data"]
    (sms,) = sns.calls
    assert sms["PhoneNumber"] == "+46701234567" and "söndag 20 september kl 18:00" in sms["Message"]
    assert sms["MessageAttributes"]["AWS.SNS.SMS.SenderID"]["StringValue"] == "Roma"


def test_the_tenants_own_domain_is_used_for_the_link(env):
    module, ses, _ = env
    tenants = boto3.resource("dynamodb", region_name=REGION).Table(TENANT_TABLE)
    tenants.put_item(Item={"PK": f"TENANT#{TENANT_A}", "SK": "DOMAIN#www.roma.se",
                           "domain": "www.roma.se", "status": "active"})
    tenants.put_item(Item={"PK": f"TENANT#{TENANT_A}", "SK": "DOMAIN#new.roma.se",
                           "domain": "new.roma.se", "status": "pending_dns"})
    tenants.put_item(Item={"PK": f"TENANT#{TENANT_A}", "SK": "DOMAIN#roma.example.app",
                           "domain": "roma.example.app", "status": "active"})
    tenants.update_item(Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"},
                        UpdateExpression="SET primaryDomain = :d", ExpressionAttributeValues={":d": "www.roma.se"})
    shared_tenant.reset_caches()
    run(module, record(booking(), event_name="INSERT"))
    text = ses.calls[0]["Message"]["Body"]["Text"]["Data"]
    assert f"https://www.roma.se/bokning/{LOC_A}/" in text and "restaurang=" not in text


def test_prod_without_a_domain_sends_no_link(env, monkeypatch):
    module, ses, _ = env
    monkeypatch.setenv("ENVIRONMENT", "prod")
    run(module, record(booking(), event_name="INSERT"))
    assert "/bokning/" not in ses.calls[0]["Message"]["Body"]["Text"]["Data"]


def test_same_notice_id_is_never_sent_twice(env):
    module, ses, sns = env
    item = booking()
    run(module, record({**item, "status": "arrived"}, old=item))
    assert ses.calls == [] and sns.calls == []


def test_stale_notice_is_dropped(env):
    module, ses, _ = env
    old = booking(notice={"id": "o" * 32, "type": "confirmed", "at": "2026-09-01T00:00:00Z"})
    new = booking(notice={"id": "x" * 32, "type": "reminder", "at": "2026-09-16T00:00:00Z"})
    assert run(module, record(new, old=old)) == {"batchItemFailures": []}
    assert ses.calls == []


@pytest.mark.parametrize("kind, subject, has_link", [
    ("changed", "Din bokning är ändrad - Roma Södermalm", True),
    ("cancelled_by_guest", "Avbokning bekräftad - Roma Södermalm", False),
    ("cancelled_by_restaurant", "Din bokning är avbokad - Roma Södermalm", False),
    ("reminder", "Påminnelse: bord söndag 20 september kl 18:00 - Roma Södermalm", True),
])
def test_each_notice_type(env, kind, subject, has_link):
    module, ses, _ = env
    new = booking(notice={"id": "y" * 32, "type": kind, "at": NOW.isoformat()})
    run(module, record(new, old=booking()))
    guest = ses.calls[0]
    assert guest["Message"]["Subject"]["Data"] == subject
    assert ("/bokning/" in guest["Message"]["Body"]["Text"]["Data"]) is has_link
    staff = [c for c in ses.calls if c["Destination"]["ToAddresses"] == ["soder@roma.se"]]
    assert bool(staff) is (kind == "cancelled_by_guest")


def test_english_guest_gets_english(env):
    module, ses, sns = env
    run(module, record(booking(language="en"), event_name="INSERT"))
    assert ses.calls[0]["Message"]["Subject"]["Data"] == "Booking confirmation - Roma Södermalm"
    assert "Sunday 20 September at 18:00" in sns.calls[0]["Message"]


def test_sms_only_when_the_tenant_turned_it_on(env):
    module, _, sns = env
    boto3.resource("dynamodb", region_name=REGION).Table(TENANT_TABLE).update_item(
        Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"},
        UpdateExpression="REMOVE notifications")
    shared_tenant.reset_caches()
    run(module, record(booking(), event_name="INSERT"))
    assert sns.calls == []


def test_staff_booking_does_not_email_the_restaurant(env):
    module, ses, _ = env
    run(module, record(booking(source="phone"), event_name="INSERT"))
    assert [c["Destination"]["ToAddresses"] for c in ses.calls] == [["anna@example.se"]]


def test_guest_html_escapes_what_the_guest_typed(env):
    module, ses, _ = env
    run(module, record(booking(customerName="<script>x</script> Bo"), event_name="INSERT"))
    html_body = ses.calls[0]["Message"]["Body"]["Html"]["Data"]
    assert "<script>" not in html_body and "&lt;script&gt;" in html_body


def test_transient_email_failure_retries_only_that_record_and_sends_nothing(env):
    module, ses, sns = env
    ses.fail = ["Throttling"]
    first = record(booking(), event_name="INSERT", seq="11")
    second = record(booking(reservationId="b" * 32), event_name="INSERT", seq="12")

    result = run(module, first, second)

    assert result == {"batchItemFailures": [{"itemIdentifier": "11"}]}
    assert len(sns.calls) == 1  # only the second record's SMS


def test_sms_failure_after_the_email_is_not_retried(env):
    module, ses, sns = env
    sns.fail = ["Throttling"]
    assert run(module, record(booking(), event_name="INSERT")) == {"batchItemFailures": []}
    assert len(ses.calls) == 2


def test_permanent_email_rejection_is_logged_not_retried(env):
    module, ses, sns = env
    ses.fail = ["MessageRejected"]
    assert run(module, record(booking(), event_name="INSERT")) == {"batchItemFailures": []}
    assert len(sns.calls) == 1


def test_inactive_tenant_gets_nothing(env):
    module, ses, sns = env
    boto3.resource("dynamodb", region_name=REGION).Table(TENANT_TABLE).update_item(
        Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"},
        UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "suspended"})
    shared_tenant.reset_caches()
    run(module, record(booking(), event_name="INSERT"))
    assert ses.calls == [] and sns.calls == []


def test_other_streams_are_acknowledged_untouched(env):
    module, ses, _ = env
    other = STREAM.replace(RES_TABLE, "test-order")
    assert run(module, record(booking(), event_name="INSERT", arn=other)) == {"batchItemFailures": []}
    assert ses.calls == []


def test_phone_only_booking_without_sms_sends_nothing_and_succeeds(env):
    module, ses, sns = env
    boto3.resource("dynamodb", region_name=REGION).Table(TENANT_TABLE).update_item(
        Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"},
        UpdateExpression="SET notifications = :n", ExpressionAttributeValues={":n": {"sms": False}})
    shared_tenant.reset_caches()
    item = booking(customerEmail=None, source="phone")
    assert run(module, record(item, event_name="INSERT")) == {"batchItemFailures": []}
    assert ses.calls == [] and sns.calls == []


def test_fee_receipt_goes_to_the_guest_and_a_failed_fee_to_the_restaurant(env):
    module, ses, sns = env
    paid = booking(status="no_show_charged", payment={"kind": "no_show", "amount": 60000, "attempt": 1},
                   notice={"id": "f" * 32, "type": "fee_charged", "at": NOW.isoformat()})
    run(module, record(paid, old=booking()))
    receipt = ses.calls[0]
    assert receipt["Destination"] == {"ToAddresses": ["anna@example.se"]}
    assert receipt["Message"]["Subject"]["Data"] == "Kvitto: Avgift för utebliven gäst - Roma Södermalm"
    assert "600 kr" in receipt["Message"]["Body"]["Text"]["Data"]
    assert "600 kr" in sns.calls[0]["Message"]
    ses.calls.clear()
    sns.calls.clear()

    failed = booking(status="no_show_charge_failed", source="phone",
                     payment={"kind": "no_show", "amount": 60000, "attempt": 1, "error": "insufficient_funds"},
                     notice={"id": "e" * 32, "type": "fee_failed", "at": NOW.isoformat()})
    run(module, record(failed, old=paid))
    (staff,) = ses.calls
    assert staff["Destination"] == {"ToAddresses": ["soder@roma.se"]} and sns.calls == []
    assert "insufficient_funds" in staff["Message"]["Body"]["Text"]["Data"]

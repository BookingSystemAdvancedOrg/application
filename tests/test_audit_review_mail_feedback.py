"""Audit #33: a review-request email writes the MSG# lookup row, so an SES
complaint on it withdraws the guest's consent (and a permanent bounce marks
a subscriber row) like any other marketing email."""

import boto3

from shared import consent
from shared import marketing as mkt
from tenant_support import REGION, TENANT_A
from test_google_business import EMAIL, _create, enable, env, fire, grant, load, visit  # noqa: F401

MKT_TABLE = "test-marketing"


def _rows():
    return [i for i in boto3.resource("dynamodb", region_name=REGION).Table(MKT_TABLE).scan()["Items"]
            if i["PK"].startswith("MSG#")]


def test_review_email_writes_the_ses_lookup_and_a_complaint_withdraws(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MARKETING_TABLE_NAME", MKT_TABLE)
    _create(boto3.client("dynamodb", region_name=REGION), MKT_TABLE)
    enable(env)
    grant()
    env.jobs.handler({"source": "booqy.guest-crm", "detail-type": "GuestVisit.Recorded", "detail": visit()})
    assert fire(env) == "sent"
    (row,) = _rows()
    assert row["SK"] == "MSG" and row["tenantId"] == TENANT_A and int(row["ttl"]) > 0
    assert row["subscriberId"] == mkt.subscriber_id(TENANT_A, EMAIL)
    assert row["consentSubject"] == consent.subject(TENANT_A, "email", EMAIL)
    assert "campaignId" not in row and EMAIL not in str(row)  # no address in the lookup row
    message_id = row["PK"].removeprefix("MSG#")
    log = boto3.resource("dynamodb", region_name=REGION).Table("test-consent")
    assert consent.may_send(log, TENANT_A, "email", EMAIL)
    load("marketing-sync").handler({"source": "aws.ses", "detail": {
        "eventType": "Complaint", "mail": {"messageId": message_id}}}, None)
    assert not consent.may_send(log, TENANT_A, "email", EMAIL)
    latest = consent.latest(log, TENANT_A, consent.subject(TENANT_A, "email", EMAIL))
    assert latest["source"] == "complaint" and "identifier" not in latest


def test_review_email_still_sent_without_the_marketing_table(env, monkeypatch):  # noqa: F811
    monkeypatch.delenv("MARKETING_TABLE_NAME", raising=False)
    enable(env)
    grant()
    env.jobs.handler({"source": "booqy.guest-crm", "detail-type": "GuestVisit.Recorded", "detail": visit()})
    assert fire(env) == "sent"


def test_a_failed_lookup_write_does_not_resend(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MARKETING_TABLE_NAME", "missing-table")
    enable(env)
    grant()
    env.jobs.handler({"source": "booqy.guest-crm", "detail-type": "GuestVisit.Recorded", "detail": visit()})
    assert fire(env) == "sent"
    assert fire(env) == "done"

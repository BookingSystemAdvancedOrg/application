"""Marketing and the consent log: every send checks the latest consent; the
withdrawal page, the unsubscribe link and spam complaints write withdrawal
records; transactional mail is never touched."""

from types import SimpleNamespace

import boto3
import pytest

from shared import consent
from tenant_support import REGION, TENANT_A
from test_consent import consent_table
from test_marketing import admin, draft, env, load, make_subscribers, outbox, subscriber  # noqa: F401

CTX = SimpleNamespace(get_remaining_time_in_millis=lambda: 120000)


@pytest.fixture
def log(env, monkeypatch):  # noqa: F811 - env is the marketing fixture
    monkeypatch.setenv("CONSENT_TABLE_NAME", "test-consent")
    return consent_table(boto3.resource("dynamodb", region_name=REGION))


def granted(log, *emails):
    for email in emails:
        consent.grant(log, tenant_id=TENANT_A, restaurant="Bistro Linnea", version=consent.CURRENT,
                      channels=["email"], email=email, source="booking")


def run_campaign(env):  # noqa: F811
    fn, sender = load("marketing"), load("marketing-send")
    _, cmp = draft(env, fn)
    admin(fn, "POST", f"/marketing/campaigns/{cmp['campaignId']}/send", {})
    sender.handler({"source": "schedule"}, CTX)
    return sorted(mail["to"] for mail in outbox(env))


def test_send_needs_a_current_grant_in_the_log(env, log):  # noqa: F811
    make_subscribers(env, 3)
    granted(log, "guest0@example.se", "guest1@example.se")
    consent.withdraw(log, tenant_id=TENANT_A, channel="email", identifier="guest1@example.se", source="admin")
    # guest2 is subscribed in the list but has no consent record: not sent
    assert run_campaign(env) == ["guest0@example.se"]


def withdraw_page(method, token):
    public = load("marketing-public")
    return public.handler({"routeKey": f"{method} /m/w/{{token}}", "pathParameters": {"token": token}}, None)


def test_withdrawal_page_shows_a_button_and_withdraws_on_post(env, log):  # noqa: F811
    make_subscribers(env, 1)
    consent.grant(log, tenant_id=TENANT_A, restaurant="Bistro Linnea", version=consent.CURRENT,
                  channels=["email", "sms"], email="guest0@example.se", phone="+46701234567", source="booking")
    url = consent.withdraw_link("https://x/api", TENANT_A, email="guest0@example.se", phone="+46701234567")
    token = url.rsplit("/", 1)[1]
    page = withdraw_page("GET", token)
    assert page["statusCode"] == 200 and "<form method='post'>" in page["body"]
    assert "e-post och sms" in page["body"] and "Bistro Linnea" in page["body"]
    assert consent.may_send(log, TENANT_A, "email", "guest0@example.se")  # GET alone changes nothing
    done = withdraw_page("POST", token)
    assert done["statusCode"] == 200 and "återkallat" in done["body"]
    assert not consent.may_send(log, TENANT_A, "email", "guest0@example.se")
    assert not consent.may_send(log, TENANT_A, "sms", "+46701234567")
    sid = consent.subject(TENANT_A, "email", "guest0@example.se")
    assert [r["action"] for r in consent.history(log, TENANT_A, sid)] == ["withdraw", "grant"]
    assert subscriber(env, "guest0@example.se")["status"] == "unsubscribed"
    again = withdraw_page("GET", token)
    assert "Du får inga erbjudanden" in again["body"]


def test_withdrawal_page_rejects_bad_links(env, log):  # noqa: F811
    assert withdraw_page("GET", "nonsense")["statusCode"] == 404


def test_unsubscribe_link_is_logged_as_a_withdrawal(env, log):  # noqa: F811
    make_subscribers(env, 1)
    granted(log, "guest0@example.se")
    from shared import marketing as m
    token = m.sign({"k": "u", "t": TENANT_A, "s": m.subscriber_id(TENANT_A, "guest0@example.se"), "c": "x"})
    load("marketing-public").handler({"routeKey": "POST /m/u/{token}", "pathParameters": {"token": token}}, None)
    assert not consent.may_send(log, TENANT_A, "email", "guest0@example.se")
    (latest,) = consent.history(log, TENANT_A, consent.subject(TENANT_A, "email", "guest0@example.se"), limit=1)
    assert latest["source"] == "unsubscribe_link"


def test_spam_complaint_is_a_withdrawal(env, log):  # noqa: F811
    make_subscribers(env, 1)
    granted(log, "guest0@example.se")
    from shared import marketing as m
    sid = m.subscriber_id(TENANT_A, "guest0@example.se")
    env.table.put_item(Item={"PK": "MSG#abc", "SK": "MSG", "tenantId": TENANT_A, "campaignId": "c1",
                             "subscriberId": sid})
    load("marketing-sync").handler({"source": "aws.ses", "detail": {"eventType": "Complaint",
                                                                    "mail": {"messageId": "abc"}}}, None)
    assert not consent.may_send(log, TENANT_A, "email", "guest0@example.se")

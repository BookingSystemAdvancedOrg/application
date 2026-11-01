"""notification: the feature notice entry (CONTRACTS.md 9.4, AP9)."""

import pytest

from tenant_support import LOC_A, LOC_B, TENANT_A, TENANT_B
from test_notification import _outbox, _saved, booking, env, record, run  # env: fixture

EMAIL = {"version": 1, "kind": "gift-cards.code", "tenantId": TENANT_A, "locationId": LOC_A, "channel": "email",
         "to": "buyer@example.se", "subject": "Ditt presentkort", "text": "Koden: ABCD-1234", "html": "<p>Koden</p>",
         "replyTo": "location"}
SMS = {"version": 1, "kind": "waitlist.offered", "tenantId": TENANT_A, "locationId": LOC_A, "channel": "sms",
       "to": "+46701234567", "text": "Ditt bord är klart.", "replyTo": None}


def test_email_through_the_outbox_in_dev(env, monkeypatch, caplog):
    module, ses, sns = env
    s3 = _outbox(monkeypatch)
    caplog.set_level("INFO")
    assert module.handler({"notice": EMAIL}, None) == {"status": "sent"}
    (mail,) = _saved(s3)
    assert mail["to"] == "buyer@example.se" and mail["subject"] == "Ditt presentkort"
    assert "Roma" in mail["from"] and "no-reply@mail.example.se" in mail["from"]
    assert mail["replyTo"] == ["soder@roma.se"] and mail["html"] == "<p>Koden</p>"
    assert ses.calls == []
    assert "buyer@example.se" not in caplog.text and "ABCD-1234" not in caplog.text


def test_reply_to_tenant_or_none(env):
    module, ses, _ = env
    module.handler({"notice": {**EMAIL, "replyTo": "tenant"}}, None)
    module.handler({"notice": {**EMAIL, "replyTo": None, "html": None}}, None)
    assert [c["ReplyToAddresses"] for c in ses.calls] == [["hej@roma.se"], []]


def test_sms_with_sender_name_and_outbox_copy(env, monkeypatch, caplog):
    module, _, sns = env
    s3 = _outbox(monkeypatch)
    caplog.set_level("INFO")
    assert module.handler({"notice": SMS}, None) == {"status": "sent"}
    (sent,) = sns.calls
    assert sent["PhoneNumber"] == "+46701234567" and sent["MessageAttributes"]["AWS.SNS.SMS.SenderID"][
        "StringValue"] == "Roma"
    assert [m["channel"] for m in _saved(s3)] == ["sms"]
    assert "+46701234567" not in caplog.text and "bord" not in caplog.text


@pytest.mark.parametrize("bad", [
    {**EMAIL, "version": 2}, {**EMAIL, "kind": "giftcards.code"}, {**EMAIL, "kind": "gift-cards"},
    {**EMAIL, "kind": "nope.code"}, {**EMAIL, "channel": "fax"}, {**EMAIL, "to": "not-an-address"},
    {**EMAIL, "subject": None}, {**EMAIL, "text": " "}, {**EMAIL, "replyTo": "guest"}, {**EMAIL, "extra": 1},
    {**SMS, "to": "+15551234567"}, {**SMS, "tenantId": None}, "notice",
])
def test_bad_payload_is_logged_and_dropped(env, bad, caplog):
    module, ses, sns = env
    result = module.handler({"notice": bad}, None)
    assert result["status"] == "dropped"
    assert ses.calls == [] and sns.calls == []
    assert "buyer@example.se" not in caplog.text and "+46701234567" not in caplog.text


@pytest.mark.parametrize("tenant_id, location_id", [(TENANT_A, LOC_B), (TENANT_B, LOC_A), ("nope", None)])
def test_foreign_location_or_unknown_tenant_is_dropped(env, tenant_id, location_id):
    module, ses, _ = env
    notice = {**EMAIL, "tenantId": tenant_id, "locationId": location_id}
    assert module.handler({"notice": notice}, None) == {"status": "dropped", "reason": "inactive"}
    assert ses.calls == []


def test_stream_path_unchanged(env):
    module, ses, _ = env
    assert run(module, record(booking(), event_name="INSERT")) == {"batchItemFailures": []}
    assert {c["Destination"]["ToAddresses"][0] for c in ses.calls} == {"anna@example.se", "soder@roma.se"}

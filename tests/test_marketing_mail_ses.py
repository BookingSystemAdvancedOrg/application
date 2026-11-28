"""shared/marketing_mail.send: the SES path (prod, or dev without an outbox
bucket). The campaign tests in test_marketing.py only see the dev outbox,
so this checks the raw email that really goes to SES: one recipient, the
no-reply source, the configuration set and the RFC 8058 one-click
unsubscribe headers."""

from email import message_from_bytes
from email.header import decode_header, make_header

import pytest

from shared import marketing_mail


class FakeSes:
    def __init__(self):
        self.calls = []

    def send_raw_email(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": "ses-1"}


class NoS3:
    def put_object(self, **kwargs):
        raise AssertionError("prod must never write to the dev outbox")


@pytest.fixture
def ses(monkeypatch):
    fake = FakeSes()
    monkeypatch.setattr(marketing_mail, "_clients", {"ses": fake, "s3": NoS3()})
    monkeypatch.setenv("NO_REPLY_EMAIL_ADDRESS", "no-reply@mail.booqy.se")
    monkeypatch.setenv("MARKETING_CONFIGURATION_SET", "booqy-marketing")
    return fake


def _send(**extra):
    return marketing_mail.send(
        to="anna@example.se", subject="Höstmeny på Linnea", text="Hej Anna", html_body="<p>Hej Anna</p>",
        sender=marketing_mail.from_address("Bistro Linnea"), reply_to="hej@linnea.se",
        unsubscribe_url="https://restaurant.booqy.se/api/u/abc", **extra)


def test_prod_sends_raw_email_through_ses_even_with_an_outbox_bucket(ses, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("MAIL_OUTBOX_BUCKET_NAME", "dev-outbox")
    assert _send() == "ses-1"
    (call,) = ses.calls
    assert call["Source"] == "no-reply@mail.booqy.se"
    assert call["Destinations"] == ["anna@example.se"]
    assert call["ConfigurationSetName"] == "booqy-marketing"
    msg = message_from_bytes(call["RawMessage"]["Data"])
    assert str(make_header(decode_header(msg["Subject"]))) == "Höstmeny på Linnea"
    assert msg["To"] == "anna@example.se"
    assert msg["Reply-To"] == "hej@linnea.se"
    assert "no-reply@mail.booqy.se" in msg["From"]
    assert msg["List-Unsubscribe"] == "<https://restaurant.booqy.se/api/u/abc>"
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    parts = {p.get_content_type(): p.get_payload(decode=True).decode("utf-8") for p in msg.walk()
             if not p.is_multipart()}
    assert parts == {"text/plain": "Hej Anna", "text/html": "<p>Hej Anna</p>"}


def test_dev_without_an_outbox_bucket_also_uses_ses(ses, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.delenv("MAIL_OUTBOX_BUCKET_NAME", raising=False)
    monkeypatch.delenv("MARKETING_CONFIGURATION_SET")
    assert _send() == "ses-1"
    assert "ConfigurationSetName" not in ses.calls[0]


def test_a_test_send_without_unsubscribe_link_has_no_list_headers(ses, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    marketing_mail.send(to="owner@linnea.se", subject="Test", text="t", html_body="<p>t</p>",
                        sender=marketing_mail.from_address("Bistro Linnea"))
    msg = message_from_bytes(ses.calls[0]["RawMessage"]["Data"])
    assert msg["List-Unsubscribe"] is None and msg["Reply-To"] is None


def test_outbox_sender_is_readable():
    from email.header import Header
    from email.utils import formataddr
    from shared import marketing_mail
    encoded = formataddr((str(Header("Kåken Ö", "utf-8")), "noreply@mail.dev.booqy.se"))
    assert marketing_mail._plain(encoded) == "Kåken Ö <noreply@mail.dev.booqy.se>"
    assert marketing_mail._plain("noreply@mail.dev.booqy.se") == "noreply@mail.dev.booqy.se"

"""Booking confirmations carry the guest's withdrawal link only when the guest
ticked "offers" and the function has the marketing link key."""

import boto3
import pytest
from moto import mock_aws

from shared import consent, marketing, messages

REGION = "eu-north-1"
T = "01tttttttttttttttttttttttt"
ITEM = {"tenantId": T, "date": "2026-10-30", "startTime": "18:00", "endTime": "20:00", "partySize": 2,
        "customerName": "Anna Gäst", "customerEmail": "anna@example.se", "customerPhone": "+46701234567"}


@pytest.fixture
def key(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t", "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        arn = boto3.client("secretsmanager", region_name=REGION).create_secret(Name="k", SecretString="k" * 40)["ARN"]
        monkeypatch.setenv("MARKETING_LINK_KEY_SECRET_ARN", arn)
        monkeypatch.setenv("MARKETING_LINK_BASE", "https://restaurang.dev.booqy.se/api")
        marketing.reset_key_cache()
        yield
        marketing.reset_key_cache()


def test_confirmation_links_the_withdrawal_page_for_a_consenting_guest(key):
    _, text, html_body = messages.guest_email("confirmed", {**ITEM, "marketingOptIn": True}, {"name": "Roma"}, {},
                                              "https://x/b")
    assert "Ta tillbaka ditt samtycke här: https://restaurang.dev.booqy.se/api/m/w/" in text
    token = text.split("/api/m/w/")[1].split()[0]
    assert consent.read_token(token)[0] == T
    assert "/api/m/w/" in html_body


def test_no_link_without_consent_or_on_other_messages(key):
    assert "/m/w/" not in messages.guest_email("confirmed", ITEM, {"name": "Roma"}, {}, None)[1]
    assert "/m/w/" not in messages.guest_email("reminder", {**ITEM, "marketingOptIn": True}, {"name": "Roma"}, {},
                                               None)[1]


def test_no_link_and_no_failure_without_the_key(monkeypatch):
    monkeypatch.delenv("MARKETING_LINK_BASE", raising=False)
    _, text, _ = messages.guest_email("confirmed", {**ITEM, "marketingOptIn": True}, {"name": "Roma"}, {}, None)
    assert "/m/w/" not in text and "Bokningsbekräftelse" not in text  # body only; subject is separate


def test_signing_failure_still_sends_the_confirmation(monkeypatch):
    monkeypatch.setenv("MARKETING_LINK_BASE", "https://x/api")
    monkeypatch.delenv("MARKETING_LINK_KEY_SECRET_ARN", raising=False)
    marketing.reset_key_cache()
    subject, text, _ = messages.guest_email("confirmed", {**ITEM, "marketingOptIn": True}, {"name": "Roma"}, {}, None)
    assert subject.startswith("Bokningsbekräftelse") and "/m/w/" not in text

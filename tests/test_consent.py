"""shared/consent.py: append-only consent log, latest state, withdrawal links."""

from datetime import datetime, timedelta, timezone

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from shared import consent, marketing

REGION = "eu-north-1"
T = "01tttttttttttttttttttttttt"


def consent_table(res, name="test-consent"):
    return res.create_table(
        TableName=name, BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("PK", "SK", "tenantId", "at")],
        GlobalSecondaryIndexes=[{"IndexName": "byTenant", "Projection": {"ProjectionType": "ALL"},
                                 "KeySchema": [{"AttributeName": "tenantId", "KeyType": "HASH"},
                                               {"AttributeName": "at", "KeyType": "RANGE"}]}])


@pytest.fixture
def table(monkeypatch):
    for key, value in {"AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t", "AWS_DEFAULT_REGION": REGION,
                       "CONSENT_TABLE_NAME": "test-consent"}.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        secret = boto3.client("secretsmanager", region_name=REGION).create_secret(
            Name="link", SecretString="k" * 40)["ARN"]
        monkeypatch.setenv("MARKETING_LINK_KEY_SECRET_ARN", secret)
        marketing.reset_key_cache()
        yield consent_table(boto3.resource("dynamodb", region_name=REGION))
        marketing.reset_key_cache()


def test_no_box_ticked_means_no_consent():
    assert consent.parse_request(None, email="a@b.se") is None
    assert consent.parse_request(None, email="a@b.se", legacy_opt_in=False) is None


def test_new_field_keeps_only_channels_with_a_contact_detail():
    raw = {"channels": ["email", "sms"], "textVersion": consent.CURRENT}
    assert consent.parse_request(raw, email="a@b.se", phone="+46701234567") == (consent.CURRENT, ["email", "sms"])
    assert consent.parse_request(raw, email="a@b.se", phone=None) == (consent.CURRENT, ["email"])
    assert consent.parse_request({"channels": ["sms"], "textVersion": consent.CURRENT}, email="a@b.se") is None


@pytest.mark.parametrize("raw", [
    "yes", {"channels": [], "textVersion": consent.CURRENT}, {"channels": ["fax"], "textVersion": consent.CURRENT},
    {"channels": ["email"], "textVersion": "made_up"}, {"channels": ["email"]},
    {"channels": ["email"], "textVersion": consent.CURRENT, "text": "client wording"},
])
def test_malformed_consent_is_rejected(raw):
    with pytest.raises(ValueError):
        consent.parse_request(raw, email="a@b.se")


def test_legacy_bool_is_email_with_the_old_wording():
    assert consent.parse_request(None, email="a@b.se", legacy_opt_in=True, source="booking") == \
        ("offers_booking_v0", ["email"])
    assert consent.parse_request(None, email="a@b.se", legacy_opt_in=True, source="order") == \
        ("offers_order_v0", ["email"])


def test_every_wording_renders_with_the_restaurant_name():
    for version in consent.TEXTS:
        assert "{" not in consent.render(version, "Roma")
    assert "Roma" in consent.render(consent.CURRENT, "Roma")


def test_grant_writes_one_record_per_channel_with_the_exact_wording(table):
    written = consent.grant(table, tenant_id=T, restaurant="Roma", version=consent.CURRENT,
                            channels=["email", "sms"], email="Gäst@Example.se ", phone="+46701234567",
                            location_id="loc-1", source="booking", ref="r1")
    assert [w["channel"] for w in written] == ["email", "sms"]
    email = written[0]
    assert email["identifier"] == "gäst@example.se"
    assert email["text"] == consent.render(consent.CURRENT, "Roma")
    assert email["textVersion"] == consent.CURRENT and email["source"] == "booking" and email["ref"] == "r1"
    assert email["tenantId"] == T and email["locationId"] == "loc-1" and email["at"]
    assert consent.state(table, T, "email", "gast@x.se") == "none"
    assert consent.state(table, T, "email", "GÄST@example.se") == "granted"
    assert consent.state(table, T, "sms", "+46701234567") == "granted"


def test_withdrawal_is_a_new_record_and_wins(table):
    consent.grant(table, tenant_id=T, restaurant="Roma", version=consent.CURRENT, channels=["email"],
                  email="a@b.se", source="order", at="2026-01-01T10:00:00.000Z")
    consent.withdraw(table, tenant_id=T, channel="email", identifier="a@b.se", source="admin", actor="owner-sub",
                     at="2026-02-01T10:00:00.000Z")
    sid = consent.subject(T, "email", "a@b.se")
    items = consent.history(table, T, sid)
    assert [i["action"] for i in items] == ["withdraw", "grant"]  # nothing overwritten
    assert items[0]["actor"] == "owner-sub"
    assert not consent.may_send(table, T, "email", "a@b.se")
    # a new tick later is a new consent
    consent.grant(table, tenant_id=T, restaurant="Roma", version=consent.CURRENT, channels=["email"],
                  email="a@b.se", source="booking", at="2026-03-01T10:00:00.000Z")
    assert consent.may_send(table, T, "email", "a@b.se")


def test_records_cannot_be_overwritten(table):
    item = consent.grant(table, tenant_id=T, restaurant="Roma", version=consent.CURRENT, channels=["email"],
                         email="a@b.se", source="booking")[0]
    with pytest.raises(ClientError):
        consent._put(table, {**item, "action": "withdraw"})


def test_consent_is_per_restaurant(table):
    consent.grant(table, tenant_id=T, restaurant="Roma", version=consent.CURRENT, channels=["email"],
                  email="a@b.se", source="booking")
    assert not consent.may_send(table, "01oooooooooooooooooooooooo", "email", "a@b.se")


def test_no_record_means_no_send(table):
    assert not consent.may_send(table, T, "email", "nobody@b.se")


def test_withdraw_token_round_trip_and_expiry(table):
    url = consent.withdraw_link("https://api.example.se/api/", T, email="a@b.se", phone="+46701234567")
    assert url.startswith("https://api.example.se/api/m/w/")
    token = url.rsplit("/", 1)[1]
    tenant_id, subjects = consent.read_token(token)
    assert tenant_id == T
    assert subjects == {"email": consent.subject(T, "email", "a@b.se"), "sms": consent.subject(T, "sms", "+46701234567")}
    later = datetime.now(timezone.utc) + timedelta(days=consent.WITHDRAW_LINK_DAYS + 1)
    assert consent.read_token(token, now=later) is None
    assert consent.read_token(token[:-2] + "xx") is None
    unsubscribe = marketing.sign({"k": "u", "t": T, "s": "x"})
    assert consent.read_token(unsubscribe) is None


def test_withdraw_link_needs_a_contact_detail():
    assert consent.withdraw_link("https://x", T) is None


def test_public_text_matches_the_stored_wording():
    text = consent.public_text("Roma")
    assert text["version"] == consent.CURRENT
    assert text["label"] == consent.render(consent.CURRENT, "Roma")
    assert [c["value"] for c in text["channels"]] == ["email", "sms"]

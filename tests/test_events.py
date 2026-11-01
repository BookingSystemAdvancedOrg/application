"""shared/events.py: contract-checked publishing to the feature bus (CONTRACTS.md 5)."""

import json

import pytest

from shared import events

SOLD = {"tenantId": "t1", "cardId": "c1", "locationId": "l1", "amountOre": 50000,
        "expiresAt": "2027-11-01T00:00:00Z"}


class Stub:
    def __init__(self, answer=None):
        self.entries = []
        self.answer = answer or {"FailedEntryCount": 0, "Entries": [{"EventId": "ev-1"}]}

    def put_events(self, Entries):
        self.entries.extend(Entries)
        return self.answer


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setenv("EVENT_BUS_NAME", "dev-booqy-events")
    s = Stub()
    monkeypatch.setattr(events, "_client", s)
    return s


def test_publish_fills_common_fields_and_returns_the_id(stub):
    assert events.publish("gift-cards", "GiftCard.Sold", SOLD) == "ev-1"
    entry = stub.entries[0]
    assert entry["Source"] == "booqy.gift-cards" and entry["DetailType"] == "GiftCard.Sold"
    assert entry["EventBusName"] == "dev-booqy-events"
    detail = json.loads(entry["Detail"])
    assert detail["version"] == 1 and detail["occurredAt"].endswith("Z")
    assert {k: detail[k] for k in SOLD} == SOLD


def test_optional_fields_are_allowed(stub):
    events.publish("meta-ads", "MetaConnection.Connected",
                   {"tenantId": "t", "adAccountId": "a", "pageId": "p", "instagramId": "i"})
    events.publish("google-business", "GoogleConnection.Connected", {"tenantId": "t"})
    assert len(stub.entries) == 2


@pytest.mark.parametrize("feature, detail_type", [("gift-cards", "GiftCard.Stolen"),
                                                  ("nope", "GiftCard.Sold"), ("waitlist", "GiftCard.Sold")])
def test_unknown_source_or_type_raises(stub, feature, detail_type):
    with pytest.raises(ValueError):
        events.publish(feature, detail_type, SOLD)
    assert stub.entries == []


@pytest.mark.parametrize("drop", ["tenantId", "cardId", "amountOre"])
def test_missing_required_field_raises(stub, drop):
    with pytest.raises(ValueError, match=drop):
        events.publish("gift-cards", "GiftCard.Sold", {k: v for k, v in SOLD.items() if k != drop})
    assert stub.entries == []


def test_extra_field_raises(stub):
    with pytest.raises(ValueError, match="buyerEmail"):
        events.publish("gift-cards", "GiftCard.Sold", {**SOLD, "buyerEmail": "a@b.se"})
    assert stub.entries == []


def test_missing_bus_env_raises(stub, monkeypatch):
    monkeypatch.delenv("EVENT_BUS_NAME")
    with pytest.raises(KeyError):
        events.publish("gift-cards", "GiftCard.Sold", SOLD)


def test_a_rejected_entry_raises(monkeypatch):
    monkeypatch.setenv("EVENT_BUS_NAME", "b")
    monkeypatch.setattr(events, "_client", Stub({"FailedEntryCount": 1, "Entries": [{"ErrorCode": "X"}]}))
    with pytest.raises(RuntimeError):
        events.publish("gift-cards", "GiftCard.Sold", SOLD)


def test_publish_through_moto(monkeypatch):
    from moto import mock_aws
    for k, v in {"AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t", "AWS_DEFAULT_REGION": "eu-north-1"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("EVENT_BUS_NAME", "dev-booqy-events")
    monkeypatch.setattr(events, "_client", None)
    with mock_aws():
        import boto3
        boto3.client("events").create_event_bus(Name="dev-booqy-events")
        assert events.publish("waitlist", "Waitlist.Left",
                              {"tenantId": "t", "locationId": "l", "entryId": "e", "date": "2026-11-01",
                               "by": "guest"})
    monkeypatch.setattr(events, "_client", None)

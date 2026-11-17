"""Audit #53: a non-ASCII token answers "invalid" instead of crashing.

hmac.compare_digest raises TypeError for a str with non-ASCII characters;
every public token check compares through shared.safe_compare."""

import pytest

from shared import catering, manage_link, safe_compare, signing
from shared import orders as o
from shared.features.events_tickets import qr

TENANT, LOC = "t-1", "loc-1"


def test_safe_equal():
    assert safe_compare.equal("abc", "abc") and safe_compare.equal(b"abc", "abc")
    assert not safe_compare.equal("abc", "åbc")
    assert not safe_compare.equal("å", "åå")
    assert safe_compare.equal("å", "å")
    assert not safe_compare.equal("abc", "\udcff")
    assert not safe_compare.equal("abc", None) and not safe_compare.equal(None, None)
    assert not safe_compare.equal("abc", 1)


def test_table_code_with_a_non_ascii_signature_is_invalid(monkeypatch):
    monkeypatch.setattr(o, "_sign", lambda *parts: "sig")
    with pytest.raises(ValueError):
        o.read_table_code(TENANT, LOC, "abc.å")


def test_order_status_token_non_ascii_is_no_match(monkeypatch):
    monkeypatch.setattr(o, "_sign", lambda *parts: "sig")
    item = {"tenantId": TENANT, "locationId": LOC, "orderId": "o1"}
    assert o.token_matches(item, "sig")
    assert o.token_matches(item, "å") is False


def test_manage_token_non_ascii_is_no_match(monkeypatch):
    monkeypatch.setattr(manage_link, "token_for", lambda item: "tok")
    item = {"tenantId": TENANT, "locationId": LOC, "reservationId": "r1"}
    assert manage_link.matches(item, "tok")
    assert manage_link.matches(item, "tåk") is False


def test_catering_token_non_ascii_is_no_match(monkeypatch):
    monkeypatch.setattr(catering, "token_for", lambda item: "tok")
    assert catering.token_matches({}, "tok")
    assert catering.token_matches({}, "å") is False


def test_signing_webhook_token_non_ascii_is_rejected():
    assert signing.verify_webhook({"callbackToken": "tok", "provider": "simulator"}, {}, b"", {"t": "å"}) is None


def test_ticket_link_and_scan_with_non_ascii_mac_are_rejected(monkeypatch):
    monkeypatch.setattr(qr.et, "mac", lambda value: b"m" * 32)
    monkeypatch.setattr(qr.et, "valid_id", lambda value: True)
    good = qr.token("t", TENANT, "abc123")
    body = good.split(".")[0]
    assert qr.read_token(good) is not None
    assert qr.read_token(f"{body}.åå") is None
    monkeypatch.setattr(qr, "scan_key", lambda tenant_id, occ_id: "00" * 32)
    assert qr.verify(TENANT, "occ", "abc123", "å" * 16) is False

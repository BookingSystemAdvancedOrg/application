"""Accounting with gift cards (AP13): the two gift card roles, feature
postings through the
accounting.events hook, and the regression that a tenant without gift
cards gets byte-identical SIE, CSV and JSON exports (golden files written
by the code before this change)."""

import base64
import json
import os
from pathlib import Path

import pytest

from hooks_fake import entitle, ever_entitled, install_hook, no_hooks, record_invocations
from shared import accounting as acc
from test_accounting import parse_sie
from tenant_support import LOC_A, TENANT_A
from test_accounting_function import app, call, env  # fixtures used by name

GOLDEN = Path(__file__).parent / "fixtures" / "accounting_run0"
MONTH = {"from": "2026-10-01", "to": "2026-10-31"}


def _export(app, fmt, period=MONTH):
    status, body = call(app, "GET", "/export", query={**period, "format": fmt})
    assert status == 200, body
    return json.dumps(body["entries"], sort_keys=True).encode() if fmt == "json" else base64.b64decode(body["content"])


@pytest.mark.parametrize("fmt", ["sie", "csv", "json"])
def test_without_gift_cards_the_export_is_byte_identical(app, monkeypatch, fmt):
    no_hooks(monkeypatch)
    content = _export(app, fmt)
    if os.environ.get("WRITE_ACCOUNTING_GOLDEN"):
        GOLDEN.mkdir(parents=True, exist_ok=True)
        (GOLDEN / f"export.{fmt}").write_bytes(content)
    assert content == (GOLDEN / f"export.{fmt}").read_bytes()


def test_without_gift_cards_no_lambda_is_invoked(app, monkeypatch):
    seen = record_invocations(monkeypatch)
    for fmt in ("sie", "csv", "json"):
        assert _export(app, fmt) == (GOLDEN / f"export.{fmt}").read_bytes()
    assert seen == []


def test_a_feature_with_no_events_changes_nothing(app, monkeypatch):
    entitle(TENANT_A, "giftCards")
    calls = install_hook(monkeypatch, "gift-cards", "accounting.events", lambda r: {"ok": True, "events": []})
    assert _export(app, "sie") == (GOLDEN / "export.sie").read_bytes()
    assert calls[0]["tenantId"] == TENANT_A and calls[0]["locationId"] == LOC_A
    assert (calls[0]["first"], calls[0]["last"]) == ("2026-10-01", "2026-10-31")


def test_roles_are_in_the_settings_and_accepted():
    settings = acc.settings_of({"locationId": LOC_A})
    assert settings["accounts"]["giftCardLiability"] == "2421"
    assert settings["accounts"]["giftCardBreakage"] == "3990"
    stored = acc.validate_settings({"accounts": {"giftCardLiability": "2422"}}, LOC_A)
    assert stored["accounts"] == {"giftCardLiability": "2422"}
    assert list(acc.ROLES)[-2:] == ["giftCardLiability", "giftCardBreakage"]
    names = acc.account_names(settings["accounts"])
    assert names["2421"] == "Ej inlösta presentkort" and names["3990"] == "Övriga ersättningar och intäkter"


SOLD = {"day": "2026-10-23", "postings": [{"role": "stripe", "amount": 50000, "text": "Presentkort sålda online"},
                                          {"role": "giftCardLiability", "amount": -50000,
                                           "text": "Presentkort sålda online"}], "warnings": []}
REDEEMED = {"day": "2026-10-23", "postings": [
    {"role": "giftCardLiability", "amount": 10000, "text": "Presentkort inlösta online"},
    {"role": "stripe", "amount": -10000, "text": "Presentkort inlösta online"}], "warnings": []}
EXPIRED = {"day": "2026-10-24", "postings": [
    {"role": "giftCardLiability", "amount": 2000, "text": "Presentkort som gått ut"},
    {"role": "giftCardBreakage", "amount": -2000, "text": "Presentkort som gått ut"}], "warnings": ["kontrollera"]}


def test_feature_postings_are_booked_balanced_with_the_new_accounts(app, env, monkeypatch):
    entitle(TENANT_A, "giftCards")
    install_hook(monkeypatch, "gift-cards", "accounting.events",
                 lambda r: {"ok": True, "events": [SOLD, REDEEMED, EXPIRED]})
    status, body = call(app, "GET", "/export", query={"from": "2026-10-23", "to": "2026-10-24", "format": "json"})
    assert status == 200 and body["summary"]["balanced"] is True
    lines = {}
    for e in body["entries"]:
        for ln in e["lines"]:
            lines[(e["date"], ln["account"])] = lines.get((e["date"], ln["account"]), 0) + ln["amount"]
    assert lines[("2026-10-23", "2421")] == -40000 and lines[("2026-10-24", "3990")] == -2000
    assert {"date": "2026-10-24", "text": "kontrollera"} in body["summary"]["warnings"]
    records = parse_sie(_export(app, "sie", {"from": "2026-10-23", "to": "2026-10-24"}))
    konton = {r[1][0]: r[1][1] for r in records if r[0] == "#KONTO"}
    assert konton["2421"] == "Ej inlösta presentkort" and konton["3990"] == "Övriga ersättningar och intäkter"


@pytest.mark.parametrize("answer", [
    lambda r: 1 / 0,
    lambda r: {"ok": False, "status": 409, "code": "busy"},
    lambda r: {"ok": True, "events": [{"day": "2026-10-23", "postings": [{"role": "nope", "amount": 1, "text": "x"},
                                                                         {"role": "stripe", "amount": -1, "text": "x"}]}]},
    lambda r: {"ok": True, "events": [{"day": "2026-10-23", "postings": [{"role": "stripe", "amount": 1, "text": "x"}]}]},
    lambda r: {"ok": True, "events": [{"day": "2025-01-01", "postings": []}]},
])
def test_a_failing_or_wrong_feature_makes_the_export_503(app, monkeypatch, answer):
    entitle(TENANT_A, "giftCards")
    install_hook(monkeypatch, "gift-cards", "accounting.events", answer)
    status, body = call(app, "GET", "/export", query={**MONTH, "format": "sie"})
    assert status == 503 and body["error"] == "service_unavailable"


def test_sie_history_still_has_a_feature_turned_off(app, monkeypatch):
    ever_entitled(TENANT_A, "giftCards")
    install_hook(monkeypatch, "gift-cards", "accounting.events", lambda r: {"ok": True, "events": [SOLD]})
    records = parse_sie(_export(app, "sie", {"from": "2026-10-23", "to": "2026-10-23"}))
    assert "2421" in {r[1][0] for r in records if r[0] == "#KONTO"}

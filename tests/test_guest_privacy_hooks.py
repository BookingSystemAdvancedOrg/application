"""guest-privacy asks the features for their guest data through the
privacy.export / privacy.erase hooks (CONTRACTS.md 9.2, AP7)."""

import json

from hooks_fake import install_hook, no_hooks
from shared import consent
from tenant_support import TENANT_A, TENANT_B, with_tenant_claims
from test_guest_privacy import call, env, guest  # fixtures used by name

ITEMS = [{"cardId": "c1", "last4": "1234"}]


def test_export_merges_feature_items_and_sends_subjects(env, guest, monkeypatch):
    calls = install_hook(monkeypatch, "gift-cards", "privacy.export", lambda r: {"ok": True, "items": ITEMS})
    install_hook(monkeypatch, "waitlist", "privacy.export", lambda r: {"ok": True, "items": []})
    status, body = call(env, "GET", "/privacy/guests", query={"email": "anna@example.se", "phone": "0701234567"})
    assert status == 200
    assert body["features"] == {"gift-cards": ITEMS, "waitlist": []}
    assert [o["orderId"] for o in body["orders"]] == ["o1"]  # own data unchanged
    (req,) = calls
    assert req["hook"] == "privacy.export" and req["tenantId"] == TENANT_A
    assert req["email"] == "anna@example.se" and req["phone"] == "+46701234567"
    assert req["subjects"] == {"email": consent.subject(TENANT_A, "email", "anna@example.se"),
                               "sms": consent.subject(TENANT_A, "sms", "+46701234567")}


def test_not_deployed_features_are_skipped(env, guest, monkeypatch):
    no_hooks(monkeypatch)
    status, body = call(env, "GET", "/privacy/guests", query={"email": "anna@example.se"})
    assert status == 200 and body["features"] == {}
    status, body = call(env, "POST", "/privacy/guests/erase", {"email": "anna@example.se"})
    assert status == 200 and body["features"] == {}


def test_erase_runs_after_own_steps_and_adds_counts(env, guest, monkeypatch, caplog):
    seen = {}

    def erase(req):
        seen["anonymisedFirst"] = not env["marketing"].scan()["Items"]
        return {"ok": True, "erased": {"purchase": 2}, "kept": {"card": 1}}
    install_hook(monkeypatch, "gift-cards", "privacy.erase", erase)
    caplog.set_level("INFO")
    status, body = call(env, "POST", "/privacy/guests/erase", {"email": "anna@example.se"})
    assert status == 200 and body["anonymised"] == 3
    assert body["features"] == {"gift-cards": {"erased": {"purchase": 2}, "kept": {"card": 1}}}
    assert seen["anonymisedFirst"]
    assert "anna@example.se" not in caplog.text


def test_a_failing_feature_makes_the_call_503_and_erase_can_run_again(env, guest, monkeypatch):
    install_hook(monkeypatch, "waitlist", "privacy.erase", lambda r: 1 / 0)
    install_hook(monkeypatch, "guest-crm", "privacy.export", lambda r: {"ok": False, "status": 409, "code": "busy"})
    assert call(env, "POST", "/privacy/guests/erase", {"email": "anna@example.se"})[0] == 503
    assert call(env, "GET", "/privacy/guests", query={"email": "anna@example.se"})[0] == 503
    install_hook(monkeypatch, "waitlist", "privacy.erase", lambda r: {"ok": True, "erased": {"entry": 1}})
    status, body = call(env, "POST", "/privacy/guests/erase", {"email": "anna@example.se"})
    assert status == 200 and body["features"]["waitlist"]["erased"] == {"entry": 1}


def test_staff_and_other_tenants(env, guest, monkeypatch):
    calls = install_hook(monkeypatch, "gift-cards", "privacy.export", lambda r: {"ok": True, "items": []})
    assert call(env, "GET", "/privacy/guests", query={"email": "a@b.se"}, role="staff_user")[0] == 403
    assert calls == []
    event = {"routeKey": "GET /privacy/guests", "queryStringParameters": {"email": "anna@example.se"}}
    out = env["fn"].handler(with_tenant_claims(event, TENANT_B), None)
    body = json.loads(out["body"])
    assert out["statusCode"] == 200 and [b["reservationId"] for b in body["bookings"]] == ["b"]
    assert calls[-1]["tenantId"] == TENANT_B

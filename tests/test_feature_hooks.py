"""shared/feature_hooks.py: the hook envelope and call() (CONTRACTS.md 9.1)."""

import io
import json

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from hooks_fake import install_hook, no_hooks
from shared import feature_hooks, tenant


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")


def test_function_name_has_the_environment_prefix(monkeypatch):
    assert feature_hooks.function_name("gift-cards") == "dev-gift-cards"
    monkeypatch.setenv("ENVIRONMENT", "prod")
    assert feature_hooks.function_name("gift-cards") == "gift-cards"


def test_not_deployed_targets_are_skipped(monkeypatch):
    no_hooks(monkeypatch)
    assert feature_hooks.call("privacy.export", {"tenantId": "t"}) == {}


def test_envelope_shape_and_results(monkeypatch):
    calls = install_hook(monkeypatch, "gift-cards", "privacy.export", lambda r: {"ok": True, "items": [1]})
    result = feature_hooks.call("privacy.export", {"tenantId": "t", "email": "a@b.se", "hook": "x"})
    assert result == {"gift-cards": {"ok": True, "items": [1]}}
    assert calls == [{"hook": "privacy.export", "hookVersion": 1, "tenantId": "t", "email": "a@b.se"}]


def test_ok_false_is_passed_through(monkeypatch):
    install_hook(monkeypatch, "gift-cards", "checkout.giftcard",
                 lambda r: {"ok": False, "status": 409, "code": "card_expired"})
    assert feature_hooks.call("checkout.giftcard", {"tenantId": "t"})["gift-cards"]["code"] == "card_expired"


@pytest.mark.parametrize("fn", [lambda r: 1 / 0, lambda r: "not json", lambda r: {"no": "ok"}])
def test_crash_or_bad_answer_raises_hook_failed(monkeypatch, fn):
    install_hook(monkeypatch, "waitlist", "privacy.erase", fn)
    with pytest.raises(feature_hooks.HookFailed) as exc:
        feature_hooks.call("privacy.erase", {"tenantId": "t"})
    assert exc.value.feature_id == "waitlist"


def test_unknown_target_or_missing_tenant_raises(monkeypatch):
    no_hooks(monkeypatch)
    with pytest.raises(ValueError):
        feature_hooks.call("stripe.event", {"tenantId": "t"}, targets=["waitlist"])
    with pytest.raises(ValueError):
        feature_hooks.call("stripe.event", {})
    with pytest.raises(KeyError):
        feature_hooks.call("no.such", {"tenantId": "t"})


class FakeLambda:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.requests = []

    def invoke(self, **kw):
        self.requests.append(kw)
        return self.behaviour(kw)


def _real_with(monkeypatch, behaviour):
    fake = FakeLambda(behaviour)
    monkeypatch.setattr(feature_hooks, "_client", lambda timeout: fake)
    return fake


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "m"}}, "Invoke")


def test_real_invoker_live_alias_and_not_found(monkeypatch):
    def behaviour(kw):
        if kw["FunctionName"] == "dev-gift-cards":
            return {"Payload": io.BytesIO(json.dumps({"ok": True}).encode())}
        raise _client_error("ResourceNotFoundException")
    fake = _real_with(monkeypatch, behaviour)
    assert feature_hooks.call("privacy.export", {"tenantId": "t"}) == {"gift-cards": {"ok": True}}
    assert {r["Qualifier"] for r in fake.requests} == {"live"}
    assert len(fake.requests) == 4


@pytest.mark.parametrize("behaviour", [
    lambda kw: {"FunctionError": "Unhandled", "Payload": io.BytesIO(b"{}")},
    lambda kw: (_ for _ in ()).throw(_client_error("AccessDeniedException")),
    lambda kw: (_ for _ in ()).throw(ReadTimeoutError(endpoint_url="x")),
])
def test_real_invoker_failures(monkeypatch, behaviour):
    _real_with(monkeypatch, behaviour)
    with pytest.raises(feature_hooks.HookFailed):
        feature_hooks.call("stripe.event", {"tenantId": "t"})


def test_purge_gets_the_long_timeout(monkeypatch):
    seen = []
    monkeypatch.setattr(feature_hooks, "invoke", lambda name, body, timeout: seen.append(timeout) or (None, b'{"ok": true}'))
    feature_hooks.call("tenant.purge", {"tenantId": "t"}, targets=["waitlist"])
    feature_hooks.call("stripe.event", {"tenantId": "t"})
    assert seen == [60, 10]


def test_targets_for_follows_the_entitlements():
    row = {"entitlements": {"features": {"giftCards": True, "waitlist": False}}}
    assert feature_hooks.targets_for("privacy.export", row) == ["gift-cards"]
    assert feature_hooks.targets_for("privacy.export", {}) == []
    assert feature_hooks.targets_for("tenant.purge", None) == []


def test_empty_targets_invoke_nothing(monkeypatch):
    seen = []
    monkeypatch.setattr(feature_hooks, "invoke", lambda *a: seen.append(a))
    assert feature_hooks.call("privacy.export", {"tenantId": "t"}, targets=[]) == {}
    assert seen == []


def test_access_denied_is_skipped_for_purge_only(monkeypatch):
    _real_with(monkeypatch, lambda kw: (_ for _ in ()).throw(_client_error("AccessDeniedException")))
    assert feature_hooks.call("tenant.purge", {"tenantId": "t"}) == {}
    with pytest.raises(feature_hooks.HookFailed):
        feature_hooks.call("privacy.export", {"tenantId": "t"})


def test_ever_on_and_targets_for_ever():
    row = {"entitlements": {"features": {"guestCrm": True}, "featuresEver": {"giftCards"}}}
    assert tenant.ever_on(row, "giftCards") and tenant.ever_on(row, "guestCrm")
    assert not tenant.ever_on(row, "waitlist") and not tenant.ever_on({}, "giftCards")
    assert tenant.ever_on({"entitlements": {"featuresEver": ["waitlist"]}}, "waitlist")
    assert feature_hooks.targets_for("privacy.export", row) == ["guest-crm"]
    assert feature_hooks.targets_for("privacy.export", row, ever=True) == ["guest-crm", "gift-cards"]

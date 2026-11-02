"""Fake feature hook targets for unit tests (CONTRACTS.md 9.1). No real
Lambda: every target without an installed hook answers like a feature
that isn't deployed (skipped).

    calls = install_hook(monkeypatch, "gift-cards", "privacy.export",
                         lambda req: {"ok": True, "items": [{"cardId": "c1"}]})
    ...
    assert calls[0]["tenantId"] == TENANT_A

`fn(request)` gets the decoded envelope and returns the answer. If it
raises, the target counts as crashed (Lambda FunctionError); returning a
non-dict (e.g. "oops") is a non-JSON-object answer.
"""

import json

from shared import feature_hooks


class _Fake:
    def __init__(self):
        self.hooks = {}  # (feature_id, hook) -> (fn, calls)

    def __call__(self, name, body, timeout):
        request = json.loads(body)
        for (feature_id, hook), (fn, calls) in self.hooks.items():
            if feature_hooks.function_name(feature_id) == name and hook == request["hook"]:
                calls.append(request)
                try:
                    answer = fn(request)
                except Exception:  # the target crashed
                    return "Unhandled", b'{"errorMessage": "boom"}'
                return None, (json.dumps(answer) if isinstance(answer, dict) else str(answer)).encode()
        raise feature_hooks.NotDeployed(name)


def install_hook(monkeypatch, feature_id, hook, fn):
    """Installs `fn` as `feature_id`'s answer to `hook`; returns the list
    the requests it receives are appended to."""
    fake = feature_hooks.invoke
    if not isinstance(fake, _Fake):
        fake = _Fake()
        monkeypatch.setattr(feature_hooks, "invoke", fake)
    calls = []
    fake.hooks[(feature_id, hook)] = (fn, calls)
    return calls


def no_hooks(monkeypatch):
    """Every target answers 'not deployed' without touching AWS."""
    monkeypatch.setattr(feature_hooks, "invoke", _Fake())


def record_invocations(monkeypatch):
    """Replaces the invoker with one that only records: the returned list
    stays empty when nothing tries to invoke a feature Lambda."""
    seen = []

    def invoker(name, body, timeout):
        seen.append(name)
        raise feature_hooks.NotDeployed(name)
    monkeypatch.setattr(feature_hooks, "invoke", invoker)
    return seen


def entitle(tenant_id, *keys):
    """Turns entitlement keys on for a tenant in the moto tenant table."""
    import boto3

    from shared import tenant as shared_tenant
    from tenant_support import REGION, TENANT_TABLE
    table = boto3.resource("dynamodb", region_name=REGION).Table(TENANT_TABLE)
    for key in keys:
        table.update_item(Key={"PK": f"TENANT#{tenant_id}", "SK": "PROFILE"},
                          UpdateExpression="SET entitlements.features.#k = :t",
                          ExpressionAttributeNames={"#k": key}, ExpressionAttributeValues={":t": True})
    shared_tenant.reset_caches()


def ever_entitled(tenant_id, *keys):
    """Marks keys as once on (`entitlements.featuresEver`), while they stay off."""
    import boto3

    from shared import tenant as shared_tenant
    from tenant_support import REGION, TENANT_TABLE
    boto3.resource("dynamodb", region_name=REGION).Table(TENANT_TABLE).update_item(
        Key={"PK": f"TENANT#{tenant_id}", "SK": "PROFILE"},
        UpdateExpression="SET entitlements.featuresEver = :e", ExpressionAttributeValues={":e": set(keys)})
    shared_tenant.reset_caches()

"""tenant-purge calls tenant.purge on every deployed feature and writes the
tombstone only once all of them are done (CONTRACTS.md 9.2, AP8)."""

from datetime import timedelta

from hooks_fake import install_hook, no_hooks
from test_tenant_purge import GONE, KEEP, LOC_GONE, LOC_KEEP, NOW, Context, count, env, seed_tenant  # env: fixture


def _status(e, tenant_id):
    return e["t"]["tenant"].get_item(Key={"PK": f"TENANT#{tenant_id}", "SK": "PROFILE"})["Item"]["status"]


def _due(e):
    seed_tenant(e, GONE, LOC_GONE, status="offboarded", purge_at=NOW - timedelta(hours=1), slug="gone")


def test_not_deployed_features_do_not_hold_the_purge(env, monkeypatch):
    no_hooks(monkeypatch)
    _due(env)
    assert env["app"].handler({}, Context())["purged"] == [GONE]
    assert _status(env, GONE) == "purged"


def test_not_done_waits_and_the_next_run_asks_again(env, monkeypatch):
    _due(env)
    answers = iter([{"ok": True, "done": False, "counts": {"rows": 25}},
                    {"ok": True, "done": True, "counts": {"rows": 3}}])
    calls = install_hook(monkeypatch, "waitlist", "tenant.purge", lambda r: next(answers))
    install_hook(monkeypatch, "gift-cards", "tenant.purge", lambda r: {"ok": True, "done": True, "counts": {}})

    first = env["app"].handler({}, Context())
    assert first["purged"] == [] and first["waiting"] == [GONE]
    assert _status(env, GONE) == "offboarded"
    assert count(env["t"]["test-reservation"], f"LOCATION#{LOC_GONE}") == 0   # own steps ran
    assert "Waiting: Restaurang gone" in env["sns"].calls[0]["Message"] and "waitlist" in env["sns"].calls[0]["Message"]

    second = env["app"].handler({"sweep": True, "round": 1}, Context())
    assert second["purged"] == [GONE] and _status(env, GONE) == "purged"
    # the location rows were gone on the second run; the ids came from PROFILE
    assert [c["locationIds"] for c in calls] == [[LOC_GONE], [LOC_GONE]]
    assert [c["round"] for c in calls] == [0, 1]
    assert all(c["tenantId"] == GONE and c["hook"] == "tenant.purge" for c in calls)


def test_a_failing_feature_is_reported_and_others_purged(env, monkeypatch):
    _due(env)
    seed_tenant(env, KEEP, LOC_KEEP, status="offboarded", purge_at=NOW - timedelta(hours=1), slug="keep")

    def purge(req):
        if req["tenantId"] == GONE:
            raise RuntimeError("crash")
        return {"ok": True, "done": True, "counts": {}}
    install_hook(monkeypatch, "meta-ads", "tenant.purge", purge)
    result = env["app"].handler({}, Context())
    assert result["failed"] == [GONE] and result["purged"] == [KEEP]
    assert _status(env, GONE) == "offboarded"
    assert "FAILED" in env["sns"].calls[0]["Subject"] and "meta-ads" in env["sns"].calls[0]["Message"]


def test_active_tenants_never_reach_the_hook(env, monkeypatch):
    seed_tenant(env, KEEP, LOC_KEEP, slug="keep")
    calls = install_hook(monkeypatch, "waitlist", "tenant.purge", lambda r: {"ok": True, "done": True})
    env["app"].handler({}, Context())
    assert calls == []

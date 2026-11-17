"""Audit #39: one tenant (or one campaign) whose Meta pause fails must not
stop the kill-switch sweep from pausing the rest."""

import pytest

from shared.features import meta_ads as ads
from shared.meta_graph import MetaGraphError
from test_meta_ads import CAMP, CAMP2, TENANT_A, _active, connect, env  # noqa: F401 (env is a fixture)


def _kill_switch_on(env):
    assert env.api.handler({"hook": "platform", "op": "meta-ads.settings.put", "tenantId": None,
                            "body": {"killSwitch": True, "maxMonthlyCapSek": 20000}})["ok"]


def test_a_tenant_whose_pause_fails_does_not_stop_the_kill_switch_for_the_rest(env, monkeypatch):
    connect(env)
    _active(env)
    _kill_switch_on(env)
    broken = "tenant-broken"
    monkeypatch.setattr(env.jobs, "connected_tenants", lambda: [broken, TENANT_A])
    monkeypatch.setattr(ads, "blocked_by", lambda tenant_id: True)
    real_pause = ads.pause_all

    def pause_all(tenant_id, by):
        if tenant_id == broken:
            raise MetaGraphError("token_revoked")
        return real_pause(tenant_id, by)
    monkeypatch.setattr(ads, "pause_all", pause_all)
    result = env.jobs.handler({"job": "sweep"})
    assert result["paused"] == 1 and result["failed"] == 1  # the failed kill-switch pause is not hidden
    assert ads.get_campaign(TENANT_A, CAMP)["status"] == "paused"


def test_a_tenant_whose_blocked_check_fails_does_not_stop_the_rest(env, monkeypatch):
    connect(env)
    _active(env)
    _kill_switch_on(env)
    broken = "tenant-broken"
    monkeypatch.setattr(env.jobs, "connected_tenants", lambda: [broken, TENANT_A])

    def blocked_by(tenant_id):
        if tenant_id == broken:
            raise RuntimeError("read failed")
        return True
    monkeypatch.setattr(ads, "blocked_by", blocked_by)
    result = env.jobs.handler({"job": "sweep"})
    assert result["paused"] == 1 and result["failed"] == 1
    assert ads.get_campaign(TENANT_A, CAMP)["status"] == "paused"


@pytest.mark.parametrize("bad", [CAMP, CAMP2])
def test_one_campaign_whose_pause_fails_does_not_stop_the_tenants_other_pauses(env, monkeypatch, bad):
    connect(env)
    rows = {CAMP: _active(env, CAMP), CAMP2: _active(env, CAMP2)}
    good = CAMP2 if bad == CAMP else CAMP
    bad_ids = set(ads.meta_ids(rows[bad]))
    real_set_status = env.graph.set_status

    def set_status(token, object_id, status):
        if object_id in bad_ids:
            raise MetaGraphError("rate_limited")
        return real_set_status(token, object_id, status)
    monkeypatch.setattr(env.graph, "set_status", set_status)
    with pytest.raises(MetaGraphError):
        ads.pause_all(TENANT_A, "kill_switch")
    assert ads.get_campaign(TENANT_A, bad)["status"] == "active"
    assert ads.get_campaign(TENANT_A, good)["status"] == "paused"  # whichever order campaigns() returns

"""Insights and margin per dish (AP14): the five margin fields are money
(stripped for staff), the insights.dishCosts hook is asked only for an
owner with dishMargin on, and with dishMargin off the answer is unchanged."""

import json

import pytest

from hooks_fake import install_hook
from shared import dish_margin
from shared import insights as ins
from shared import tenant as shared_tenant
from tenant_support import LOC_A, LOC_A2, LOC_B, TENANT_A, TENANT_TABLE
from test_insights_functions import api, body, env, event, rollup  # fixtures used by name

QUERY = {"from": "2026-10-19", "to": "2026-10-21", "compare": "none"}
COSTS = {"ok": True, "targetMarginPct": 70, "costs": {"m1": [{"validFrom": "2026-01-01", "costOre": 4000}]}}
MARGIN = ("cost", "margin", "marginPct", "missingCost", "targetMarginPct")


def dish_margin_on(env):
    env.Table(TENANT_TABLE).update_item(Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"},
                                        UpdateExpression="SET entitlements.features.dishMargin = :t",
                                        ExpressionAttributeValues={":t": True})
    shared_tenant.reset_caches()


def fake_apply(summary, costs):
    return {**summary, "missingCost": 1, "targetMarginPct": costs["targetMarginPct"],
            "dishes": [{**d, "cost": 1, "margin": 2, "marginPct": 3} for d in summary["dishes"]]}


def test_without_money_drops_the_margin_fields():
    summary = {"guests": 3, "missingCost": 2, "targetMarginPct": 70, "catering": {}, "byDay": [],
               "dishes": [{"id": "m1", "name": "Pizza", "quantity": 2, "revenue": 1, "cost": 1, "margin": 2,
                           "marginPct": 3}]}
    text = json.dumps(ins.without_money(summary))
    assert not any(f'"{f}"' in text for f in (*MARGIN, "revenue"))
    assert ins.without_money(summary)["dishes"] == [{"id": "m1", "name": "Pizza", "quantity": 2}]


def test_stub_returns_the_summary_unchanged():
    summary = {"dishes": []}
    assert dish_margin.apply(summary, COSTS) is summary


def test_off_the_answer_is_unchanged_and_no_hook_is_called(rollup, api, monkeypatch):
    rollup.handler({}, None)
    before = api.handler(event(QUERY), None)["body"]
    calls = install_hook(monkeypatch, "dish-margin", "insights.dishCosts", lambda r: COSTS)
    assert api.handler(event(QUERY), None)["body"] == before
    assert calls == []


def test_owner_with_dish_margin_gets_costs_applied(rollup, api, env, monkeypatch):
    rollup.handler({}, None)
    dish_margin_on(env)
    calls = install_hook(monkeypatch, "dish-margin", "insights.dishCosts", lambda r: COSTS)
    seen = []
    monkeypatch.setattr(dish_margin, "apply", lambda s, c: seen.append(c) or fake_apply(s, c))
    out = body(api.handler(event(QUERY), None))
    (req,) = calls
    assert req["tenantId"] == TENANT_A and sorted(req["locationIds"]) == [LOC_A, LOC_A2]
    assert (req["first"], req["last"]) == ("2026-10-19", "2026-10-21")
    assert seen == [{"targetMarginPct": 70, "costs": COSTS["costs"]}]
    assert out["current"]["targetMarginPct"] == 70 and out["current"]["missingCost"] == 1


def test_staff_never_trigger_the_hook_or_see_margins(rollup, api, env, monkeypatch):
    rollup.handler({}, None)
    dish_margin_on(env)
    calls = install_hook(monkeypatch, "dish-margin", "insights.dishCosts", lambda r: COSTS)
    monkeypatch.setattr(dish_margin, "apply", fake_apply)
    response = api.handler(event(QUERY, role="staff_user"), None)
    assert response["statusCode"] == 200 and calls == []
    assert not any(f'"{f}"' in response["body"] for f in MARGIN)


@pytest.mark.parametrize("answer", [lambda r: 1 / 0, lambda r: {"ok": False, "status": 409, "code": "x"}])
def test_a_failing_feature_is_503(rollup, api, env, monkeypatch, answer):
    rollup.handler({}, None)
    dish_margin_on(env)
    install_hook(monkeypatch, "dish-margin", "insights.dishCosts", answer)
    assert api.handler(event(QUERY), None)["statusCode"] == 503


def test_foreign_location_is_404_without_a_hook_call(rollup, api, env, monkeypatch):
    dish_margin_on(env)
    calls = install_hook(monkeypatch, "dish-margin", "insights.dishCosts", lambda r: COSTS)
    assert api.handler(event({**QUERY, "locationId": LOC_B}), None)["statusCode"] == 404
    assert calls == []

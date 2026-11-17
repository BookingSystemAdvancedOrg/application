"""The site builder was removed (owner 2026-11-17). Its Lambda stays deployed
until infrastructure removes it, as a stub: every route answers 410 Gone,
workflow tasks fail, hooks do nothing, and no contract lists it any more."""

import importlib.util
import json
from pathlib import Path

import pytest

from shared import contracts

ROOT = Path(__file__).parents[1]


@pytest.fixture(scope="module")
def app():
    spec = importlib.util.spec_from_file_location("sb_stub", ROOT / "functions" / "site-builder" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("route", ["GET /tenant/site-builder", "PUT /tenant/site-builder/draft",
                                   "POST /tenant/site-builder/publish", "POST /public/site-builder/preview"])
def test_every_route_is_gone(app, route):
    method, path = route.split(" ")
    response = app.handler({"requestContext": {"http": {"method": method}}, "routeKey": route, "rawPath": path})
    assert response["statusCode"] == 410
    assert json.loads(response["body"]) == {"error": "gone", "code": "gone"}


def test_hooks_and_tasks(app):
    assert app.handler({"hook": "tenant.purge", "tenantId": "t1"})["ok"] is True
    assert app.handler({"hook": "platform", "op": "site-builder.get"}) == {"ok": False, "status": 410, "code": "gone"}
    with pytest.raises(RuntimeError):
        app.handler({"task": "Promote", "tenantId": "t1", "publishId": "p"})


def test_nothing_points_at_the_builder_any_more():
    assert "siteBuilder" not in contracts.FEATURE_KEYS and "siteBuilder" not in contracts.SITE_CONFIG_FEATURES
    assert not any(k.startswith("siteBuilder") for k in contracts.LIMITS)
    assert "booqy.site-builder" not in contracts.EVENT_SOURCES
    assert all("site-builder" not in h["targets"] for h in contracts.HOOKS.values())
    assert not (ROOT / "shared" / "features" / "site_builder").exists()
    assert not (ROOT / "functions" / "site-builder" / "requirements.txt").exists()

"""shared/console_invoke.py: route templates and the dev-only gate the dev
twin seeder relies on (booqy-console docs/DEV-TWINS.md)."""

import json

import pytest

from shared import console_invoke


def _call(**extra):
    return {"booqyConsole": {"operator": "dev-twin", "tenantId": "t1", "method": "PUT",
                             "path": "/locations/loc-1", **extra}}


def test_api_gateway_events_pass_unchanged():
    event = {"routeKey": "GET /tenant", "requestContext": {}}
    assert console_invoke.unwrap(event, dev_only=True) is event
    assert not console_invoke.is_operator()


def test_route_template_gives_route_key_and_path_parameters(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    event = console_invoke.unwrap(_call(route="/locations/{locationId}", body={"name": "A"}), dev_only=True)
    assert event["routeKey"] == "PUT /locations/{locationId}"
    assert event["pathParameters"] == {"locationId": "loc-1"}
    assert event["rawPath"] == "/locations/loc-1"
    assert json.loads(event["body"]) == {"name": "A"}
    claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
    assert claims["sub"] == "operator:dev-twin" and claims["tenant_id"] == "t1" and claims["role"] == "owner_user"
    assert console_invoke.is_operator()


def test_greedy_proxy_takes_the_rest(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    event = console_invoke.unwrap(_call(method="POST", path="/locations/loc-1/menu/items/abc",
                                        route="/locations/{locationId}/menu/{proxy+}"), dev_only=True)
    assert event["pathParameters"] == {"locationId": "loc-1", "proxy": "items/abc"}


@pytest.mark.parametrize("route", ["/locations", "/locations/{locationId}/menu", "/tenants/{locationId}",
                                   "locations/{locationId}", "/locations/{locationId}/{proxy+}"])
def test_a_path_outside_the_route_is_refused(monkeypatch, route):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    with pytest.raises(console_invoke.Refused):
        console_invoke.unwrap(_call(route=route), dev_only=True)
    assert not console_invoke.is_operator()


def test_dev_only_calls_are_refused_in_prod(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    with pytest.raises(console_invoke.Refused):
        console_invoke.unwrap(_call(route="/locations/{locationId}"), dev_only=True)
    # The Users tab (not dev_only) keeps working in prod.
    assert console_invoke.unwrap(_call(), prefix="")["routeKey"] == "PUT /locations/loc-1"


def test_dev_twin_entry_answers_400_in_prod_and_passes_api_events(monkeypatch):
    seen = []

    @console_invoke.dev_twin_entry
    def handler(event, context):
        seen.append(event)
        return {"statusCode": 200}

    monkeypatch.setenv("ENVIRONMENT", "prod")
    refused = handler(_call(route="/locations/{locationId}"), None)
    assert refused["statusCode"] == 400 and json.loads(refused["body"])["error"] == "console_refused"
    api = {"routeKey": "PUT /locations/{locationId}", "requestContext": {}}
    assert handler(api, None) == {"statusCode": 200} and seen == [api]
    monkeypatch.setenv("ENVIRONMENT", "dev")
    assert handler(_call(route="/locations/{locationId}"), None) == {"statusCode": 200}
    assert seen[-1]["pathParameters"] == {"locationId": "loc-1"}

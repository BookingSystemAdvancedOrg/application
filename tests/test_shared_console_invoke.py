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


def test_query_becomes_query_string_parameters(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    event = console_invoke.unwrap(_call(method="GET", path="/menu-images/presigned-url",
                                        query={"locationId": "loc-1", "contentType": "image/jpeg"}), dev_only=True)
    assert event["queryStringParameters"] == {"locationId": "loc-1", "contentType": "image/jpeg"}
    assert console_invoke.unwrap(_call(), dev_only=True)["queryStringParameters"] is None
    with pytest.raises(console_invoke.Refused):
        console_invoke.unwrap(_call(query={"size": 12}), dev_only=True)


@pytest.mark.parametrize("environment", [None, "", "prod", "staging", "Dev"])
def test_dev_only_calls_need_environment_dev(monkeypatch, environment):
    """Fail closed: a Lambda without ENVIRONMENT (or with any other value)
    refuses the seeder's calls instead of taking them."""
    if environment is None:
        monkeypatch.delenv("ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("ENVIRONMENT", environment)
    with pytest.raises(console_invoke.Refused, match="dev only"):
        console_invoke.unwrap(_call(route="/locations/{locationId}"), dev_only=True)
    assert not console_invoke.is_operator()


def test_dev_twin_entry_route_allowlist(monkeypatch):
    seen = []

    @console_invoke.dev_twin_entry(routes={"PUT /locations/{locationId}", "GET /tenant/settings"})
    def handler(event, context):
        seen.append((event, console_invoke.is_operator()))
        return {"statusCode": 200}

    monkeypatch.setenv("ENVIRONMENT", "dev")
    assert handler(_call(route="/locations/{locationId}"), None) == {"statusCode": 200}
    assert seen[-1][1] is True
    assert handler(_call(method="GET", path="/tenant/settings"), None) == {"statusCode": 200}
    # A route the seed doesn't use: refused before the handler runs, operator cleared.
    for other in (_call(method="POST", path="/locations/loc-1/refund", route="/locations/{locationId}/refund"),
                  _call(method="DELETE", route="/locations/{locationId}"),
                  _call(method="PUT", path="/locations/loc-1")):
        refused = handler(other, None)
        assert refused["statusCode"] == 400 and json.loads(refused["body"])["error"] == "console_refused"
        assert not console_invoke.is_operator()
    assert len(seen) == 2
    # API Gateway, hook and scheduler events are not console calls: any route passes.
    api = {"routeKey": "POST /locations/{locationId}/refund", "requestContext": {}}
    assert handler(api, None) == {"statusCode": 200} and seen[-1] == (api, False)
    assert handler({"job": "sweep"}, None) == {"statusCode": 200} and seen[-1] == ({"job": "sweep"}, False)
    # The allowlist doesn't open prod.
    monkeypatch.setenv("ENVIRONMENT", "prod")
    assert handler(_call(route="/locations/{locationId}"), None)["statusCode"] == 400

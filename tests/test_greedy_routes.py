"""Greedy API Gateway routes (shared/routing.py) for the Run 0-2 features.

The dev HTTP API's route quota is full, so the JWT routes of these features
are registered as `ANY <prefix>` + `ANY <prefix>/{proxy+}` per path prefix.
These tests show that, for every per-endpoint ROUTES key of every feature
handler, the greedy event (route key `ANY ...`, `pathParameters`
`{locationId, proxy}`, the real path in `rawPath`) is answered exactly as the
per-endpoint event is: same dispatch, same path parameters, same tenant,
role, feature and location checks, same errors. Nothing here replaces the
feature suites (two-tenant isolation, owner-only) - they run unchanged on the
per-endpoint events, for which the resolver is a no-op."""

import ast
import importlib.util
import json
import re
from pathlib import Path
from urllib.parse import quote

import pytest
from moto import mock_aws

from shared import contracts, http, routing
from tenant_support import (LOC_A, LOC_B, REGION, TENANT_A, TENANT_B, features_on, seed_tenancy,
                            with_tenant_claims)

ROOT = Path(__file__).parents[1] / "functions"
# The nine Run 0-2 features whose JWT routes are consolidated (owner instruction).
FEATURE_IDS = ("meta-ads", "site-builder", "guest-crm", "google-business", "gift-cards", "waitlist",
               "social-posting", "personalliggare", "phone-agent")
# personalliggare and phone-agent land with application#74; until then the
# handlers on this branch are the ones on disk.
HANDLERS = tuple(f for f in FEATURE_IDS if (ROOT / f / "app.py").exists())
FEATURE_KEY = {feature_id: key for key, feature_id in contracts.FEATURE_IDS.items()}
# H6: the export stays readable without the entitlement (CONTRACTS.md 6.H).
NO_FEATURE_GATE = {("personalliggare", "GET /locations/{locationId}/personalliggare/export")}


def routes_of(feature_id):
    """The module-level ROUTES keys of functions/<id>/app.py (read with ast, nothing imported)."""
    tree = ast.parse((ROOT / feature_id / "app.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "ROUTES" for t in node.targets):
            return [ast.literal_eval(k) for k in node.value.keys]
    raise AssertionError(f"functions/{feature_id}/app.py has no module-level ROUTES dict")


def jwt_routes_of(feature_id):
    return [k for k in routes_of(feature_id) if not k.split(" ", 1)[1].startswith("/public/")]


JWT_KEYS = [(feature_id, key) for feature_id in HANDLERS for key in jwt_routes_of(feature_id)]


def params_of(template):
    return [p[1:-1] for p in template.split("/") if p.startswith("{") and p.endswith("}")]


def values_for(template, **overrides):
    values = {name: LOC_A if name == "locationId" else f"{name}-01" for name in params_of(template)}
    values.update(overrides)
    return values


def fill(template, values):
    return "/" + "/".join(quote(values[p[1:-1]], safe="") if p.startswith("{") else p
                          for p in template.split("/")[1:])


def greedy_prefix(template):
    """The prefix the infra registers greedily: /tenant/<first> or /locations/{locationId}/<first>."""
    parts = template.split("/")[1:]
    return "/" + "/".join(parts[:3 if parts[0] == "locations" else 2])


def greedy_event(method, template, values=None, *, path=None, stage="$default"):
    """The event API Gateway delivers for `method path` through the greedy route of `template`'s prefix."""
    values = values if values is not None else values_for(template)
    path = path if path is not None else fill(template, values)
    prefix = fill(greedy_prefix(template), values)
    visible = path[len("/" + stage):] if stage != "$default" else path  # what the route sees
    assert visible == prefix or visible.startswith(prefix + "/"), (path, prefix)
    rest = visible[len(prefix):].lstrip("/")
    params = {"locationId": values["locationId"]} if "{locationId}" in greedy_prefix(template) else {}
    if rest:
        params["proxy"] = rest
    key = f"ANY {greedy_prefix(template)}" + ("/{proxy+}" if rest else "")
    return {"version": "2.0", "routeKey": key, "rawPath": path, "rawQueryString": "", "pathParameters": params,
            "queryStringParameters": None, "body": None,
            "requestContext": {"stage": stage, "http": {"method": method, "path": path}}}


def endpoint_event(method, template, values=None):
    """The event the per-endpoint route delivers today (what every feature test sends)."""
    values = values if values is not None else values_for(template)
    return {"version": "2.0", "routeKey": f"{method} {template}", "pathParameters": dict(values),
            "queryStringParameters": None, "body": None, "requestContext": {"http": {"method": method}}}


# --- the resolver alone ----------------------------------------------------------------------

@pytest.mark.parametrize("feature_id,key", JWT_KEYS, ids=[f"{f}:{k}" for f, k in JWT_KEYS])
def test_every_route_key_resolves_from_its_greedy_event(feature_id, key):
    routes = dict.fromkeys(routes_of(feature_id))
    method, template = key.split(" ", 1)
    values = values_for(template)
    event = greedy_event(method, template, values)
    assert event["routeKey"] not in routes, "the greedy key must not be a per-endpoint key"
    routing.resolve(event, routes)
    assert event["routeKey"] == key
    assert event["pathParameters"] == values
    assert http.route(event) == (method, template)


@pytest.mark.parametrize("feature_id", HANDLERS)
def test_resolver_is_a_no_op_for_per_endpoint_events(feature_id):
    for key in routes_of(feature_id):
        method, template = key.split(" ", 1)
        event = endpoint_event(method, template)
        # A per-endpoint event never carries rawPath; even a misleading one is ignored.
        event["rawPath"] = "/tenant/somewhere/else"
        before = json.loads(json.dumps(event))
        assert routing.resolve(event, dict.fromkeys(routes_of(feature_id))) is event
        assert event == before


def test_literal_segment_beats_parameter():
    routes = dict.fromkeys(routes_of("gift-cards"))
    event = greedy_event("GET", "/tenant/gift-cards/settings", {})
    routing.resolve(event, routes)
    assert (event["routeKey"], event["pathParameters"]) == ("GET /tenant/gift-cards/settings", {})
    event = greedy_event("GET", "/tenant/gift-cards/{cardId}", {"cardId": "settings2"})
    routing.resolve(event, routes)
    assert (event["routeKey"], event["pathParameters"]) == ("GET /tenant/gift-cards/{cardId}", {"cardId": "settings2"})


def test_parameters_are_percent_decoded_like_api_gateway():
    routes = dict.fromkeys(routes_of("waitlist"))
    # Percent-escapes are decoded (space, slash, UTF-8); a literal "+" stays "+" (a path is not a form).
    event = greedy_event("POST", "/locations/{locationId}/waitlist/{entryId}/offer",
                         path=f"/locations/{LOC_A}/waitlist/a%20b%2F%C3%A4+c/offer")
    routing.resolve(event, routes)
    assert event["routeKey"] == "POST /locations/{locationId}/waitlist/{entryId}/offer"
    assert event["pathParameters"] == {"locationId": LOC_A, "entryId": "a b/ä+c"}


@pytest.mark.parametrize("path", [
    "/locations/loc-a-1/waitlist/",                    # trailing slash
    "/locations/loc-a-1/waitlist/e1",                   # missing action
    "/locations/loc-a-1/waitlist/e1/offer/extra",       # too deep
    "/locations/loc-a-1/waitlist//offer",               # empty parameter
    "/locations/loc-a-1/waitlist/e1/unknown",           # unknown action
])
def test_unknown_sub_paths_leave_the_event_alone(path):
    routes = dict.fromkeys(routes_of("waitlist"))
    event = greedy_event("POST", "/locations/{locationId}/waitlist/{entryId}/offer", path=path)
    before = json.loads(json.dumps(event))
    routing.resolve(event, routes)
    assert event == before
    assert http.route(event)[0] == "ANY" and http.route(event)[1] not in routes


def test_wrong_method_leaves_the_event_alone():
    routes = dict.fromkeys(routes_of("waitlist"))
    event = greedy_event("DELETE", "/locations/{locationId}/waitlist/{entryId}/offer")
    before = json.loads(json.dumps(event))
    routing.resolve(event, routes)
    assert event == before


def test_stage_prefix_is_removed_only_for_a_named_stage():
    routes = dict.fromkeys(routes_of("meta-ads"))
    template = "/locations/{locationId}/meta-ads/campaigns/{campaignId}"
    values = values_for(template)
    staged = greedy_event("GET", template, values, path="/dev" + fill(template, values), stage="dev")
    routing.resolve(staged, routes)
    assert (staged["routeKey"], staged["pathParameters"]) == (f"GET {template}", values)
    default = {"routeKey": "ANY /locations/{locationId}/meta-ads/{proxy+}", "rawPath": "/dev" + fill(template, values),
               "pathParameters": {}, "requestContext": {"stage": "$default", "http": {"method": "GET"}}}
    before = json.loads(json.dumps(default))
    routing.resolve(default, routes)
    assert default == before
    # The stage name alone is not a route and never matches a parameter.
    bare = {"routeKey": "ANY /locations/{locationId}/meta-ads/{proxy+}", "rawPath": "/dev", "pathParameters": {},
            "requestContext": {"stage": "dev", "http": {"method": "GET", "path": "/dev"}}}
    before = json.loads(json.dumps(bare))
    routing.resolve(bare, routes)
    assert bare == before


def test_falls_back_to_request_context_path_and_tolerates_odd_events():
    routes = dict.fromkeys(routes_of("social-posting"))
    event = {"requestContext": {"http": {"method": "get", "path": "/tenant/social-posts/p1"}}}
    routing.resolve(event, routes)
    assert (event["routeKey"], event["pathParameters"]) == ("GET /tenant/social-posts/{postId}", {"postId": "p1"})
    for odd in (None, [], {}, {"routeKey": ["x"]}, {"requestContext": "x"}, {"rawPath": "tenant/social-posts"},
                {"rawPath": "/tenant/social-posts", "requestContext": {"http": {}}}):
        before = json.loads(json.dumps(odd))
        assert routing.resolve(odd, routes) is odd
        assert odd == before


# --- through the real handlers ---------------------------------------------------------------

def load(feature_id):
    spec = importlib.util.spec_from_file_location(f"greedy_{feature_id.replace('-', '_')}",
                                                  ROOT / feature_id / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def env(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
        monkeypatch.setenv("ENVIRONMENT", "dev")
        # Tenant A has every consolidated feature; tenant B has none of them.
        seed_tenancy(monkeypatch, features_a=features_on(*(FEATURE_KEY[f] for f in HANDLERS)))
        yield


def stub_routes(module):
    """Every route target answers 200 with what the dispatch handed it; the
    dispatch (resolver, tenant/role/feature/location checks, errors) is the real one."""
    def target(key):
        def fn(event, ctx=None):
            return http.respond(200, {"key": key, "params": event.get("pathParameters"),
                                      "tenant": ctx.tenant_id if ctx else None,
                                      "location": ctx.location_id if ctx else None})
        return fn
    for key, fn in list(module.ROUTES.items()):
        if fn is not None:
            module.ROUTES[key] = target(key)


def call(module, event, *, tenant=TENANT_A, role="owner_user"):
    with_tenant_claims(event, tenant, role)
    response = module.handler(event, None)
    return response["statusCode"], (json.loads(response["body"]) if response.get("body") else None)


def _pattern(template):
    return "^" + re.sub(r"\\\{[^/]+\\\}", "([^/]+)", re.escape(template)) + "$"


def reference_route(method, path, routes):
    """The ROUTES key API Gateway's per-endpoint routes would have matched for
    `method path` (regex, most literal segments win), or None. Written apart
    from shared/routing.py so the two can disagree."""
    best = None
    for key in routes:
        key_method, template = key.split(" ", 1)
        if key_method == method and re.match(_pattern(template), path):
            literals = sum(1 for p in template.split("/") if not p.startswith("{"))
            if best is None or literals > best[0]:
                best = (literals, key)
    return None if best is None else best[1]


def baseline(module, method, path, **claims):
    """The per-endpoint answer for `method path`: the matched route's event, else the handler's 404."""
    key = reference_route(method, path, module.ROUTES)
    if key is None:
        return call(module, endpoint_event(method, path, {}), **claims)
    template = key.split(" ", 1)[1]
    found = re.match(_pattern(template), path).groups()
    return call(module, endpoint_event(method, template, dict(zip(params_of(template), found))), **claims)


def wrong_method_for(template, routes):
    return next(m for m in ("DELETE", "PATCH", "PUT", "POST", "GET") if f"{m} {template}" not in routes)


@pytest.mark.parametrize("feature_id,key", JWT_KEYS, ids=[f"{f}:{k}" for f, k in JWT_KEYS])
def test_handler_answers_the_greedy_event_like_the_per_endpoint_event(env, feature_id, key):
    module = load(feature_id)
    stub_routes(module)
    method, template = key.split(" ", 1)
    values = values_for(template)
    has_location = "{locationId}" in template

    # Owner of tenant A: dispatched with the decoded parameters and the checked location.
    status, body = call(module, greedy_event(method, template, values))
    assert (status, body) == (200, {"key": key, "params": values, "tenant": TENANT_A,
                                    "location": LOC_A if has_location else None})
    assert call(module, endpoint_event(method, template, values)) == (status, body)

    # Staff of tenant A: whatever the handler decides today (owner_only or dispatched), unchanged.
    staff = call(module, greedy_event(method, template, values), role="staff_user")
    assert staff == call(module, endpoint_event(method, template, values), role="staff_user")
    assert staff[0] in (200, 403), staff
    if staff[0] == 403:
        assert staff[1]["error"] == "owner_only"

    # Owner of tenant B, feature off: refused (H6 export is the documented exception).
    off = call(module, greedy_event(method, template, values), tenant=TENANT_B)
    assert off == call(module, endpoint_event(method, template, values), tenant=TENANT_B)
    if (feature_id, key) in NO_FEATURE_GATE:
        assert off[0] == 404  # LOC_A is not tenant B's
    else:
        assert (off[0], off[1]["error"]) == (403, "feature_not_in_plan")

    # Tenant A reaching for tenant B's location: 404, same body as unknown.
    if has_location:
        foreign = values_for(template, locationId=LOC_B)
        got = call(module, greedy_event(method, template, foreign))
        assert got == call(module, endpoint_event(method, template, foreign))
        assert (got[0], got[1]["error"]) == (404, "not found")

    # A sub-path and a method the key does not have: exactly what the per-endpoint
    # routes gave (a sibling `{param}` route when API Gateway would have picked one,
    # else the handler's own 404, as today).
    deeper = fill(template, values) + "/not-a-route"
    unknown = call(module, greedy_event(method, template, values, path=deeper))
    assert unknown == baseline(module, method, deeper)
    assert unknown[0] == (200 if reference_route(method, deeper, module.ROUTES) else 404), unknown
    if unknown[0] == 404:
        assert unknown[1]["error"] == "not found"
    other = wrong_method_for(template, module.ROUTES)
    wrong = call(module, greedy_event(other, template, values))
    assert wrong == baseline(module, other, fill(template, values))
    assert wrong[0] == (200 if reference_route(other, fill(template, values), module.ROUTES) else 404), wrong
    if wrong[0] == 404:
        assert wrong[1]["error"] == "not found"


@pytest.mark.parametrize("feature_id", HANDLERS)
def test_handler_resolves_before_routing(feature_id):
    source = (ROOT / feature_id / "app.py").read_text(encoding="utf-8")
    resolve_at = source.find("    routing.resolve(event, ROUTES)\n")
    route_at = source.find("    method, path = http.route(event)\n")
    assert 0 <= resolve_at < route_at, f"functions/{feature_id}/app.py must resolve greedy routes before dispatch"
    assert source.count("routing.resolve(event, ROUTES)") == 1


def test_greedy_prefixes_cover_every_jwt_route():
    """The route keys the infra registers per feature (two per prefix), for the consolidation."""
    for feature_id in HANDLERS:
        prefixes = sorted({greedy_prefix(k.split(" ", 1)[1]) for k in jwt_routes_of(feature_id)})
        assert prefixes, feature_id
        for prefix in prefixes:
            assert prefix.startswith(("/tenant/", "/locations/{locationId}/")), (feature_id, prefix)
            assert "{" not in prefix.replace("{locationId}", ""), (feature_id, prefix)

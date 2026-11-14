"""Google is optional (admin "Vi har ingen Google-profil"): PUT link {skipped: true}
marks the location skipped, the location view says so, DELETE link or a real
link undoes it, and another tenant never sees or changes it."""

from test_google_business import LINK, LOC, call, connect, env, link  # noqa: F401 (env is the fixture)
from tenant_support import LOC_A, LOC_A2, LOC_B, TENANT_B


def view(e, loc=LOC_A, **kw):
    return call(e, "GET", LOC, params={"locationId": loc}, **kw)


def skip(e, loc=LOC_A, body=None, **kw):
    return call(e, "PUT", LINK, params={"locationId": loc}, body=body or {"skipped": True}, **kw)


def test_skip_without_connection_persists_and_delete_undoes(env):
    assert "skipped" not in view(env)[1]
    assert skip(env) == (200, {"skipped": True})
    assert view(env)[1]["skipped"] is True
    assert "skipped" not in view(env, loc=LOC_A2)[1]  # per location
    assert skip(env)[0] == 200  # idempotent
    assert call(env, "DELETE", LINK, params={"locationId": LOC_A})[0] == 204
    assert "skipped" not in view(env)[1]


def test_linking_a_place_clears_the_skip_and_skip_refused_while_linked(env):
    skip(env)
    connect(env)
    assert link(env)[0] == 200
    body = view(env)[1]
    assert body["link"]["googleLocation"] == "locations/sim-1" and "skipped" not in body
    assert skip(env)[0] == 409
    call(env, "DELETE", LINK, params={"locationId": LOC_A})
    assert "skipped" not in view(env)[1]


def test_skip_body_validation_and_roles(env):
    assert skip(env, body={"skipped": False})[0] == 400
    assert skip(env, body={"skipped": "yes"})[0] == 400
    assert skip(env, body={"skipped": True, "googleLocation": "locations/sim-1"})[0] == 400
    assert skip(env, role="staff_user")[0] == 403
    assert "skipped" not in view(env)[1]


def test_skip_is_tenant_isolated(env):
    assert skip(env, loc=LOC_B)[0] == 404  # tenant A cannot touch tenant B's location
    skip(env)
    assert "skipped" not in view(env, loc=LOC_B, tenant=TENANT_B)[1]
    assert view(env, loc=LOC_A, tenant=TENANT_B)[0] == 404

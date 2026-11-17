"""Per-IP limit on POST /auth/login (audit round 2, finding 10).

Failed sign-ins count against the caller's IP address; successful ones are
given back, so a restaurant with many staff behind one IP address is never
blocked at shift start, while a flood of wrong passwords stops before it
reaches Cognito.
"""

import json
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from error_body import err
from test_manage_auth import _GuardTable, app, auth_event  # noqa: F401


def _wrong_password():
    return ClientError(
        {"Error": {"Code": "NotAuthorizedException", "Message": "Incorrect username or password."}},
        "InitiateAuth",
    )


@pytest.fixture
def guard(app, monkeypatch):  # noqa: F811
    from shared import request_guard

    monkeypatch.setenv("REQUEST_GUARD_TABLE_NAME", "guard")
    table = _GuardTable()
    monkeypatch.setattr(request_guard, "table", lambda name: table)
    monkeypatch.setattr(app, "LOGIN_LIMITS", (("auth-login-ip", 3, 300), ("auth-login-ip-hour", 5, 3600)))
    return table


def _login(app, ip, username="staff@example.com"):  # noqa: F811
    body = {"username": username, "password": "Fel-losen1"}
    return app.handler(auth_event("login", body, ip=ip), None)


def test_failed_logins_are_limited_per_ip_address(app, guard):  # noqa: F811
    cognito = Mock()
    cognito.initiate_auth.side_effect = _wrong_password()
    app._cognito_client = cognito

    codes = [_login(app, "203.0.113.5", f"s{i}@example.com") for i in range(4)]

    assert [r["statusCode"] for r in codes] == [401, 401, 401, 429]
    assert json.loads(codes[3]["body"]) == err("too many attempts")
    assert 0 < int(codes[3]["headers"]["Retry-After"]) <= 300
    assert codes[3]["headers"]["Cache-Control"] == "no-store"
    assert cognito.initiate_auth.call_count == 3
    # Another address isn't affected.
    assert _login(app, "198.51.100.7")["statusCode"] == 401


def test_successful_logins_do_not_use_up_the_limit(app, guard):  # noqa: F811
    cognito = Mock()
    cognito.initiate_auth.return_value = {
        "AuthenticationResult": {
            "AccessToken": "a", "IdToken": "i", "RefreshToken": "r",
            "ExpiresIn": 3600, "TokenType": "Bearer",
        }
    }
    app._cognito_client = cognito

    # Many staff behind one NAT address signing in at shift start.
    codes = [_login(app, "203.0.113.9", f"s{i}@example.com")["statusCode"] for i in range(20)]

    assert codes == [200] * 20


def test_default_limits_leave_room_for_typos_and_shared_addresses(app):  # noqa: F811
    limits = {kind: (n, window) for kind, n, window in app.LOGIN_LIMITS}
    assert limits["auth-login-ip"] == (30, 300)
    assert limits["auth-login-ip-hour"] == (100, 3600)


def test_login_without_the_guard_table_is_not_limited(app, monkeypatch):  # noqa: F811
    monkeypatch.delenv("REQUEST_GUARD_TABLE_NAME", raising=False)
    cognito = Mock()
    cognito.initiate_auth.side_effect = _wrong_password()
    app._cognito_client = cognito

    assert {_login(app, "203.0.113.5")["statusCode"] for _ in range(40)} == {401}

"""Cookie sessions in manage-auth: the refresh token never reaches script."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError


APP_PATH = Path(__file__).parents[1] / "functions" / "manage-auth" / "app.py"
ORIGIN = "https://restaurant.booqy.se"


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "local-test-pool")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "local-test-client")
    monkeypatch.setenv("COGNITO_CLIENT_SECRET", "local-test-secret")
    monkeypatch.setenv("COOKIE_ALLOWED_ORIGINS", "http://localhost:7070/")
    monkeypatch.setenv("CLOUDFRONT_EDGE_SECRET", "edge-s3cret")
    spec = importlib.util.spec_from_file_location("manage_auth_cookies", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def event(proxy, body=None, headers=None, cookies=None):
    e = {
        "requestContext": {"http": {"method": "POST"}},
        "pathParameters": {"proxy": proxy},
        "headers": {
            "x-booqy-session": "cookie",
            "origin": ORIGIN,
            "x-booqy-edge": "edge-s3cret",
            "x-booqy-viewer-host": "restaurant.booqy.se",
            **(headers or {}),
        },
        "body": json.dumps(body if body is not None else {}),
        "isBase64Encoded": False,
    }
    if cookies is not None:
        e["cookies"] = cookies
    return e


def tokens(refresh="new-refresh-token"):
    result = {"AccessToken": "access", "IdToken": "id", "ExpiresIn": 3600, "TokenType": "Bearer"}
    if refresh:
        result["RefreshToken"] = refresh
    return {"AuthenticationResult": result}


def parse_cookie(set_cookie):
    parts = [p.strip() for p in set_cookie.split(";")]
    name, value = parts[0].split("=", 1)
    attrs = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        attrs[k.lower()] = v
    return name, value, attrs


def test_login_puts_refresh_token_in_hardened_cookie(app):
    cognito = Mock()
    cognito.initiate_auth.return_value = tokens("rt-1")
    app._cognito_client = cognito

    response = app.handler(event("login", {"username": "a@b.se", "password": "pw"}), None)

    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert "RefreshToken" not in body["authenticationResult"]
    assert "rt-1" not in response["body"]
    (cookie,) = response["cookies"]
    name, value, attrs = parse_cookie(cookie)
    assert name == "__Secure-booqy_rt"
    assert value == "rt-1"
    assert attrs["path"] == "/api/auth"
    assert attrs["max-age"] == "43200"
    assert "domain" not in attrs
    for flag in ("secure", "httponly"):
        assert flag in attrs
    assert attrs["samesite"] == "Strict"


def test_login_without_header_keeps_legacy_body(app):
    cognito = Mock()
    cognito.initiate_auth.return_value = tokens("rt-1")
    app._cognito_client = cognito
    e = event("login", {"username": "a@b.se", "password": "pw"})
    del e["headers"]["x-booqy-session"]

    response = app.handler(e, None)

    assert json.loads(response["body"])["authenticationResult"]["RefreshToken"] == "rt-1"
    assert "cookies" not in response


def test_challenge_completion_also_sets_cookie(app):
    cognito = Mock()
    cognito.respond_to_auth_challenge.return_value = tokens("rt-2")
    app._cognito_client = cognito
    body = {"challengeName": "NEW_PASSWORD_REQUIRED", "session": "s", "username": "a@b.se",
            "responses": {"NEW_PASSWORD": "Secret123!"}}

    response = app.handler(event("challenge", body), None)

    assert parse_cookie(response["cookies"][0])[1] == "rt-2"
    assert "rt-2" not in response["body"]


def test_refresh_reads_cookie_and_rotates_it(app):
    cognito = Mock()
    cognito.get_tokens_from_refresh_token.return_value = tokens("rt-rotated")
    app._cognito_client = cognito

    response = app.handler(event("refresh", cookies=["other=1", "__Secure-booqy_rt=rt-old"]), None)

    assert response["statusCode"] == 200
    cognito.get_tokens_from_refresh_token.assert_called_once_with(
        RefreshToken="rt-old", ClientId="local-test-client", ClientSecret="local-test-secret")
    assert parse_cookie(response["cookies"][0])[1] == "rt-rotated"
    assert "rt-rotated" not in response["body"]


def test_refresh_without_rotation_leaves_cookie_alone(app):
    cognito = Mock()
    cognito.get_tokens_from_refresh_token.return_value = tokens(refresh=None)
    app._cognito_client = cognito

    response = app.handler(event("refresh", cookies=["__Secure-booqy_rt=rt-old"]), None)

    assert response["statusCode"] == 200
    assert "cookies" not in response


def test_refresh_also_reads_a_cookie_header(app):
    cognito = Mock()
    cognito.get_tokens_from_refresh_token.return_value = tokens(refresh=None)
    app._cognito_client = cognito

    response = app.handler(event("refresh", headers={"cookie": "a=1; __Secure-booqy_rt=rt-h"}), None)

    assert response["statusCode"] == 200
    assert cognito.get_tokens_from_refresh_token.call_args.kwargs["RefreshToken"] == "rt-h"


def test_refresh_without_cookie_is_401(app):
    cognito = Mock()
    app._cognito_client = cognito

    response = app.handler(event("refresh", cookies=[]), None)

    assert response["statusCode"] == 401
    cognito.get_tokens_from_refresh_token.assert_not_called()


def test_expired_refresh_clears_the_cookie(app):
    cognito = Mock()
    cognito.get_tokens_from_refresh_token.side_effect = ClientError(
        {"Error": {"Code": "NotAuthorizedException", "Message": "Refresh Token has expired"}},
        "GetTokensFromRefreshToken",
    )
    app._cognito_client = cognito

    response = app.handler(event("refresh", cookies=["__Secure-booqy_rt=rt-old"]), None)

    assert response["statusCode"] == 401
    name, value, attrs = parse_cookie(response["cookies"][0])
    assert (name, value, attrs["max-age"]) == ("__Secure-booqy_rt", "", "0")


@pytest.mark.parametrize("route", ["refresh", "logout"])
def test_cookie_routes_refuse_foreign_origin(app, route):
    cognito = Mock()
    app._cognito_client = cognito

    response = app.handler(
        event(route, headers={"origin": "https://evil.example"}, cookies=["__Secure-booqy_rt=rt"]), None)

    assert response["statusCode"] == 403
    assert json.loads(response["body"]) == {"error": "origin not allowed"}
    cognito.get_tokens_from_refresh_token.assert_not_called()
    cognito.revoke_token.assert_not_called()


@pytest.mark.parametrize("route", ["refresh", "logout"])
def test_cookie_routes_refuse_missing_origin(app, route):
    e = event(route, cookies=["__Secure-booqy_rt=rt"])
    del e["headers"]["origin"]
    app._cognito_client = Mock()

    assert app.handler(e, None)["statusCode"] == 403


def test_cloudfront_address_works_too(app):
    cognito = Mock()
    cognito.get_tokens_from_refresh_token.return_value = tokens(refresh=None)
    app._cognito_client = cognito
    headers = {"origin": "https://dxyz.cloudfront.net", "x-booqy-viewer-host": "dxyz.cloudfront.net"}

    response = app.handler(event("refresh", headers=headers, cookies=["__Secure-booqy_rt=rt"]), None)

    assert response["statusCode"] == 200


def test_viewer_host_without_edge_secret_is_not_trusted(app):
    app._cognito_client = Mock()
    headers = {"x-booqy-edge": "guess"}

    response = app.handler(event("refresh", headers=headers, cookies=["__Secure-booqy_rt=rt"]), None)

    assert response["statusCode"] == 403


def test_direct_api_gateway_call_is_refused(app):
    app._cognito_client = Mock()
    e = event("refresh", cookies=["__Secure-booqy_rt=rt"])
    del e["headers"]["x-booqy-edge"]
    del e["headers"]["x-booqy-viewer-host"]

    assert app.handler(e, None)["statusCode"] == 403


def test_local_dev_origin_from_allowlist(app):
    cognito = Mock()
    cognito.get_tokens_from_refresh_token.return_value = tokens(refresh=None)
    app._cognito_client = cognito
    e = event("refresh", headers={"origin": "http://localhost:7070"}, cookies=["__Secure-booqy_rt=rt"])
    del e["headers"]["x-booqy-edge"]

    assert app.handler(e, None)["statusCode"] == 200


def test_logout_requires_the_session_header(app):
    e = event("logout", cookies=["__Secure-booqy_rt=rt"])
    del e["headers"]["x-booqy-session"]
    cognito = Mock()
    app._cognito_client = cognito

    response = app.handler(e, None)

    assert response["statusCode"] == 403
    cognito.revoke_token.assert_not_called()


def test_logout_revokes_and_clears(app):
    cognito = Mock()
    app._cognito_client = cognito

    response = app.handler(event("logout", cookies=["__Secure-booqy_rt=rt-live"]), None)

    assert response["statusCode"] == 200
    cognito.revoke_token.assert_called_once_with(
        Token="rt-live", ClientId="local-test-client", ClientSecret="local-test-secret")
    assert parse_cookie(response["cookies"][0])[2]["max-age"] == "0"


def test_logout_clears_cookie_even_if_revoke_fails(app):
    cognito = Mock()
    cognito.revoke_token.side_effect = ClientError(
        {"Error": {"Code": "UnsupportedTokenTypeException", "Message": "x"}}, "RevokeToken")
    app._cognito_client = cognito

    response = app.handler(event("logout", cookies=["__Secure-booqy_rt=rt-live"]), None)

    assert response["statusCode"] == 200
    assert parse_cookie(response["cookies"][0])[2]["max-age"] == "0"


def test_garbage_cookie_value_is_refused_and_cleared(app):
    cognito = Mock()
    app._cognito_client = cognito

    response = app.handler(event("refresh", cookies=["__Secure-booqy_rt=bad value;x"]), None)

    assert response["statusCode"] == 401
    assert parse_cookie(response["cookies"][0])[2]["max-age"] == "0"
    cognito.get_tokens_from_refresh_token.assert_not_called()


def test_two_cookies_with_the_name_are_refused(app):
    """Cookie tossing: a sibling subdomain plants a second copy with a wider Domain."""
    cognito = Mock()
    app._cognito_client = cognito

    response = app.handler(
        event("refresh", cookies=["__Secure-booqy_rt=attacker", "__Secure-booqy_rt=victim"]), None)

    assert response["statusCode"] == 401
    assert parse_cookie(response["cookies"][0])[2]["max-age"] == "0"
    cognito.get_tokens_from_refresh_token.assert_not_called()


def test_reused_rotated_token_is_401_and_clears_cookie(app):
    cognito = Mock()
    cognito.get_tokens_from_refresh_token.side_effect = ClientError(
        {"Error": {"Code": "RefreshTokenReuseException", "Message": "reused"}}, "GetTokensFromRefreshToken")
    app._cognito_client = cognito

    response = app.handler(event("refresh", cookies=["__Secure-booqy_rt=old"]), None)

    assert response["statusCode"] == 401
    assert parse_cookie(response["cookies"][0])[2]["max-age"] == "0"

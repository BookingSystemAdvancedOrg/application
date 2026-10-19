import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError


APP_PATH = (
    Path(__file__).parents[1]
    / "functions"
    / "manage-auth"
    / "app.py"
)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "local-test-pool")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "local-test-client")
    monkeypatch.setenv("COGNITO_CLIENT_SECRET", "local-test-secret")

    spec = importlib.util.spec_from_file_location("manage_auth_app", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


def make_event(method="POST", proxy="login"):
    return {
        "requestContext": {
            "http": {
                "method": method,
            }
        },
        "pathParameters": {
            "proxy": proxy,
        },
        "body": "{}",
        "isBase64Encoded": False,
    }


def test_requires_cognito_client_secret(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "local-test-pool")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "local-test-client")
    monkeypatch.delenv("COGNITO_CLIENT_SECRET", raising=False)

    spec = importlib.util.spec_from_file_location(
        "manage_auth_without_client_secret",
        APP_PATH,
    )
    module = importlib.util.module_from_spec(spec)

    with pytest.raises(KeyError, match="COGNITO_CLIENT_SECRET"):
        spec.loader.exec_module(module)


def test_calculates_secret_hash(app):
    assert app._secret_hash("staff@example.com") == (
        "lBMgbQL20sOl5Whi4M9rO7OsXqnaDwwYQlzlEM4j1G4="
    )


def test_unknown_route_returns_404(app):
    response = app.handler(make_event(proxy="unknown"), None)

    assert response["statusCode"] == 404
    assert json.loads(response["body"]) == {"error": "not found"}


def test_non_post_method_returns_405(app):
    response = app.handler(make_event(method="GET"), None)

    assert response["statusCode"] == 405
    assert response["headers"]["Allow"] == "POST"
    assert json.loads(response["body"]) == {"error": "method not allowed"}

@pytest.mark.parametrize(
    ("body", "expected_error"),
    [
        (None, "request body is required"),
        ("", "request body is required"),
        ("{", "request body must be valid JSON"),
        ("[]", "request body must be a JSON object"),
    ],
)
def test_rejects_invalid_request_body(app, body, expected_error):
    event = make_event()
    event["body"] = body

    response = app.handler(event, None)

    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": expected_error}
@pytest.mark.parametrize(
    ("body", "expected_error"),
    [
        ({"password": "Example123!"}, "username is required"),
        ({"username": "staff@example.com"}, "password is required"),
        (
            {"username": "", "password": "Example123!"},
            "username is required",
        ),
    ],
)
def test_login_requires_credentials(app, body, expected_error):
    event = make_event()
    event["body"] = json.dumps(body)

    response = app.handler(event, None)

    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": expected_error}   



def test_login_calls_cognito(app):
    cognito = Mock()
    cognito.initiate_auth.return_value = {
        "AuthenticationResult": {
            "AccessToken": "access-token",
            "IdToken": "id-token",
            "RefreshToken": "refresh-token",
            "ExpiresIn": 3600,
            "TokenType": "Bearer",
        }
    }
    app._cognito_client = cognito

    event = make_event(proxy="login")
    event["body"] = json.dumps(
        {
            "username": "staff@example.com",
            "password": "Example123!",
        }
    )

    response = app.handler(event, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["status"] == "authenticated"
    assert body["authenticationResult"]["AccessToken"] == "access-token"

    cognito.initiate_auth.assert_called_once_with(
        AuthFlow="USER_PASSWORD_AUTH",
        ClientId="local-test-client",
        AuthParameters={
            "USERNAME": "staff@example.com",
            "PASSWORD": "Example123!",
            "SECRET_HASH": (
                "lBMgbQL20sOl5Whi4M9rO7OsXqnaDwwYQlzlEM4j1G4="
            ),
        },
    )


def test_login_returns_challenge(app):
    cognito = Mock()
    cognito.initiate_auth.return_value = {
        "ChallengeName": "NEW_PASSWORD_REQUIRED",
        "ChallengeParameters": {
            "USER_ID_FOR_SRP": "staff-user-id",
        },
        "Session": "challenge-session",
    }
    app._cognito_client = cognito

    event = make_event(proxy="login")
    event["body"] = json.dumps(
        {
            "username": "staff@example.com",
            "password": "Temporary123!",
        }
    )

    response = app.handler(event, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body == {
        "status": "challenge",
        "challengeName": "NEW_PASSWORD_REQUIRED",
        "challengeParameters": {
            "USER_ID_FOR_SRP": "staff-user-id",
        },
        "session": "challenge-session",
        "challengeUsername": "staff-user-id",
    }


@pytest.mark.parametrize(
    ("error_code", "expected_status", "expected_message"),
    [
        (
            "NotAuthorizedException",
            401,
            "invalid username or password",
        ),
        (
            "UserNotFoundException",
            401,
            "invalid username or password",
        ),
        (
            "UserNotConfirmedException",
            403,
            "user is not confirmed",
        ),
        (
            "PasswordResetRequiredException",
            403,
            "password reset required",
        ),
        (
            "TooManyRequestsException",
            429,
            "too many authentication attempts",
        ),
        (
            "InvalidParameterException",
            400,
            "invalid authentication request",
        ),
        (
            "InternalErrorException",
            502,
            "authentication service unavailable",
        ),
    ],
)
def test_login_maps_cognito_errors(
    app,
    error_code,
    expected_status,
    expected_message,
):
    cognito = Mock()
    cognito.initiate_auth.side_effect = ClientError(
        {
            "Error": {
                "Code": error_code,
                "Message": "sensitive AWS message",
            }
        },
        "InitiateAuth",
    )
    app._cognito_client = cognito

    event = make_event(proxy="login")
    event["body"] = json.dumps(
        {
            "username": "staff@example.com",
            "password": "Incorrect123!",
        }
    )

    response = app.handler(event, None)

    assert response["statusCode"] == expected_status
    assert json.loads(response["body"]) == {
        "error": expected_message,
    }
    assert "sensitive AWS message" not in response["body"]
    assert response["headers"]["Cache-Control"] == "no-store"


def test_login_maps_transport_errors_to_502(app):
    cognito = Mock()
    cognito.initiate_auth.side_effect = EndpointConnectionError(
        endpoint_url="https://cognito-idp.eu-north-1.amazonaws.com",
    )
    app._cognito_client = cognito

    event = make_event(proxy="login")
    event["body"] = json.dumps(
        {
            "username": "staff@example.com",
            "password": "Example123!",
        }
    )

    response = app.handler(event, None)

    assert response["statusCode"] == 502
    assert json.loads(response["body"]) == {
        "error": "authentication service unavailable",
    }
    assert response["headers"]["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    ("body", "expected_error"),
    [
        (
            {
                "session": "challenge-session",
                "username": "staff@example.com",
                "responses": {"NEW_PASSWORD": "NewPassword123!"},
            },
            "challengeName is required",
        ),
        (
            {
                "challengeName": "NEW_PASSWORD_REQUIRED",
                "username": "staff@example.com",
                "responses": {"NEW_PASSWORD": "NewPassword123!"},
            },
            "session is required",
        ),
        (
            {
                "challengeName": "NEW_PASSWORD_REQUIRED",
                "session": "challenge-session",
                "responses": {"NEW_PASSWORD": "NewPassword123!"},
            },
            "username is required",
        ),
        (
            {
                "challengeName": "NEW_PASSWORD_REQUIRED",
                "session": "challenge-session",
                "username": "staff@example.com",
            },
            "responses is required",
        ),
        (
            {
                "challengeName": "NEW_PASSWORD_REQUIRED",
                "session": "challenge-session",
                "username": "staff@example.com",
                "responses": {},
            },
            "responses is required",
        ),
        (
            {
                "challengeName": "NEW_PASSWORD_REQUIRED",
                "session": "challenge-session",
                "username": "staff@example.com",
                "responses": {"NEW_PASSWORD": 123},
            },
            "responses must contain non-empty string keys and values",
        ),
        (
            {
                "challengeName": "NEW_PASSWORD_REQUIRED",
                "session": "challenge-session",
                "username": "staff@example.com",
                "responses": {"USERNAME": "different@example.com"},
            },
            "responses must not include USERNAME or SECRET_HASH",
        ),
    ],
)
def test_challenge_validates_request(app, body, expected_error):
    cognito = Mock()
    app._cognito_client = cognito
    event = make_event(proxy="challenge")
    event["body"] = json.dumps(body)

    response = app.handler(event, None)

    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": expected_error}
    cognito.respond_to_auth_challenge.assert_not_called()


def test_challenge_calls_cognito(app):
    cognito = Mock()
    cognito.respond_to_auth_challenge.return_value = {
        "AuthenticationResult": {
            "AccessToken": "access-token",
            "IdToken": "id-token",
            "RefreshToken": "refresh-token",
            "ExpiresIn": 3600,
            "TokenType": "Bearer",
        }
    }
    app._cognito_client = cognito

    event = make_event(proxy="challenge")
    event["body"] = json.dumps(
        {
            "challengeName": "NEW_PASSWORD_REQUIRED",
            "session": "challenge-session",
            "username": "staff-user-id",
            "responses": {
                "NEW_PASSWORD": "NewPassword123!",
            },
        }
    )

    response = app.handler(event, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["status"] == "authenticated"
    assert body["authenticationResult"]["AccessToken"] == "access-token"
    cognito.respond_to_auth_challenge.assert_called_once_with(
        ClientId="local-test-client",
        ChallengeName="NEW_PASSWORD_REQUIRED",
        Session="challenge-session",
        ChallengeResponses={
            "NEW_PASSWORD": "NewPassword123!",
            "USERNAME": "staff-user-id",
            "SECRET_HASH": (
                "cSBwbixaKd5GK//CRUV4iOwch8vu+HGqPt9qROOPaHU="
            ),
        },
    )


def test_challenge_can_return_another_challenge(app):
    cognito = Mock()
    cognito.respond_to_auth_challenge.return_value = {
        "ChallengeName": "SOFTWARE_TOKEN_MFA",
        "ChallengeParameters": {},
        "Session": "next-session",
    }
    app._cognito_client = cognito

    event = make_event(proxy="challenge")
    event["body"] = json.dumps(
        {
            "challengeName": "NEW_PASSWORD_REQUIRED",
            "session": "challenge-session",
            "username": "staff-user-id",
            "responses": {
                "NEW_PASSWORD": "NewPassword123!",
            },
        }
    )

    response = app.handler(event, None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {
        "status": "challenge",
        "challengeName": "SOFTWARE_TOKEN_MFA",
        "challengeParameters": {},
        "session": "next-session",
        "challengeUsername": "staff-user-id",
    }


@pytest.mark.parametrize(
    ("error_code", "expected_message"),
    [
        ("CodeMismatchException", "invalid challenge response"),
        ("ExpiredCodeException", "challenge has expired"),
        ("InvalidPasswordException", "password does not meet requirements"),
        (
            "PasswordHistoryPolicyViolationException",
            "password does not meet requirements",
        ),
    ],
)
def test_challenge_maps_cognito_errors(app, error_code, expected_message):
    cognito = Mock()
    cognito.respond_to_auth_challenge.side_effect = ClientError(
        {
            "Error": {
                "Code": error_code,
                "Message": "sensitive AWS message",
            }
        },
        "RespondToAuthChallenge",
    )
    app._cognito_client = cognito

    event = make_event(proxy="challenge")
    event["body"] = json.dumps(
        {
            "challengeName": "NEW_PASSWORD_REQUIRED",
            "session": "challenge-session",
            "username": "staff@example.com",
            "responses": {
                "NEW_PASSWORD": "NewPassword123!",
            },
        }
    )

    response = app.handler(event, None)

    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": expected_message}
    assert "sensitive AWS message" not in response["body"]
    assert response["headers"]["Cache-Control"] == "no-store"


def test_challenge_maps_transport_errors_to_502(app):
    cognito = Mock()
    cognito.respond_to_auth_challenge.side_effect = EndpointConnectionError(
        endpoint_url="https://cognito-idp.eu-north-1.amazonaws.com",
    )
    app._cognito_client = cognito

    event = make_event(proxy="challenge")
    event["body"] = json.dumps(
        {
            "challengeName": "NEW_PASSWORD_REQUIRED",
            "session": "challenge-session",
            "username": "staff@example.com",
            "responses": {
                "NEW_PASSWORD": "NewPassword123!",
            },
        }
    )

    response = app.handler(event, None)

    assert response["statusCode"] == 502
    assert json.loads(response["body"]) == {
        "error": "authentication service unavailable",
    }
    assert response["headers"]["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "refresh_token",
    [None, "", "   ", 123],
)
def test_refresh_requires_refresh_token(app, refresh_token):
    cognito = Mock()
    app._cognito_client = cognito
    event = make_event(proxy="refresh")
    event["body"] = json.dumps(
        {
            "refreshToken": refresh_token,
            "sub": "staff-user-sub",
        }
    )

    response = app.handler(event, None)

    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {
        "error": "refreshToken is required",
    }
    cognito.get_tokens_from_refresh_token.assert_not_called()


def test_refresh_no_longer_needs_sub(app):
    cognito = Mock()
    cognito.get_tokens_from_refresh_token.return_value = {
        "AuthenticationResult": {"AccessToken": "a", "ExpiresIn": 3600, "TokenType": "Bearer"}
    }
    app._cognito_client = cognito
    event = make_event(proxy="refresh")
    event["body"] = json.dumps({"refreshToken": "existing-refresh-token"})

    response = app.handler(event, None)

    assert response["statusCode"] == 200


def test_refresh_calls_cognito(app):
    cognito = Mock()
    cognito.get_tokens_from_refresh_token.return_value = {
        "AuthenticationResult": {
            "AccessToken": "new-access-token",
            "IdToken": "new-id-token",
            "ExpiresIn": 3600,
            "TokenType": "Bearer",
        }
    }
    app._cognito_client = cognito

    event = make_event(proxy="refresh")
    event["body"] = json.dumps(
        {
            "refreshToken": "existing-refresh-token",
            "sub": "staff-user-sub",
        }
    )

    response = app.handler(event, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body == {
        "status": "authenticated",
        "authenticationResult": {
            "AccessToken": "new-access-token",
            "IdToken": "new-id-token",
            "ExpiresIn": 3600,
            "TokenType": "Bearer",
        },
    }
    assert response["headers"]["Cache-Control"] == "no-store"
    assert "cookies" not in response
    cognito.get_tokens_from_refresh_token.assert_called_once_with(
        RefreshToken="existing-refresh-token",
        ClientId="local-test-client",
        ClientSecret="local-test-secret",
    )


def test_refresh_maps_cognito_errors(app):
    cognito = Mock()
    cognito.get_tokens_from_refresh_token.side_effect = ClientError(
        {
            "Error": {
                "Code": "NotAuthorizedException",
                "Message": "sensitive AWS message",
            }
        },
        "InitiateAuth",
    )
    app._cognito_client = cognito

    event = make_event(proxy="refresh")
    event["body"] = json.dumps(
        {
            "refreshToken": "expired-refresh-token",
            "sub": "staff-user-sub",
        }
    )

    response = app.handler(event, None)

    assert response["statusCode"] == 401
    assert json.loads(response["body"]) == {
        "error": "invalid username or password",
    }
    assert "sensitive AWS message" not in response["body"]
    assert response["headers"]["Cache-Control"] == "no-store"


def test_refresh_maps_transport_errors_to_502(app):
    cognito = Mock()
    cognito.get_tokens_from_refresh_token.side_effect = EndpointConnectionError(
        endpoint_url="https://cognito-idp.eu-north-1.amazonaws.com",
    )
    app._cognito_client = cognito

    event = make_event(proxy="refresh")
    event["body"] = json.dumps(
        {
            "refreshToken": "existing-refresh-token",
            "sub": "staff-user-sub",
        }
    )

    response = app.handler(event, None)

    assert response["statusCode"] == 502
    assert json.loads(response["body"]) == {
        "error": "authentication service unavailable",
    }
    assert response["headers"]["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    ("error_code", "message", "expected"),
    [
        ("UserLambdaValidationException",
         "PreTokenGeneration failed with error refusing tokens.", "account_not_provisioned"),
        ("NotAuthorizedException", "User is disabled.", "account_disabled"),
    ],
)
def test_login_maps_tenant_refusals_to_stable_codes(app, error_code, message, expected):
    """A user without a tenant (pre-token trigger refuses) or of a suspended
    tenant (disabled) gets a 403 code the admin app can show a message for."""
    cognito = Mock()
    cognito.initiate_auth.side_effect = ClientError(
        {"Error": {"Code": error_code, "Message": message}}, "InitiateAuth")
    app._cognito_client = cognito
    event = make_event(proxy="login")
    event["body"] = json.dumps({"username": "staff@example.com", "password": "Correct123!"})

    response = app.handler(event, None)

    assert response["statusCode"] == 403
    assert json.loads(response["body"]) == {"error": expected}


# --- forgot password ----------------------------------------------------------------

SECRET_HASH = "lBMgbQL20sOl5Whi4M9rO7OsXqnaDwwYQlzlEM4j1G4="


def auth_event(proxy, body, ip=None):
    event = make_event(proxy=proxy)
    event["body"] = json.dumps(body)
    if ip:
        event["requestContext"]["http"]["sourceIp"] = ip
    return event


def cognito_error(code, message="sensitive AWS message", operation="ForgotPassword"):
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


class _GuardTable:
    """The request guard's conditional counter, in memory."""

    def __init__(self):
        self.rows = {}

    def update_item(self, Key, ExpressionAttributeValues, **kwargs):
        key = Key["PK"]
        if ":minus" in ExpressionAttributeValues:
            self.rows[key] = self.rows.get(key, 0) - 1
            return
        if self.rows.get(key, 0) >= ExpressionAttributeValues[":max"]:
            raise cognito_error("ConditionalCheckFailedException", operation="UpdateItem")
        self.rows[key] = self.rows.get(key, 0) + 1


@pytest.fixture
def guard(app, monkeypatch):
    from shared import request_guard

    monkeypatch.setenv("REQUEST_GUARD_TABLE_NAME", "guard")
    table = _GuardTable()
    monkeypatch.setattr(request_guard, "table", lambda name: table)
    monkeypatch.setattr(app, "FORGOT_LIMIT", (2, 900))
    monkeypatch.setattr(app, "RESET_LIMIT", (2, 900))
    return table


def test_forgot_password_asks_cognito_for_the_email(app):
    cognito = Mock()
    cognito.forgot_password.return_value = {
        "CodeDeliveryDetails": {"Destination": "s***@e***", "DeliveryMedium": "EMAIL"},
    }
    app._cognito_client = cognito

    response = app.handler(auth_event("forgot-password", {"username": " staff@example.com "}), None)

    assert response["statusCode"] == 202
    assert json.loads(response["body"]) == {"status": "sent"}
    assert response["headers"]["Cache-Control"] == "no-store"
    cognito.forgot_password.assert_called_once_with(
        ClientId="local-test-client",
        SecretHash=SECRET_HASH,
        Username="staff@example.com",
    )


@pytest.mark.parametrize(
    ("body", "expected_error"),
    [
        ({}, "username is required"),
        ({"username": "   "}, "username is required"),
        ({"username": "not-an-email"}, "username must be an email address"),
        ({"username": "a@b"}, "username must be an email address"),
        ({"username": "x" * 250 + "@example.se"}, "username must be an email address"),
    ],
)
def test_forgot_password_needs_an_email_address(app, body, expected_error):
    app._cognito_client = Mock()

    response = app.handler(auth_event("forgot-password", body), None)

    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": expected_error}
    app._cognito_client.forgot_password.assert_not_called()


@pytest.mark.parametrize(
    "error_code",
    [
        "UserNotFoundException",
        "NotAuthorizedException",  # e.g. still on the invitation's temporary password
        "InvalidParameterException",  # no verified email
        "CodeDeliveryFailureException",
    ],
)
def test_forgot_password_answers_the_same_when_no_email_goes_out(app, caplog, error_code):
    cognito = Mock()
    cognito.forgot_password.side_effect = cognito_error(error_code)
    app._cognito_client = cognito

    response = app.handler(auth_event("forgot-password", {"username": "nobody@example.com"}), None)

    assert response["statusCode"] == 202
    assert json.loads(response["body"]) == {"status": "sent"}
    assert error_code in caplog.text
    assert "nobody@example.com" not in caplog.text


@pytest.mark.parametrize("error_code", ["LimitExceededException", "TooManyRequestsException"])
def test_forgot_password_too_often_for_one_account(app, error_code):
    cognito = Mock()
    cognito.forgot_password.side_effect = cognito_error(error_code)
    app._cognito_client = cognito

    response = app.handler(auth_event("forgot-password", {"username": "staff@example.com"}), None)

    assert response["statusCode"] == 429
    assert json.loads(response["body"]) == {"error": "too many attempts"}


@pytest.mark.parametrize(
    "error_code",
    ["InvalidLambdaResponseException", "UnexpectedLambdaException", "InvalidEmailRoleAccessPolicyException"],
)
def test_a_broken_reset_email_is_not_hidden(app, caplog, error_code):
    cognito = Mock()
    cognito.forgot_password.side_effect = cognito_error(error_code)
    app._cognito_client = cognito

    response = app.handler(auth_event("forgot-password", {"username": "staff@example.com"}), None)

    assert response["statusCode"] == 502
    assert json.loads(response["body"]) == {"error": "authentication service unavailable"}
    assert error_code in caplog.text


def test_forgot_password_transport_error_is_502(app):
    cognito = Mock()
    cognito.forgot_password.side_effect = EndpointConnectionError(
        endpoint_url="https://cognito-idp.eu-north-1.amazonaws.com",
    )
    app._cognito_client = cognito

    response = app.handler(auth_event("forgot-password", {"username": "staff@example.com"}), None)

    assert response["statusCode"] == 502


def test_forgot_password_is_limited_per_ip_address(app, guard):
    app._cognito_client = Mock()

    codes = [
        app.handler(auth_event("forgot-password", {"username": f"s{i}@example.com"}, ip="203.0.113.5"), None)
        for i in range(3)
    ]

    assert [r["statusCode"] for r in codes] == [202, 202, 429]
    assert json.loads(codes[2]["body"]) == {"error": "too many attempts"}
    assert 0 < int(codes[2]["headers"]["Retry-After"]) <= 900
    assert app._cognito_client.forgot_password.call_count == 2
    # Another address isn't affected.
    other = app.handler(auth_event("forgot-password", {"username": "s@example.com"}, ip="198.51.100.7"), None)
    assert other["statusCode"] == 202


def test_forgot_password_requires_post(app):
    response = app.handler(make_event(method="GET", proxy="forgot-password"), None)

    assert response["statusCode"] == 405
    assert response["headers"]["Allow"] == "POST"


# --- reset password -------------------------------------------------------------------------

RESET = {"username": "staff@example.com", "code": "123456", "newPassword": "NewPassword123!"}


def test_reset_password_sets_the_new_password(app):
    cognito = Mock()
    cognito.confirm_forgot_password.return_value = {}
    app._cognito_client = cognito

    response = app.handler(auth_event("reset-password", {**RESET, "code": " 123 456 "}), None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"status": "reset"}
    assert response["headers"]["Cache-Control"] == "no-store"
    cognito.confirm_forgot_password.assert_called_once_with(
        ClientId="local-test-client",
        SecretHash=SECRET_HASH,
        Username="staff@example.com",
        ConfirmationCode="123456",
        Password="NewPassword123!",
    )


@pytest.mark.parametrize(
    ("change", "expected_error"),
    [
        ({"username": "nope"}, "username must be an email address"),
        ({"code": None}, "code is required"),
        ({"code": "12345"}, "code must be the 6-digit code from the email"),
        ({"code": "12345a"}, "code must be the 6-digit code from the email"),
        ({"newPassword": ""}, "newPassword is required"),
        ({"newPassword": "A1!" + "a" * 254}, "newPassword must be at most 256 characters"),
    ],
)
def test_reset_password_validates_the_request(app, change, expected_error):
    app._cognito_client = Mock()
    body = {k: v for k, v in {**RESET, **change}.items() if v is not None}

    response = app.handler(auth_event("reset-password", body), None)

    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": expected_error}
    app._cognito_client.confirm_forgot_password.assert_not_called()


@pytest.mark.parametrize(
    ("error_code", "message", "expected_status", "expected_error"),
    [
        ("CodeMismatchException", "x", 400, "invalid code"),
        ("UserNotFoundException", "x", 400, "invalid code"),
        ("ExpiredCodeException", "x", 400, "code expired"),
        ("InvalidPasswordException", "x", 400, "password does not meet requirements"),
        ("PasswordHistoryPolicyViolationException", "x", 400, "password does not meet requirements"),
        ("InvalidParameterException", "x", 400, "invalid reset request"),
        ("LimitExceededException", "x", 429, "too many attempts"),
        ("TooManyFailedAttemptsException", "x", 429, "too many attempts"),
        ("TooManyRequestsException", "x", 429, "too many attempts"),
        ("NotAuthorizedException", "User is disabled.", 403, "account_disabled"),
        ("NotAuthorizedException", "Invalid code provided", 400, "invalid code"),
        ("InternalErrorException", "x", 502, "authentication service unavailable"),
    ],
)
def test_reset_password_maps_cognito_errors(app, error_code, message, expected_status, expected_error):
    cognito = Mock()
    cognito.confirm_forgot_password.side_effect = cognito_error(error_code, message, "ConfirmForgotPassword")
    app._cognito_client = cognito

    response = app.handler(auth_event("reset-password", RESET), None)

    assert response["statusCode"] == expected_status
    assert json.loads(response["body"]) == {"error": expected_error}
    assert response["headers"]["Cache-Control"] == "no-store"


def test_reset_password_is_limited_per_ip_address(app, guard):
    cognito = Mock()
    cognito.confirm_forgot_password.side_effect = cognito_error("CodeMismatchException")
    app._cognito_client = cognito

    codes = [
        app.handler(auth_event("reset-password", RESET, ip="203.0.113.5"), None)["statusCode"]
        for _ in range(3)
    ]

    assert codes == [400, 400, 429]
    assert cognito.confirm_forgot_password.call_count == 2


def test_forgot_and_reset_have_their_own_counters(app, guard):
    app._cognito_client = Mock()
    app._cognito_client.confirm_forgot_password.return_value = {}

    for _ in range(2):
        app.handler(auth_event("forgot-password", {"username": "staff@example.com"}, ip="203.0.113.5"), None)

    assert app.handler(auth_event("reset-password", RESET, ip="203.0.113.5"), None)["statusCode"] == 200

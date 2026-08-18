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
            "username": "staff@example.com",
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
            "USERNAME": "staff@example.com",
            "NEW_PASSWORD": "NewPassword123!",
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
            "username": "staff@example.com",
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
    event["body"] = json.dumps({"refreshToken": refresh_token})

    response = app.handler(event, None)

    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {
        "error": "refreshToken is required",
    }
    cognito.initiate_auth.assert_not_called()


def test_refresh_calls_cognito(app):
    cognito = Mock()
    cognito.initiate_auth.return_value = {
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
    cognito.initiate_auth.assert_called_once_with(
        AuthFlow="REFRESH_TOKEN_AUTH",
        ClientId="local-test-client",
        AuthParameters={
            "REFRESH_TOKEN": "existing-refresh-token",
        },
    )


def test_refresh_maps_cognito_errors(app):
    cognito = Mock()
    cognito.initiate_auth.side_effect = ClientError(
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
    cognito.initiate_auth.side_effect = EndpointConnectionError(
        endpoint_url="https://cognito-idp.eu-north-1.amazonaws.com",
    )
    app._cognito_client = cognito

    event = make_event(proxy="refresh")
    event["body"] = json.dumps(
        {
            "refreshToken": "existing-refresh-token",
        }
    )

    response = app.handler(event, None)

    assert response["statusCode"] == 502
    assert json.loads(response["body"]) == {
        "error": "authentication service unavailable",
    }
    assert response["headers"]["Cache-Control"] == "no-store"

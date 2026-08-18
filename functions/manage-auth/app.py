"""manage-auth

TRIGGER:
    API Gateway -- ANY /auth/{proxy+} -- Auth: NONE

PURPOSE:
    Handles the staff login flow itself (sign-in, challenge responses, token
    refresh). Necessarily NONE-auth - you can't require a valid JWT to obtain
    one. Only staff/owner/super_user accounts exist in Cognito; customers
    never authenticate.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    COGNITO_USER_POOL_ID -- Target user pool for auth calls
    COGNITO_CLIENT_ID -- App client ID for InitiateAuth/RespondToAuthChallenge

AWS RESOURCE ACCESS:
    Cognito InitiateAuth and RespondToAuthChallenge only, scoped to the user
    pool. No DynamoDB access.

Full details: docs/LAMBDA_REFERENCE.md
"""

import base64
import binascii
import json
import os
from http import HTTPStatus

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from shared.responses import error_response, json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
COGNITO_USER_POOL_ID = os.environ["COGNITO_USER_POOL_ID"]
COGNITO_CLIENT_ID = os.environ["COGNITO_CLIENT_ID"]

_COGNITO_ERROR_MAPPING = {
    "NotAuthorizedException": (
        HTTPStatus.UNAUTHORIZED.value,
        "invalid username or password",
    ),
    "UserNotFoundException": (
        HTTPStatus.UNAUTHORIZED.value,
        "invalid username or password",
    ),
    "UserNotConfirmedException": (
        HTTPStatus.FORBIDDEN.value,
        "user is not confirmed",
    ),
    "PasswordResetRequiredException": (
        HTTPStatus.FORBIDDEN.value,
        "password reset required",
    ),
    "TooManyRequestsException": (
        HTTPStatus.TOO_MANY_REQUESTS.value,
        "too many authentication attempts",
    ),
    "InvalidParameterException": (
        HTTPStatus.BAD_REQUEST.value,
        "invalid authentication request",
    ),
    "CodeMismatchException": (
        HTTPStatus.BAD_REQUEST.value,
        "invalid challenge response",
    ),
    "ExpiredCodeException": (
        HTTPStatus.BAD_REQUEST.value,
        "challenge has expired",
    ),
    "InvalidPasswordException": (
        HTTPStatus.BAD_REQUEST.value,
        "password does not meet requirements",
    ),
    "PasswordHistoryPolicyViolationException": (
        HTTPStatus.BAD_REQUEST.value,
        "password does not meet requirements",
    ),
}

_DEFAULT_COGNITO_ERROR = (
    HTTPStatus.BAD_GATEWAY.value,
    "authentication service unavailable",
)


def _parse_json_body(event):
    raw_body = event.get("body")

    if not isinstance(raw_body, str) or not raw_body.strip():
        raise ValueError("request body is required")

    if event.get("isBase64Encoded") is True:
        try:
            raw_body = base64.b64decode(
                raw_body,
                validate=True,
            ).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            raise ValueError("request body must be valid base64") from None

    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        raise ValueError("request body must be valid JSON") from None

    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")

    return body


def _required_string(body, field):
    value = body.get(field)

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")

    return value


def _required_string_map(body, field):
    value = body.get(field)

    if not isinstance(value, dict) or not value:
        raise ValueError(f"{field} is required")

    if any(
        not isinstance(key, str)
        or not key.strip()
        or not isinstance(item, str)
        or not item.strip()
        for key, item in value.items()
    ):
        raise ValueError(f"{field} must contain non-empty string keys and values")

    if {"USERNAME", "SECRET_HASH"} & value.keys():
        raise ValueError(f"{field} must not include USERNAME or SECRET_HASH")

    return value


def _auth_error_response(status_code, message):
    return json_response(
        status_code,
        {"error": message},
        headers={"Cache-Control": "no-store"},
    )


def _cognito_error_response(exc):
    error_code = exc.response.get("Error", {}).get("Code")

    status_code, message = _COGNITO_ERROR_MAPPING.get(
        error_code,
        _DEFAULT_COGNITO_ERROR,
    )

    return _auth_error_response(status_code, message)


def _cognito_auth_response(response):
    if "AuthenticationResult" in response:
        return json_response(
            HTTPStatus.OK.value,
            {
                "status": "authenticated",
                "authenticationResult": response["AuthenticationResult"],
            },
            headers={"Cache-Control": "no-store"},
        )

    if "ChallengeName" in response:
        return json_response(
            HTTPStatus.OK.value,
            {
                "status": "challenge",
                "challengeName": response["ChallengeName"],
                "challengeParameters": response.get(
                    "ChallengeParameters",
                    {},
                ),
                "session": response.get("Session"),
            },
            headers={"Cache-Control": "no-store"},
        )

    return _auth_error_response(
        HTTPStatus.BAD_GATEWAY.value,
        "invalid response from authentication service",
    )


def handler(event, context):
    http = (event.get("requestContext") or {}).get("http") or {}
    method = http.get("method", "").upper()

    path_parameters = event.get("pathParameters") or {}
    proxy_path = path_parameters.get("proxy", "").strip("/")

    if proxy_path not in {"login", "challenge", "refresh"}:
        return error_response(404, "not found")

    if method != "POST":
        return json_response(
            405,
            {"error": "method not allowed"},
            headers={"Allow": "POST"},
        )

    try:
        body = _parse_json_body(event)

        if proxy_path == "login":
            return handle_login(body)

        if proxy_path == "challenge":
            return handle_challenge(body)

        return handle_refresh(body)

    except ValueError as exc:
        return error_response(400, str(exc))


def handle_challenge(body):
    challenge_name = _required_string(body, "challengeName")
    session = _required_string(body, "session")
    username = _required_string(body, "username")
    responses = _required_string_map(body, "responses")

    try:
        response = _get_cognito_client().respond_to_auth_challenge(
            ClientId=COGNITO_CLIENT_ID,
            ChallengeName=challenge_name,
            Session=session,
            ChallengeResponses={
                "USERNAME": username,
                **responses,
            },
        )
    except ClientError as exc:
        return _cognito_error_response(exc)
    except BotoCoreError:
        return _auth_error_response(
            HTTPStatus.BAD_GATEWAY.value,
            "authentication service unavailable",
        )

    return _cognito_auth_response(response)


def handle_refresh(body):
    refresh_token = _required_string(body, "refreshToken")

    try:
        response = _get_cognito_client().initiate_auth(
            AuthFlow="REFRESH_TOKEN_AUTH",
            ClientId=COGNITO_CLIENT_ID,
            AuthParameters={
                "REFRESH_TOKEN": refresh_token,
            },
        )
    except ClientError as exc:
        return _cognito_error_response(exc)
    except BotoCoreError:
        return _auth_error_response(
            HTTPStatus.BAD_GATEWAY.value,
            "authentication service unavailable",
        )

    return _cognito_auth_response(response)

_cognito_client = None


def _get_cognito_client():
    global _cognito_client

    if _cognito_client is None:
        _cognito_client = boto3.client("cognito-idp")

    return _cognito_client


def handle_login(body):
    username = _required_string(body, "username")
    password = _required_string(body, "password")

    try:
        response = _get_cognito_client().initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            ClientId=COGNITO_CLIENT_ID,
            AuthParameters={
                "USERNAME": username,
                "PASSWORD": password,
            },
        )
    except ClientError as exc:
        return _cognito_error_response(exc)
    except BotoCoreError:
        return _auth_error_response(
            HTTPStatus.BAD_GATEWAY.value,
            "authentication service unavailable",
        )

    return _cognito_auth_response(response)

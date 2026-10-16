"""manage-auth

TRIGGER:
    API Gateway -- ANY /auth/{proxy+} -- Auth: NONE

PURPOSE:
    Handles the staff login flow itself (sign-in, challenge responses, token
    refresh) and "forgot password". Necessarily NONE-auth - you can't
    require a valid JWT to obtain one. Only staff/owner accounts exist in
    Cognito; customers never authenticate.

    Forgot password (Cognito ForgotPassword / ConfirmForgotPassword):
      POST /auth/forgot-password {username}       -> 202, always the same
          answer whether or not the address has an account (no account
          enumeration). Cognito emails a 6-digit code; with the pool on SES
          the email is a link to the admin app's /aterstall-losenord page
          carrying the code (infrastructure: the pool's custom message
          trigger), otherwise it shows the code.
      POST /auth/reset-password {username, code, newPassword} -> 200
    Both are limited per IP address (shared/request_guard.py, 429) on top of
    Cognito's own per-user limits.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    COGNITO_USER_POOL_ID -- Target user pool for auth calls
    COGNITO_CLIENT_ID -- App client ID for InitiateAuth/RespondToAuthChallenge
    COGNITO_CLIENT_SECRET -- App client secret used to calculate SECRET_HASH
    REQUEST_GUARD_TABLE_NAME -- attempt counters (optional; unset = no
                                per-IP limits)

AWS RESOURCE ACCESS:
    Cognito InitiateAuth, RespondToAuthChallenge, ForgotPassword and
    ConfirmForgotPassword, scoped to the user pool. DynamoDB UpdateItem on
    the request-guard table only.

Full details: docs/LAMBDA_REFERENCE.md
"""

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
from datetime import datetime, timezone
from http import HTTPStatus

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from shared import request_guard
from shared.http import source_ip
from shared.responses import error_response, json_response

logger = logging.getLogger(__name__)

ENVIRONMENT = os.environ["ENVIRONMENT"]
COGNITO_USER_POOL_ID = os.environ["COGNITO_USER_POOL_ID"]
COGNITO_CLIENT_ID = os.environ["COGNITO_CLIENT_ID"]
COGNITO_CLIENT_SECRET = os.environ["COGNITO_CLIENT_SECRET"]

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

# Forgot password: (max attempts, window seconds) per IP address.
FORGOT_LIMIT = (5, 15 * 60)
RESET_LIMIT = (10, 15 * 60)
_EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_EMAIL_MAX = 254
_CODE_PATTERN = re.compile(r"[0-9]{6}")
_PASSWORD_MAX = 256

# ForgotPassword errors that mean "no email goes out for this address" - no
# account, a disabled account, an account that has never signed in (still on
# its invitation), no verified email, or the email couldn't be delivered.
# The caller gets the same answer as when the email went out, so the form
# can't be used to find out which addresses have accounts.
_FORGOT_NOT_SENT = frozenset({
    "UserNotFoundException",
    "NotAuthorizedException",
    "InvalidParameterException",
    "CodeDeliveryFailureException",
})

_RESET_ERROR_MAPPING = {
    "CodeMismatchException": (HTTPStatus.BAD_REQUEST.value, "invalid code"),
    "UserNotFoundException": (HTTPStatus.BAD_REQUEST.value, "invalid code"),
    "ExpiredCodeException": (HTTPStatus.BAD_REQUEST.value, "code expired"),
    "InvalidPasswordException": (
        HTTPStatus.BAD_REQUEST.value,
        "password does not meet requirements",
    ),
    "PasswordHistoryPolicyViolationException": (
        HTTPStatus.BAD_REQUEST.value,
        "password does not meet requirements",
    ),
    "InvalidParameterException": (
        HTTPStatus.BAD_REQUEST.value,
        "invalid reset request",
    ),
    "LimitExceededException": (
        HTTPStatus.TOO_MANY_REQUESTS.value,
        "too many attempts",
    ),
    "TooManyFailedAttemptsException": (
        HTTPStatus.TOO_MANY_REQUESTS.value,
        "too many attempts",
    ),
    "TooManyRequestsException": (
        HTTPStatus.TOO_MANY_REQUESTS.value,
        "too many attempts",
    ),
}


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


def _secret_hash(username):
    digest = hmac.new(
        COGNITO_CLIENT_SECRET.encode("utf-8"),
        f"{username}{COGNITO_CLIENT_ID}".encode("utf-8"),
        hashlib.sha256,
    ).digest()

    return base64.b64encode(digest).decode("ascii")


def _auth_error_response(status_code, message, headers=None):
    return json_response(
        status_code,
        {"error": message},
        headers={"Cache-Control": "no-store", **(headers or {})},
    )


def _cognito_error_response(exc):
    error = exc.response.get("Error", {})
    error_code = error.get("Code")
    message = str(error.get("Message") or "")

    # Multi-tenant sign-in refusals (stable codes the admin app keys off):
    # - the pre-token-generation trigger refuses users without a tenant or
    #   tenant role -> Cognito answers UserLambdaValidationException;
    # - users of a suspended/offboarded tenant are disabled in Cognito.
    if error_code == "UserLambdaValidationException":
        return _auth_error_response(
            HTTPStatus.FORBIDDEN.value,
            "account_not_provisioned",
        )
    if error_code == "NotAuthorizedException" and "disabled" in message.lower():
        return _auth_error_response(
            HTTPStatus.FORBIDDEN.value,
            "account_disabled",
        )

    status_code, message = _COGNITO_ERROR_MAPPING.get(
        error_code,
        _DEFAULT_COGNITO_ERROR,
    )

    return _auth_error_response(status_code, message)


def _cognito_auth_response(response, challenge_username=None):
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
        challenge_parameters = response.get("ChallengeParameters", {})
        challenge_username = (
            challenge_parameters.get("USER_ID_FOR_SRP")
            or challenge_username
        )

        response_body = {
            "status": "challenge",
            "challengeName": response["ChallengeName"],
            "challengeParameters": challenge_parameters,
            "session": response.get("Session"),
        }

        if challenge_username:
            response_body["challengeUsername"] = challenge_username

        return json_response(
            HTTPStatus.OK.value,
            response_body,
            headers={"Cache-Control": "no-store"},
        )

    return _auth_error_response(
        HTTPStatus.BAD_GATEWAY.value,
        "invalid response from authentication service",
    )


_ROUTES = frozenset({"login", "challenge", "refresh", "forgot-password", "reset-password"})


def handler(event, context):
    http = (event.get("requestContext") or {}).get("http") or {}
    method = http.get("method", "").upper()

    path_parameters = event.get("pathParameters") or {}
    proxy_path = path_parameters.get("proxy", "").strip("/")

    if proxy_path not in _ROUTES:
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

        if proxy_path == "forgot-password":
            return handle_forgot_password(body, source_ip(event))

        if proxy_path == "reset-password":
            return handle_reset_password(body, source_ip(event))

        return handle_refresh(body)

    except ValueError as exc:
        return error_response(400, str(exc))
    except request_guard.Limited as exc:
        return _auth_error_response(
            HTTPStatus.TOO_MANY_REQUESTS.value,
            "too many attempts",
            {"Retry-After": str(exc.retry_after)},
        )


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
                **responses,
                "USERNAME": username,
                "SECRET_HASH": _secret_hash(username),
            },
        )
    except ClientError as exc:
        return _cognito_error_response(exc)
    except BotoCoreError:
        return _auth_error_response(
            HTTPStatus.BAD_GATEWAY.value,
            "authentication service unavailable",
        )

    return _cognito_auth_response(response, username)


def handle_refresh(body):
    refresh_token = _required_string(body, "refreshToken")
    user_sub = _required_string(body, "sub")

    try:
        response = _get_cognito_client().initiate_auth(
            AuthFlow="REFRESH_TOKEN_AUTH",
            ClientId=COGNITO_CLIENT_ID,
            AuthParameters={
                "REFRESH_TOKEN": refresh_token,
                "SECRET_HASH": _secret_hash(user_sub),
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
                "SECRET_HASH": _secret_hash(username),
            },
        )
    except ClientError as exc:
        return _cognito_error_response(exc)
    except BotoCoreError:
        return _auth_error_response(
            HTTPStatus.BAD_GATEWAY.value,
            "authentication service unavailable",
        )

    return _cognito_auth_response(response, username)


def _email(body, field):
    value = _required_string(body, field).strip()
    if len(value) > _EMAIL_MAX or not _EMAIL_PATTERN.fullmatch(value):
        raise ValueError(f"{field} must be an email address")
    return value


def _now():
    return datetime.now(timezone.utc)


def _error_code(exc):
    return exc.response.get("Error", {}).get("Code")


def handle_forgot_password(body, ip):
    """Asks Cognito to email a reset code (or link) to the account."""
    username = _email(body, "username")
    request_guard.check([("auth-forgot-ip", ip, *FORGOT_LIMIT)], _now())

    try:
        _get_cognito_client().forgot_password(
            ClientId=COGNITO_CLIENT_ID,
            SecretHash=_secret_hash(username),
            Username=username,
        )
    except ClientError as exc:
        error_code = _error_code(exc)
        if error_code in {"LimitExceededException", "TooManyRequestsException"}:
            return _auth_error_response(
                HTTPStatus.TOO_MANY_REQUESTS.value,
                "too many attempts",
            )
        if error_code not in _FORGOT_NOT_SENT:
            # A broken reset email (trigger, SES setup) - never hidden.
            logger.error(json.dumps({"forgotPassword": "failed", "code": error_code}))
            return _auth_error_response(*_DEFAULT_COGNITO_ERROR)
        # The address itself is never logged.
        log = logger.error if error_code == "CodeDeliveryFailureException" else logger.warning
        log(json.dumps({"forgotPassword": "not_sent", "code": error_code}))
    except BotoCoreError:
        return _auth_error_response(*_DEFAULT_COGNITO_ERROR)

    return json_response(
        HTTPStatus.ACCEPTED.value,
        {"status": "sent"},
        headers={"Cache-Control": "no-store"},
    )


def handle_reset_password(body, ip):
    """Sets the new password with the code from the reset email."""
    username = _email(body, "username")
    code = re.sub(r"\s+", "", _required_string(body, "code"))
    if not _CODE_PATTERN.fullmatch(code):
        raise ValueError("code must be the 6-digit code from the email")
    password = _required_string(body, "newPassword")
    if len(password) > _PASSWORD_MAX:
        raise ValueError("newPassword must be at most 256 characters")
    request_guard.check([("auth-reset-ip", ip, *RESET_LIMIT)], _now())

    try:
        _get_cognito_client().confirm_forgot_password(
            ClientId=COGNITO_CLIENT_ID,
            SecretHash=_secret_hash(username),
            Username=username,
            ConfirmationCode=code,
            Password=password,
        )
    except ClientError as exc:
        error_code = _error_code(exc)
        message = str(exc.response.get("Error", {}).get("Message") or "")
        if error_code == "NotAuthorizedException":
            if "disabled" in message.lower():
                return _auth_error_response(HTTPStatus.FORBIDDEN.value, "account_disabled")
            return _auth_error_response(HTTPStatus.BAD_REQUEST.value, "invalid code")
        if error_code not in _RESET_ERROR_MAPPING:
            logger.error(json.dumps({"resetPassword": "failed", "code": error_code}))
        return _auth_error_response(
            *_RESET_ERROR_MAPPING.get(error_code, _DEFAULT_COGNITO_ERROR)
        )
    except BotoCoreError:
        return _auth_error_response(*_DEFAULT_COGNITO_ERROR)

    return json_response(
        HTTPStatus.OK.value,
        {"status": "reset"},
        headers={"Cache-Control": "no-store"},
    )

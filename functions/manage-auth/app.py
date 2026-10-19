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

    Cookie sessions (the admin app, served with the API under /api on the
    same address):
      A request with the header "X-Booqy-Session: cookie" gets the refresh
      token in an HttpOnly cookie instead of the JSON body, so no script on
      the page can ever read it. The access and ID tokens still come back in
      the body and are kept in memory only.
      POST /auth/refresh {}  -> reads the cookie, rotates it
      POST /auth/logout  {}  -> revokes the refresh token, clears the cookie
      CSRF: the cookie is SameSite=Strict and host-only, both routes require
      the custom header (a cross-site form can't send it, and the API's CORS
      setup doesn't allow it cross-origin), and the Origin must be the host
      the browser used (from the admin app's CloudFront) or one of
      COOKIE_ALLOWED_ORIGINS.
      Requests without the header keep the old body-based flow
      ({refreshToken, sub}) so an older admin build keeps working during a
      rollout.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    COGNITO_USER_POOL_ID -- Target user pool for auth calls
    COGNITO_CLIENT_ID -- App client ID for InitiateAuth/RespondToAuthChallenge
    COGNITO_CLIENT_SECRET -- App client secret used to calculate SECRET_HASH
    REQUEST_GUARD_TABLE_NAME -- attempt counters (optional; unset = no
                                per-IP limits)
    AUTH_COOKIE_PATH -- browser path of the auth routes (default /api/auth)
    REFRESH_COOKIE_MAX_AGE -- seconds; match the refresh token validity
                              (default 43200 = 12 h)
    COOKIE_ALLOWED_ORIGINS -- extra origins allowed to use the cookie
                              routes, comma-separated (optional; local dev)
    CLOUDFRONT_EDGE_SECRET -- trusts the viewer IP and host passed by the
                              admin app's CloudFront (shared/http.py)

AWS RESOURCE ACCESS:
    Cognito InitiateAuth, RespondToAuthChallenge, GetTokensFromRefreshToken,
    RevokeToken, ForgotPassword and ConfirmForgotPassword, scoped to the user
    pool. DynamoDB UpdateItem on
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
from shared.http import edge_header, header, source_ip
from shared.responses import error_response, json_response

logger = logging.getLogger(__name__)

ENVIRONMENT = os.environ["ENVIRONMENT"]
COGNITO_USER_POOL_ID = os.environ["COGNITO_USER_POOL_ID"]
COGNITO_CLIENT_ID = os.environ["COGNITO_CLIENT_ID"]
COGNITO_CLIENT_SECRET = os.environ["COGNITO_CLIENT_SECRET"]

# The refresh token cookie. __Secure- (not __Host-) because __Host- forces
# Path=/, which would send the refresh token with every page, script and
# image request. Host-only (no Domain) + Path=/api/auth gives the same lock
# to this one host while only the auth routes ever receive it.
REFRESH_COOKIE = "__Secure-booqy_rt"
AUTH_COOKIE_PATH = os.environ.get("AUTH_COOKIE_PATH") or "/api/auth"
REFRESH_COOKIE_MAX_AGE = int(os.environ.get("REFRESH_COOKIE_MAX_AGE") or 12 * 60 * 60)
COOKIE_ALLOWED_ORIGINS = frozenset(
    o.strip().rstrip("/")
    for o in (os.environ.get("COOKIE_ALLOWED_ORIGINS") or "").split(",")
    if o.strip()
)
SESSION_HEADER = "x-booqy-session"
_COOKIE_VALUE = re.compile(r"[A-Za-z0-9._~+/=-]+")

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
    # A rotated-away refresh token used again after the grace period.
    "RefreshTokenReuseException": (
        HTTPStatus.UNAUTHORIZED.value,
        "invalid username or password",
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


def _cookie_mode(event):
    return (header(event, SESSION_HEADER) or "").strip().lower() == "cookie"


def _refresh_cookie(value, max_age):
    return (
        f"{REFRESH_COOKIE}={value}; Path={AUTH_COOKIE_PATH}; Max-Age={max_age}; "
        "Secure; HttpOnly; SameSite=Strict"
    )


_AMBIGUOUS = object()


def _read_refresh_cookie(event):
    """The refresh token from the request's cookies (HTTP API v2 `cookies`).

    Returns None if absent and _AMBIGUOUS if the name appears more than once:
    a second copy can only come from a sibling subdomain planting one with a
    wider Domain (cookie tossing), so neither copy is trusted.
    """
    cookies = event.get("cookies") if isinstance(event, dict) else None
    if not isinstance(cookies, list):
        raw = header(event, "cookie") or ""
        cookies = raw.split(";")
    found = []
    for item in cookies:
        if not isinstance(item, str) or "=" not in item:
            continue
        name, value = item.strip().split("=", 1)
        if name == REFRESH_COOKIE:
            found.append(value)
    if not found:
        return None
    if len(found) > 1 or not _COOKIE_VALUE.fullmatch(found[0]):
        return _AMBIGUOUS
    return found[0]


def _origin_allowed(event):
    """The page calling us must be the admin app itself.

    Through the admin app's CloudFront the edge tells us which host the
    browser used (trusted via the edge secret), and the Origin must be that
    same host: works on restaurant.<domain> and on the *.cloudfront.net
    address alike. COOKIE_ALLOWED_ORIGINS adds extra origins (local dev).
    """
    origin = (header(event, "origin") or "").strip().rstrip("/")
    if not origin:
        return False
    if origin in COOKIE_ALLOWED_ORIGINS:
        return True
    viewer_host = edge_header(event, "x-booqy-viewer-host")
    return bool(viewer_host) and origin == f"https://{viewer_host.lower()}"


def _check_cookie_request(event):
    """CSRF guard for the routes that act on the cookie alone."""
    if not _cookie_mode(event):
        return _auth_error_response(HTTPStatus.FORBIDDEN.value, "missing session header")
    if not _origin_allowed(event):
        return _auth_error_response(HTTPStatus.FORBIDDEN.value, "origin not allowed")
    return None


def _with_cookies(response, cookies):
    if cookies:
        response["cookies"] = list(cookies)
    return response


def _cognito_auth_response(response, challenge_username=None, cookie_mode=False):
    if "AuthenticationResult" in response:
        result = dict(response["AuthenticationResult"])
        cookies = []
        if cookie_mode:
            refresh_token = result.pop("RefreshToken", None)
            if refresh_token:
                cookies.append(_refresh_cookie(refresh_token, REFRESH_COOKIE_MAX_AGE))
        return _with_cookies(
            json_response(
                HTTPStatus.OK.value,
                {
                    "status": "authenticated",
                    "authenticationResult": result,
                },
                headers={"Cache-Control": "no-store"},
            ),
            cookies,
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


_ROUTES = frozenset(
    {"login", "challenge", "refresh", "logout", "forgot-password", "reset-password"}
)


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

        cookie_mode = _cookie_mode(event)

        if proxy_path == "login":
            return handle_login(body, cookie_mode)

        if proxy_path == "challenge":
            return handle_challenge(body, cookie_mode)

        if proxy_path == "logout":
            return handle_logout(event)

        if proxy_path == "forgot-password":
            return handle_forgot_password(body, source_ip(event))

        if proxy_path == "reset-password":
            return handle_reset_password(body, source_ip(event))

        return handle_refresh(body, event)

    except ValueError as exc:
        return error_response(400, str(exc))
    except request_guard.Limited as exc:
        return _auth_error_response(
            HTTPStatus.TOO_MANY_REQUESTS.value,
            "too many attempts",
            {"Retry-After": str(exc.retry_after)},
        )


def handle_challenge(body, cookie_mode=False):
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

    return _cognito_auth_response(response, username, cookie_mode)


def handle_refresh(body, event=None):
    """New tokens from a refresh token: the cookie (admin) or the body (legacy).

    GetTokensFromRefreshToken rather than InitiateAuth REFRESH_TOKEN_AUTH:
    it is the call that works with refresh token rotation (a new refresh
    token on every use, the old one stops working), and it takes the client
    secret directly, so the user's sub is no longer needed.
    """
    cookie_mode = _cookie_mode(event or {})
    if cookie_mode:
        refused = _check_cookie_request(event)
        if refused:
            return refused
        refresh_token = _read_refresh_cookie(event)
        if refresh_token is _AMBIGUOUS:
            return _with_cookies(
                _auth_error_response(HTTPStatus.UNAUTHORIZED.value, "not signed in"),
                [_refresh_cookie("", 0)],
            )
        if not refresh_token:
            return _auth_error_response(HTTPStatus.UNAUTHORIZED.value, "not signed in")
    else:
        refresh_token = _required_string(body, "refreshToken")

    try:
        response = _get_cognito_client().get_tokens_from_refresh_token(
            RefreshToken=refresh_token,
            ClientId=COGNITO_CLIENT_ID,
            ClientSecret=COGNITO_CLIENT_SECRET,
        )
    except ClientError as exc:
        failed = _cognito_error_response(exc)
        if cookie_mode and failed["statusCode"] == HTTPStatus.UNAUTHORIZED.value:
            # Expired or revoked: drop the dead cookie so the app shows login.
            return _with_cookies(failed, [_refresh_cookie("", 0)])
        return failed
    except BotoCoreError:
        return _auth_error_response(
            HTTPStatus.BAD_GATEWAY.value,
            "authentication service unavailable",
        )

    return _cognito_auth_response(response, cookie_mode=cookie_mode)


def handle_logout(event):
    """Revokes the cookie's refresh token and clears the cookie."""
    refused = _check_cookie_request(event)
    if refused:
        return refused
    refresh_token = _read_refresh_cookie(event)
    if refresh_token and refresh_token is not _AMBIGUOUS:
        try:
            _get_cognito_client().revoke_token(
                Token=refresh_token,
                ClientId=COGNITO_CLIENT_ID,
                ClientSecret=COGNITO_CLIENT_SECRET,
            )
        except (ClientError, BotoCoreError) as exc:
            # The cookie is cleared regardless; an unrevoked token still
            # expires on its own (REFRESH_COOKIE_MAX_AGE).
            code = _error_code(exc) if isinstance(exc, ClientError) else "transport"
            logger.warning(json.dumps({"logout": "revoke_failed", "code": code}))
    return _with_cookies(
        json_response(
            HTTPStatus.OK.value,
            {"status": "signed_out"},
            headers={"Cache-Control": "no-store"},
        ),
        [_refresh_cookie("", 0)],
    )

_cognito_client = None


def _get_cognito_client():
    global _cognito_client

    if _cognito_client is None:
        _cognito_client = boto3.client("cognito-idp")

    return _cognito_client


def handle_login(body, cookie_mode=False):
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

    return _cognito_auth_response(response, username, cookie_mode)


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

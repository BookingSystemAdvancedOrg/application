"""The real Google Business Profile client behind shared/google_business.py
(feature D, ADR-D). Plain urllib, no Google SDK.

Google APIs used (checked against developers.google.com, 2026-11):

    OAuth 2.0 web server flow
        https://developers.google.com/identity/protocols/oauth2/web-server
        GET  https://accounts.google.com/o/oauth2/v2/auth   (access_type=offline, prompt=consent)
        POST https://oauth2.googleapis.com/token            (authorization_code, refresh_token)
        POST https://oauth2.googleapis.com/revoke           (token=<refresh token>)
    Account Management API v1, accounts.list
        https://developers.google.com/my-business/reference/accountmanagement/rest/v1/accounts/list
        GET  https://mybusinessaccountmanagement.googleapis.com/v1/accounts   (pageSize max 20)
    Business Information API v1, accounts.locations.list and locations.patch
        https://developers.google.com/my-business/reference/businessinformation/rest/v1/accounts.locations/list
        https://developers.google.com/my-business/reference/businessinformation/rest/v1/locations/patch
        GET   .../v1/{accounts/*}/locations?readMask=...    (pageSize max 100)
        PATCH .../v1/{locations/*}?updateMask=regularHours,specialHours

Scope: https://www.googleapis.com/auth/business.manage. The OAuth client
comes from Secrets Manager GOOGLE_BUSINESS_APP_SECRET_ARN, JSON
{clientId, clientSecret}, read once per container. Access tokens are kept
in memory only (keyed by a hash of the refresh token) until shortly before
they expire. Tokens, codes and the client secret are never logged: a
failed call logs the operation, the HTTP status and Google's status/reason.

Errors (GoogleBusinessError.code, see shared/google_business.py):
    invalid_code       the authorization code was refused (exchange_code)
    needs_reauth       invalid_grant on refresh, 401, missing scope, no refresh token
    permission_denied  403 on the location or account
    api_disabled       403 SERVICE_DISABLED / ACCESS_NOT_CONFIGURED (project setup)
    quota_exceeded     429/403 with no quota left (limit 0 = no API access yet, or per day)
    rate_limited       429 after retries (retryable)
    not_found          404
    invalid_request    other 400
    conflict           409 (retryable)
    unavailable        5xx, timeout or network after retries (retryable)
    misconfigured      the OAuth client or secret is wrong (invalid_client,
                       unauthorized_client, redirect_uri_mismatch)
"""

import hashlib
import http.client
import json
import logging
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

import boto3

from shared import google_business as port
from shared.google_business import GoogleBusiness, GoogleBusinessError, Token

logger = logging.getLogger()

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
ACCOUNTS_URL = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
INFO_BASE = "https://mybusinessbusinessinformation.googleapis.com/v1"
SCOPE = "https://www.googleapis.com/auth/business.manage"
READ_MASK = "name,title,storefrontAddress,metadata"
UPDATE_MASK = "regularHours,specialHours"

TIMEOUT_SECONDS = 8
ATTEMPTS = 3
BUDGET_SECONDS = 18  # all retries of one call; the run's deadline (port.start_run) caps every call too
MIN_ATTEMPT_SECONDS = 1.5  # less time than this left: no new attempt
MAX_RETRY_AFTER = 4
MAX_PAGES = 25
TOKEN_EARLY_SECONDS = 120

_LOCATION = re.compile(r"locations/[A-Za-z0-9_-]{1,64}")
_ACCOUNT = re.compile(r"accounts/[A-Za-z0-9_-]{1,64}")
_RETRY_STATUS = (429, 500, 502, 503, 504)

_APP = {}
_ACCESS = {}
_sleep = time.sleep
_clock = time.monotonic


class _HttpFailure(Exception):
    def __init__(self, status, payload, retry_after=None):
        super().__init__(status)
        self.status = status
        self.payload = payload if isinstance(payload, dict) else {}
        self.retry_after = retry_after


def reset_cache():
    """Tests: forget the OAuth client and the cached access tokens."""
    _APP.clear()
    _ACCESS.clear()


def _app():
    if "app" not in _APP:
        arn = os.environ.get("GOOGLE_BUSINESS_APP_SECRET_ARN")
        if not arn:
            raise GoogleBusinessError("misconfigured")
        try:
            raw = boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"]
            value = json.loads(raw)
        except Exception as exc:  # never echo the value: only the type
            logger.error(json.dumps({"google": "app_secret", "error": type(exc).__name__}))
            raise GoogleBusinessError("misconfigured", True) from None
        if not isinstance(value, dict) or not all(isinstance(value.get(k), str) and value[k]
                                                  for k in ("clientId", "clientSecret")):
            logger.error(json.dumps({"google": "app_secret", "error": "missing clientId or clientSecret"}))
            raise GoogleBusinessError("misconfigured")
        _APP["app"] = {"clientId": value["clientId"], "clientSecret": value["clientSecret"]}
    return _APP["app"]


def _retry_after(headers):
    try:
        return max(0.0, float((headers or {}).get("Retry-After") or ""))
    except (TypeError, ValueError):
        return None


def _once(method, url, data, headers, timeout=TIMEOUT_SECONDS):
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            payload = {}
        raise _HttpFailure(exc.code, payload, _retry_after(exc.headers)) from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        # HTTPException (IncompleteRead, BadStatusLine, RemoteDisconnected) is not an OSError
        raise _HttpFailure(0, {}) from None
    if not body:
        return {}
    try:
        return json.loads(body)
    except ValueError:
        raise _HttpFailure(502, {}) from None


def _quota_gone(failure):
    """No quota left at all (limit 0 = no API access yet, or a per-day limit)."""
    error = failure.payload.get("error")
    details = error.get("details") or [] if isinstance(error, dict) else []
    for detail in details:
        meta = detail.get("metadata") if isinstance(detail, dict) else None
        if isinstance(meta, dict) and (str(meta.get("quota_limit_value")) == "0"
                                       or "perday" in str(meta.get("quota_limit") or "").lower()):
            return True
    return False


def _limit(started):
    """When this call has to be done: its own budget or the run's deadline."""
    deadline = port.run_deadline()
    return started + BUDGET_SECONDS if deadline is None else min(started + BUDGET_SECONDS, deadline)


def _send(op, method, url, *, form=None, body=None, token=None, retry=True):
    """One Google call with timeouts and bounded retries on 429/5xx/network,
    inside BUDGET_SECONDS and the run's deadline (each attempt's timeout is
    capped at the time left). retry=False: exactly one attempt (a one-time
    code may already be used when Google answers 5xx). Raises _HttpFailure
    with the last answer."""
    headers = {"Accept": "application/json"}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    limit = _limit(_clock())
    for attempt in range(ATTEMPTS if retry else 1):
        left = limit - _clock()
        if left < MIN_ATTEMPT_SECONDS:
            logger.warning(json.dumps({"google": op, "status": 0, "skipped": "no_time_left"}))
            raise _HttpFailure(0, {})
        try:
            return _once(method, url, data, headers, min(TIMEOUT_SECONDS, left))
        except _HttpFailure as failure:
            retryable = retry and (failure.status in _RETRY_STATUS or failure.status == 0)
            if failure.status == 429 and _quota_gone(failure):
                retryable = False
            if not retryable or attempt == ATTEMPTS - 1:
                _log(op, failure)
                raise
            wait = failure.retry_after if failure.retry_after is not None else 0.5 * (2 ** attempt)
            wait = min(MAX_RETRY_AFTER, wait) + random.uniform(0, 0.25)
            if _clock() + wait + MIN_ATTEMPT_SECONDS > limit:
                _log(op, failure)
                raise
            _sleep(wait)
    raise AssertionError("unreachable")


def _log(op, failure):
    error = failure.payload.get("error")
    out = {"google": op, "status": failure.status}
    if isinstance(error, dict):
        out["googleStatus"] = str(error.get("status") or "")[:64]
        reasons = [d.get("reason") for d in error.get("details") or [] if isinstance(d, dict) and d.get("reason")]
        if reasons:
            out["reason"] = str(reasons[0])[:64]
    elif isinstance(error, str):  # OAuth endpoints: {"error": "invalid_grant"}
        out["oauthError"] = error[:64]
    logger.warning(json.dumps(out))


def _api_error(failure):
    error = failure.payload.get("error") if isinstance(failure.payload.get("error"), dict) else {}
    reasons = {str(d.get("reason") or "") for d in error.get("details") or [] if isinstance(d, dict)}
    reasons |= {str(e.get("reason") or "") for e in error.get("errors") or [] if isinstance(e, dict)}
    status = failure.status
    if status == 401 or "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in reasons:
        return GoogleBusinessError("needs_reauth")
    if status in (403, 429) and _quota_gone(failure):
        return GoogleBusinessError("quota_exceeded")
    if status == 429 or reasons & {"RATE_LIMIT_EXCEEDED", "rateLimitExceeded", "userRateLimitExceeded"}:
        return GoogleBusinessError("rate_limited", True)
    if status == 403 and reasons & {"SERVICE_DISABLED", "ACCESS_NOT_CONFIGURED", "accessNotConfigured"}:
        return GoogleBusinessError("api_disabled")
    if status == 403 and reasons & {"quotaExceeded", "dailyLimitExceeded"}:
        return GoogleBusinessError("quota_exceeded")
    if status == 403:
        return GoogleBusinessError("permission_denied")
    if status == 404:
        return GoogleBusinessError("not_found")
    if status == 409:
        return GoogleBusinessError("conflict", True)
    if status == 400:
        return GoogleBusinessError("invalid_request")
    return GoogleBusinessError("unavailable", True)


def _oauth_error(failure, invalid_grant_code):
    code = failure.payload.get("error") if isinstance(failure.payload.get("error"), str) else ""
    if code == "invalid_grant":
        return GoogleBusinessError(invalid_grant_code)
    if code in ("invalid_client", "unauthorized_client", "redirect_uri_mismatch"):
        return GoogleBusinessError("misconfigured")
    if failure.status == 429:
        return GoogleBusinessError("rate_limited", True)
    if failure.status == 0 or failure.status >= 500:
        return GoogleBusinessError("unavailable", True)
    if failure.status == 401:
        return GoogleBusinessError("misconfigured")
    return GoogleBusinessError("invalid_request")


def _key(refresh_token):
    return hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()


def _remember(refresh_token, payload):
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise GoogleBusinessError("unavailable", True)
    try:
        lifetime = int(payload.get("expires_in") or 0)
    except (TypeError, ValueError):
        lifetime = 0
    if lifetime > TOKEN_EARLY_SECONDS:
        _ACCESS[_key(refresh_token)] = (access, _clock() + lifetime - TOKEN_EARLY_SECONDS)
    return access


def _granted(payload):
    scope = payload.get("scope")
    return not isinstance(scope, str) or SCOPE in scope.split()


def _address(postal):
    if not isinstance(postal, dict):
        return ""
    lines = [str(line) for line in postal.get("addressLines") or [] if line]
    city = " ".join(str(p) for p in (postal.get("postalCode"), postal.get("locality")) if p)
    return ", ".join(lines + ([city] if city else []))


def review_uri(metadata):
    """The location's review link: metadata.newReviewUri, else the place's
    write-review page built from metadata.placeId."""
    metadata = metadata if isinstance(metadata, dict) else {}
    uri = metadata.get("newReviewUri")
    if isinstance(uri, str) and uri.startswith("https://"):
        return uri
    place = metadata.get("placeId")
    if isinstance(place, str) and place:
        return "https://search.google.com/local/writereview?" + urllib.parse.urlencode({"placeid": place})
    return ""


def _minutes(time_of_day):
    time_of_day = time_of_day or {}
    return int(time_of_day.get("hours") or 0) * 60 + int(time_of_day.get("minutes") or 0)


def _next_day(value):
    day = date(int(value["year"]), int(value["month"]), int(value["day"])) + timedelta(days=1)
    return {"year": day.year, "month": day.month, "day": day.day}


def special_periods(special_hours):
    """Google wants endDate = startDate + 1 when an open special period
    closes after midnight (closeTime not after openTime)."""
    out = []
    for period in (special_hours or {}).get("specialHourPeriods") or []:
        period = dict(period)
        if not period.get("closed") and _minutes(period.get("closeTime")) <= _minutes(period.get("openTime")):
            period["endDate"] = _next_day(period["startDate"])
        out.append(period)
    return out


def hours_body(regular_hours, special_hours):
    """The Location body for PATCH ?updateMask=regularHours,specialHours.
    An empty field is left out, which clears it at Google (the mask names it).
    Google refuses specialHours without regularHours ("This field cannot be
    set without regular hours"), so no regular hours clears both."""
    periods = list((regular_hours or {}).get("periods") or [])
    special = special_periods(special_hours)
    if not periods:
        if special:
            logger.info(json.dumps({"google": "locations.patch", "skipped": "special_without_regular"}))
        return {}
    body = {"regularHours": {"periods": periods}}
    if special:
        body["specialHours"] = {"specialHourPeriods": special}
    return body


class RealGoogleBusiness(GoogleBusiness):

    def authorize_url(self, state, redirect_uri):
        if not redirect_uri:
            raise GoogleBusinessError("misconfigured")
        params = {"client_id": _app()["clientId"], "redirect_uri": redirect_uri, "response_type": "code",
                  "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state}
        return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    def exchange_code(self, code, redirect_uri):
        app = _app()
        try:
            payload = _send("token.exchange", "POST", TOKEN_URL, retry=False,
                            form={"code": code, "client_id": app["clientId"], "client_secret": app["clientSecret"],
                                  "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
        except _HttpFailure as failure:
            raise _oauth_error(failure, "invalid_code") from None
        refresh = payload.get("refresh_token")
        access = payload.get("access_token")
        if not isinstance(access, str) or not access:
            raise GoogleBusinessError("unavailable", True)
        if not isinstance(refresh, str) or not refresh or not _granted(payload):
            # No offline access or the owner unticked the business scope: undo and ask again.
            self._revoke_quietly(refresh or access)
            raise GoogleBusinessError("needs_reauth")
        try:
            accounts = self._accounts(access)
        except GoogleBusinessError:
            self._revoke_quietly(refresh)
            raise
        if not accounts:
            self._revoke_quietly(refresh)
            raise GoogleBusinessError("not_found")
        _remember(refresh, payload)
        return Token(refresh, access, accounts[0])

    def refresh(self, refresh_token):
        cached = _ACCESS.get(_key(refresh_token))
        if cached and cached[1] > _clock():
            return cached[0]
        app = _app()
        try:
            payload = _send("token.refresh", "POST", TOKEN_URL,
                            form={"client_id": app["clientId"], "client_secret": app["clientSecret"],
                                  "grant_type": "refresh_token", "refresh_token": refresh_token})
        except _HttpFailure as failure:
            _ACCESS.pop(_key(refresh_token), None)
            raise _oauth_error(failure, "needs_reauth") from None
        return _remember(refresh_token, payload)

    def _get_pages(self, op, url, params, field, token):
        items, page_token = [], None
        for _ in range(MAX_PAGES):
            query = dict(params, **({"pageToken": page_token} if page_token else {}))
            try:
                page = _send(op, "GET", f"{url}?{urllib.parse.urlencode(query)}", token=token)
            except _HttpFailure as failure:
                raise _api_error(failure) from None
            items.extend(x for x in page.get(field) or [] if isinstance(x, dict))
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        return items

    def _accounts(self, token):
        rows = self._get_pages("accounts.list", ACCOUNTS_URL, {"pageSize": 20}, "accounts", token)
        return [a["name"] for a in rows if _ACCOUNT.fullmatch(str(a.get("name") or ""))]

    def list_locations(self, token):
        out, seen = [], set()
        for account in self._accounts(token):
            rows = self._get_pages("locations.list", f"{INFO_BASE}/{account}/locations",
                                   {"pageSize": 100, "readMask": READ_MASK}, "locations", token)
            for loc in rows:
                name = str(loc.get("name") or "")
                if not _LOCATION.fullmatch(name) or name in seen:
                    continue
                seen.add(name)
                out.append({"name": name, "title": str(loc.get("title") or ""),
                            "address": _address(loc.get("storefrontAddress")),
                            "new_review_uri": review_uri(loc.get("metadata"))})
        return out

    def patch_hours(self, token, location_name, regular_hours, special_hours):
        if not _LOCATION.fullmatch(str(location_name or "")):
            raise GoogleBusinessError("not_found")
        url = f"{INFO_BASE}/{location_name}?{urllib.parse.urlencode({'updateMask': UPDATE_MASK})}"
        try:
            _send("locations.patch", "PATCH", url, body=hours_body(regular_hours, special_hours), token=token)
        except _HttpFailure as failure:
            raise _api_error(failure) from None

    def revoke(self, token):
        _ACCESS.pop(_key(token), None)
        try:
            _send("token.revoke", "POST", REVOKE_URL, form={"token": token})
        except _HttpFailure as failure:
            if failure.status == 400 and failure.payload.get("error") == "invalid_token":
                return  # already revoked or expired
            raise _oauth_error(failure, "needs_reauth") from None

    def _revoke_quietly(self, token):
        if not token:
            return
        try:
            self.revoke(token)
        except GoogleBusinessError:
            pass

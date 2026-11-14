"""The real Google Business Profile client (shared/google_business_real.py)
against a fake urlopen: OAuth URL, exchange, refresh, revoke, accounts and
locations with pagination, the hours PATCH body and updateMask, retries and
the error mapping. Nothing here reaches Google."""

import io
import json
import logging
import urllib.error
import urllib.parse

import pytest

from shared import google_business
from shared import google_business_real as real
from shared.features import google_business as gbp
from shared.google_business import GoogleBusinessError

CLIENT_ID = "client-123.apps.googleusercontent.com"
CLIENT_SECRET = "very-secret-client-value"
REFRESH = "1//refresh-token-value"
ACCESS = "ya29.access-token-value"
REDIRECT = "https://restaurang.dev.booqy.se/installningar?google=1"


class _Response:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeGoogle:
    """Queued answers: a dict (200 JSON), (status, payload[, headers]) or an exception."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request, timeout=None):
        assert timeout == real.TIMEOUT_SECONDS
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, tuple):
            status, payload, *rest = answer
            headers = rest[0] if rest else {}
            raise urllib.error.HTTPError(request.full_url, status, "err", headers,
                                         io.BytesIO(json.dumps(payload).encode()))
        return _Response(json.dumps(answer).encode())

    def form(self, index):
        return dict(urllib.parse.parse_qsl(self.requests[index].data.decode()))

    def url(self, index):
        return urllib.parse.urlsplit(self.requests[index].full_url)

    def query(self, index):
        return dict(urllib.parse.parse_qsl(self.url(index).query))


class FakeSecrets:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def get_secret_value(self, SecretId):
        self.calls += 1
        assert SecretId == "arn:aws:secretsmanager:eu-north-1:1:secret:dev-google-business/app"
        return {"SecretString": self.value}


@pytest.fixture
def secrets(monkeypatch):
    real.reset_cache()
    store = FakeSecrets(json.dumps({"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET}))
    monkeypatch.setenv("GOOGLE_BUSINESS_APP_SECRET_ARN",
                       "arn:aws:secretsmanager:eu-north-1:1:secret:dev-google-business/app")
    monkeypatch.setattr(real.boto3, "client", lambda name: store)
    monkeypatch.setattr(real, "_sleep", lambda seconds: None)
    yield store
    real.reset_cache()


def use(monkeypatch, *answers):
    fake = FakeGoogle(*answers)
    monkeypatch.setattr(real.urllib.request, "urlopen", fake)
    return fake


def err(status, google_status="", reason=None, metadata=None):
    detail = {"@type": "type.googleapis.com/google.rpc.ErrorInfo"}
    if reason:
        detail["reason"] = reason
    if metadata:
        detail["metadata"] = metadata
    return (status, {"error": {"code": status, "message": "m", "status": google_status, "details": [detail]}})


def token_answer(**extra):
    return {"access_token": ACCESS, "expires_in": 3599, "refresh_token": REFRESH, "token_type": "Bearer",
            "scope": real.SCOPE, **extra}


# --- factory -------------------------------------------------------------------------------

def test_factory_returns_the_real_client(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("GOOGLE_BUSINESS_CLIENT", "real")
    assert isinstance(google_business.client(), real.RealGoogleBusiness)


# --- OAuth -----------------------------------------------------------------------------------

def test_authorize_url_has_the_web_server_flow_params(secrets):
    url = real.RealGoogleBusiness().authorize_url("state-abc-123", REDIRECT)
    parts = urllib.parse.urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    assert dict(urllib.parse.parse_qsl(parts.query)) == {
        "client_id": CLIENT_ID, "redirect_uri": REDIRECT, "response_type": "code",
        "scope": "https://www.googleapis.com/auth/business.manage", "access_type": "offline",
        "prompt": "consent", "state": "state-abc-123"}
    assert CLIENT_SECRET not in url


def test_secret_is_read_once_per_container(secrets):
    client = real.RealGoogleBusiness()
    client.authorize_url("s1-state-x", REDIRECT)
    client.authorize_url("s2-state-x", REDIRECT)
    assert secrets.calls == 1


@pytest.mark.parametrize("value", ["{}", json.dumps({"clientId": "x"}), "not json"])
def test_bad_secret_is_misconfigured_and_not_logged(secrets, caplog, value):
    secrets.value = value
    with caplog.at_level(logging.INFO), pytest.raises(GoogleBusinessError) as exc:
        real.RealGoogleBusiness().authorize_url("state-abc-123", REDIRECT)
    assert exc.value.code == "misconfigured"
    assert value not in caplog.text or value == "{}"


def test_exchange_code_posts_the_code_and_reads_the_account(secrets, monkeypatch):
    fake = use(monkeypatch, token_answer(),
               {"accounts": [{"name": "accounts/111", "type": "PERSONAL"}, {"name": "accounts/222"}]})
    token = real.RealGoogleBusiness().exchange_code("4/auth-code", REDIRECT)
    assert token == google_business.Token(REFRESH, ACCESS, "accounts/111")
    assert fake.requests[0].full_url == "https://oauth2.googleapis.com/token"
    assert fake.requests[0].get_method() == "POST"
    assert fake.requests[0].get_header("Content-type") == "application/x-www-form-urlencoded"
    assert fake.form(0) == {"code": "4/auth-code", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                            "redirect_uri": REDIRECT, "grant_type": "authorization_code"}
    assert fake.url(1).netloc == "mybusinessaccountmanagement.googleapis.com"
    assert fake.url(1).path == "/v1/accounts" and fake.query(1) == {"pageSize": "20"}
    assert fake.requests[1].get_header("Authorization") == f"Bearer {ACCESS}"
    # the exchange's access token is reused: no refresh call afterwards
    assert real.RealGoogleBusiness().refresh(REFRESH) == ACCESS
    assert len(fake.requests) == 2


def test_exchange_invalid_grant_is_invalid_code(secrets, monkeypatch):
    use(monkeypatch, (400, {"error": "invalid_grant", "error_description": "Bad Request"}))
    with pytest.raises(GoogleBusinessError) as exc:
        real.RealGoogleBusiness().exchange_code("4/old", REDIRECT)
    assert exc.value.code == "invalid_code" and not exc.value.retryable


def test_exchange_redirect_mismatch_is_misconfigured(secrets, monkeypatch):
    use(monkeypatch, (400, {"error": "redirect_uri_mismatch"}))
    with pytest.raises(GoogleBusinessError) as exc:
        real.RealGoogleBusiness().exchange_code("4/x", REDIRECT)
    assert exc.value.code == "misconfigured"


def test_exchange_never_retries_a_timeout(secrets, monkeypatch):
    fake = use(monkeypatch, TimeoutError())
    with pytest.raises(GoogleBusinessError) as exc:
        real.RealGoogleBusiness().exchange_code("4/x", REDIRECT)
    assert exc.value.code == "unavailable" and exc.value.retryable and len(fake.requests) == 1


@pytest.mark.parametrize("answer", [token_answer(refresh_token=None),
                                    token_answer(scope="openid email")])
def test_exchange_without_offline_grant_or_scope_revokes_and_needs_reauth(secrets, monkeypatch, answer):
    fake = use(monkeypatch, answer, {})
    with pytest.raises(GoogleBusinessError) as exc:
        real.RealGoogleBusiness().exchange_code("4/x", REDIRECT)
    assert exc.value.code == "needs_reauth"
    assert fake.requests[1].full_url == "https://oauth2.googleapis.com/revoke"


def test_exchange_with_no_account_revokes(secrets, monkeypatch):
    fake = use(monkeypatch, token_answer(), {"accounts": []}, {})
    with pytest.raises(GoogleBusinessError) as exc:
        real.RealGoogleBusiness().exchange_code("4/x", REDIRECT)
    assert exc.value.code == "not_found"
    assert fake.form(2) == {"token": REFRESH}


def test_refresh_posts_the_refresh_grant_and_caches(secrets, monkeypatch):
    fake = use(monkeypatch, {"access_token": "ya29.fresh", "expires_in": 3599, "token_type": "Bearer"})
    client = real.RealGoogleBusiness()
    assert client.refresh(REFRESH) == "ya29.fresh"
    assert client.refresh(REFRESH) == "ya29.fresh"
    assert len(fake.requests) == 1
    assert fake.form(0) == {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                            "grant_type": "refresh_token", "refresh_token": REFRESH}


def test_refresh_after_expiry_calls_again(secrets, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(real, "_clock", lambda: now[0])
    fake = use(monkeypatch, {"access_token": "a1", "expires_in": 3600}, {"access_token": "a2", "expires_in": 3600})
    client = real.RealGoogleBusiness()
    assert client.refresh(REFRESH) == "a1"
    now[0] += 3600 - real.TOKEN_EARLY_SECONDS + 1
    assert client.refresh(REFRESH) == "a2" and len(fake.requests) == 2


def test_refresh_invalid_grant_is_needs_reauth(secrets, monkeypatch):
    use(monkeypatch, (400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}))
    with pytest.raises(GoogleBusinessError) as exc:
        real.RealGoogleBusiness().refresh(REFRESH)
    assert exc.value.code == "needs_reauth" and exc.value.code in gbp.REAUTH_CODES


def test_refresh_retries_5xx_then_works(secrets, monkeypatch):
    fake = use(monkeypatch, (503, {}), {"access_token": "a1", "expires_in": 3600})
    assert real.RealGoogleBusiness().refresh(REFRESH) == "a1" and len(fake.requests) == 2


def test_revoke_posts_the_token_and_ignores_already_revoked(secrets, monkeypatch):
    fake = use(monkeypatch, {}, (400, {"error": "invalid_token"}))
    client = real.RealGoogleBusiness()
    client.revoke(REFRESH)
    client.revoke(REFRESH)
    assert fake.requests[0].full_url == "https://oauth2.googleapis.com/revoke"
    assert fake.requests[0].get_method() == "POST" and fake.form(0) == {"token": REFRESH}


def test_revoke_google_down_is_unavailable(secrets, monkeypatch):
    use(monkeypatch, (500, {}), (500, {}), (500, {}))
    with pytest.raises(GoogleBusinessError) as exc:
        real.RealGoogleBusiness().revoke(REFRESH)
    assert exc.value.code == "unavailable" and exc.value.retryable


# --- locations -------------------------------------------------------------------------------

def _location(i, **extra):
    return {"name": f"locations/{i}", "title": f"Krog {i}",
            "storefrontAddress": {"addressLines": [f"Storgatan {i}"], "postalCode": "111 22", "locality": "Stockholm"},
            "metadata": {"placeId": f"place{i}", "newReviewUri": f"https://g.page/r/{i}/review"}, **extra}


def test_list_locations_pages_accounts_and_locations(secrets, monkeypatch):
    fake = use(monkeypatch,
               {"accounts": [{"name": "accounts/1"}], "nextPageToken": "acc-p2"},
               {"accounts": [{"name": "accounts/2"}]},
               {"locations": [_location(10)], "nextPageToken": "loc-p2"},
               {"locations": [_location(11, metadata={"placeId": "ChIJabc"})]},
               {"locations": [_location(10), _location(12, metadata={})]})
    rows = real.RealGoogleBusiness().list_locations(ACCESS)
    assert rows == [
        {"name": "locations/10", "title": "Krog 10", "address": "Storgatan 10, 111 22 Stockholm",
         "new_review_uri": "https://g.page/r/10/review"},
        {"name": "locations/11", "title": "Krog 11", "address": "Storgatan 11, 111 22 Stockholm",
         "new_review_uri": "https://search.google.com/local/writereview?placeid=ChIJabc"},
        {"name": "locations/12", "title": "Krog 12", "address": "Storgatan 12, 111 22 Stockholm",
         "new_review_uri": ""},
    ]
    assert fake.query(1) == {"pageSize": "20", "pageToken": "acc-p2"}
    assert fake.url(2).netloc == "mybusinessbusinessinformation.googleapis.com"
    assert fake.url(2).path == "/v1/accounts/1/locations"
    assert fake.query(2) == {"pageSize": "100", "readMask": "name,title,storefrontAddress,metadata"}
    assert fake.query(3)["pageToken"] == "loc-p2"
    assert fake.url(4).path == "/v1/accounts/2/locations"
    assert all(r.get_header("Authorization") == f"Bearer {ACCESS}" for r in fake.requests)
    # every row fits the link validation of the feature
    assert all(gbp.valid_google_location(r["name"]) for r in rows)


def test_list_locations_401_is_needs_reauth(secrets, monkeypatch):
    use(monkeypatch, err(401, "UNAUTHENTICATED"))
    with pytest.raises(GoogleBusinessError) as exc:
        real.RealGoogleBusiness().list_locations(ACCESS)
    assert exc.value.code == "needs_reauth"


# --- hours -------------------------------------------------------------------------------------

def _mapped():
    location = {"businessHours": {
        "monday": [{"opensAt": "11:00", "closesAt": "14:00"}, {"opensAt": "17:00", "closesAt": "22:30"}],
        "friday": [{"opensAt": "18:00", "closesAt": "02:00"}],
        "sunday": [{"opensAt": "20:00", "closesAt": "01:00"}],
        "tuesday": [], "wednesday": [], "thursday": [], "saturday": []},
        "specialDays": [
            {"date": "2026-12-24", "kind": "closed"},
            {"date": "2026-12-31", "kind": "hours", "intervals": [{"opensAt": "18:00", "closesAt": "01:30"}]},
            {"date": "2026-12-30", "kind": "hours", "intervals": [{"opensAt": "12:00", "closesAt": "15:00"}]},
            {"date": "2026-12-29", "kind": "no_online"}]}
    from datetime import date
    return gbp.map_hours(location, date(2026, 11, 14))


def test_patch_hours_body_and_update_mask(secrets, monkeypatch):
    fake = use(monkeypatch, {"name": "locations/10"})
    mapped = _mapped()
    real.RealGoogleBusiness().patch_hours(ACCESS, "locations/10", mapped["regularHours"], mapped["specialHours"])
    request = fake.requests[0]
    assert request.get_method() == "PATCH"
    assert fake.url(0).netloc == "mybusinessbusinessinformation.googleapis.com"
    assert fake.url(0).path == "/v1/locations/10"
    assert fake.query(0) == {"updateMask": "regularHours,specialHours"}
    assert request.get_header("Content-type") == "application/json"
    body = json.loads(request.data)
    assert body["regularHours"]["periods"] == [
        {"openDay": "MONDAY", "openTime": {"hours": 11}, "closeDay": "MONDAY", "closeTime": {"hours": 14}},
        {"openDay": "MONDAY", "openTime": {"hours": 17}, "closeDay": "MONDAY",
         "closeTime": {"hours": 22, "minutes": 30}},
        # overnight: closes on the next day; Sunday wraps to Monday
        {"openDay": "FRIDAY", "openTime": {"hours": 18}, "closeDay": "SATURDAY", "closeTime": {"hours": 2}},
        {"openDay": "SUNDAY", "openTime": {"hours": 20}, "closeDay": "MONDAY", "closeTime": {"hours": 1}},
    ]
    # closed weekdays are simply absent; special days sorted, no_online never sent
    assert body["specialHours"]["specialHourPeriods"] == [
        {"startDate": {"year": 2026, "month": 12, "day": 24}, "endDate": {"year": 2026, "month": 12, "day": 24},
         "closed": True},
        {"startDate": {"year": 2026, "month": 12, "day": 30}, "endDate": {"year": 2026, "month": 12, "day": 30},
         "closed": False, "openTime": {"hours": 12}, "closeTime": {"hours": 15}},
        # past midnight on New Year's Eve: endDate is the next day (and the next year)
        {"startDate": {"year": 2026, "month": 12, "day": 31}, "endDate": {"year": 2027, "month": 1, "day": 1},
         "closed": False, "openTime": {"hours": 18}, "closeTime": {"hours": 1, "minutes": 30}},
    ]
    assert request.get_header("Authorization") == f"Bearer {ACCESS}"


def test_patch_hours_all_closed_clears_both_fields(secrets, monkeypatch):
    fake = use(monkeypatch, {})
    real.RealGoogleBusiness().patch_hours(ACCESS, "locations/10", {"periods": []}, {"specialHourPeriods": []})
    assert json.loads(fake.requests[0].data) == {}
    assert fake.query(0) == {"updateMask": "regularHours,specialHours"}


def test_patch_hours_bad_location_never_calls(secrets, monkeypatch):
    fake = use(monkeypatch)
    with pytest.raises(GoogleBusinessError) as exc:
        real.RealGoogleBusiness().patch_hours(ACCESS, "locations/../accounts/1", {}, {})
    assert exc.value.code == "not_found" and fake.requests == []


def test_rate_limit_retries_with_retry_after_then_succeeds(secrets, monkeypatch):
    waits = []
    monkeypatch.setattr(real, "_sleep", waits.append)
    fake = use(monkeypatch, err(429, "RESOURCE_EXHAUSTED", "RATE_LIMIT_EXCEEDED") + ({"Retry-After": "2"},),
               (503, {}), {})
    real.RealGoogleBusiness().patch_hours(ACCESS, "locations/10", {"periods": []}, {})
    assert len(fake.requests) == 3 and len(waits) == 2
    assert 2 <= waits[0] <= 2.25 and 1 <= waits[1] <= 1.25


def test_rate_limit_after_all_attempts_is_retryable(secrets, monkeypatch):
    fake = use(monkeypatch, *[err(429, "RESOURCE_EXHAUSTED", "RATE_LIMIT_EXCEEDED")] * 3)
    with pytest.raises(GoogleBusinessError) as exc:
        real.RealGoogleBusiness().patch_hours(ACCESS, "locations/10", {}, {})
    assert exc.value.code == "rate_limited" and exc.value.retryable and len(fake.requests) == real.ATTEMPTS


def test_retries_stop_at_the_time_budget(secrets, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(real, "_clock", lambda: now[0])
    monkeypatch.setattr(real, "_sleep", lambda s: None)

    def slow(*args):
        now[0] += 12
        raise real._HttpFailure(503, {})
    monkeypatch.setattr(real, "_once", slow)
    with pytest.raises(GoogleBusinessError) as exc:
        real.RealGoogleBusiness().patch_hours(ACCESS, "locations/10", {}, {})
    assert exc.value.code == "unavailable" and now[0] == 12


def test_network_failure_is_retried_then_unavailable(secrets, monkeypatch):
    fake = use(monkeypatch, urllib.error.URLError("dns"), TimeoutError(), OSError("reset"))
    with pytest.raises(GoogleBusinessError) as exc:
        real.RealGoogleBusiness().patch_hours(ACCESS, "locations/10", {}, {})
    assert exc.value.code == "unavailable" and exc.value.retryable and len(fake.requests) == 3


@pytest.mark.parametrize("answer, code, retryable, calls", [
    (err(401, "UNAUTHENTICATED"), "needs_reauth", False, 1),
    (err(403, "PERMISSION_DENIED", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"), "needs_reauth", False, 1),
    (err(403, "PERMISSION_DENIED"), "permission_denied", False, 1),
    (err(403, "PERMISSION_DENIED", "SERVICE_DISABLED"), "api_disabled", False, 1),
    (err(429, "RESOURCE_EXHAUSTED", "RATE_LIMIT_EXCEEDED", {"quota_limit_value": "0"}), "quota_exceeded", False, 1),
    (err(429, "RESOURCE_EXHAUSTED", "RATE_LIMIT_EXCEEDED",
         {"quota_limit": "UpdateLocationPerDayPerProject", "quota_limit_value": "10000"}), "quota_exceeded", False, 1),
    (err(404, "NOT_FOUND"), "not_found", False, 1),
    (err(400, "INVALID_ARGUMENT"), "invalid_request", False, 1),
    (err(409, "ABORTED"), "conflict", True, 1),
    (err(500, "INTERNAL"), "unavailable", True, 3),
    ((502, "<html>bad gateway</html>"), "unavailable", True, 3),
])
def test_error_mapping(secrets, monkeypatch, answer, code, retryable, calls):
    fake = use(monkeypatch, *[answer] * calls)
    with pytest.raises(GoogleBusinessError) as exc:
        real.RealGoogleBusiness().patch_hours(ACCESS, "locations/10", {}, {})
    assert (exc.value.code, exc.value.retryable) == (code, retryable)
    assert len(fake.requests) == calls


def test_no_secret_or_token_in_logs(secrets, monkeypatch, caplog):
    use(monkeypatch, (400, {"error": "invalid_grant", "error_description": f"bad {REFRESH}"}),
        (401, {"error": {"code": 401, "message": f"token {ACCESS}", "status": "UNAUTHENTICATED"}}),
        (400, {"error": "invalid_grant"}))
    client = real.RealGoogleBusiness()
    with caplog.at_level(logging.DEBUG):
        for call in (lambda: client.refresh(REFRESH), lambda: client.list_locations(ACCESS),
                     lambda: client.exchange_code("4/the-code", REDIRECT)):
            with pytest.raises(GoogleBusinessError):
                call()
    assert caplog.records  # failures are logged ...
    for needle in (REFRESH, ACCESS, CLIENT_SECRET, "4/the-code"):  # ... without any secret
        assert needle not in caplog.text
    assert '"oauthError": "invalid_grant"' in caplog.text and '"status": 401' in caplog.text


def test_jobs_mark_needs_reauth_on_real_codes():
    assert "needs_reauth" in gbp.REAUTH_CODES

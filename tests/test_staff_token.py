"""shared/staff_token.py: only a token Cognito accepts names a tenant."""

import base64
import json

import pytest
from botocore.exceptions import ClientError

from shared import staff_token

POOL = "eu-north-1_Booqy"
CLIENT = "staff-client"
ISSUER = f"https://cognito-idp.eu-north-1.amazonaws.com/{POOL}"


def jwt(**overrides):
    claims = {"iss": ISSUER, "token_use": "access", "client_id": CLIENT, "sub": "u1"}
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJSUzI1NiJ9.{payload}.sig"


TOKEN = jwt()


@pytest.fixture(autouse=True)
def tenant_pool(monkeypatch):
    monkeypatch.setenv("TENANT_USER_POOL_ID", POOL)
    monkeypatch.setenv("TENANT_APP_CLIENT_ID", CLIENT)


class FakeCognito:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.tokens = result, error, []

    def get_user(self, AccessToken):
        self.tokens.append(AccessToken)
        if self.error:
            raise self.error
        return self.result


def ev(auth):
    return {"headers": {"Authorization": auth}} if auth is not None else {"headers": {}}


def test_bearer_parsing():
    assert staff_token.bearer(ev("Bearer abc")) == "abc"
    assert staff_token.bearer({"headers": {"authorization": "bearer  abc "}}) == "abc"
    assert staff_token.bearer(ev("Basic abc")) is None
    assert staff_token.bearer(ev("Bearer ")) is None
    assert staff_token.bearer(ev("Bearer " + "x" * 9000)) is None
    assert staff_token.bearer(ev(None)) is None
    assert staff_token.bearer(None) is None


def test_tenant_from_a_valid_token(monkeypatch):
    fake = FakeCognito({"UserAttributes": [
        {"Name": "sub", "Value": "u1"}, {"Name": "custom:tenant_id", "Value": "t-1"}]})
    monkeypatch.setattr(staff_token, "_cognito", lambda: fake)
    assert staff_token.tenant_of(ev("Bearer " + TOKEN)) == "t-1"
    assert fake.tokens == [TOKEN]


def test_no_tenant_when_cognito_refuses_or_has_none(monkeypatch):
    refused = FakeCognito(error=ClientError(
        {"Error": {"Code": "NotAuthorizedException", "Message": "expired"}}, "GetUser"))
    monkeypatch.setattr(staff_token, "_cognito", lambda: refused)
    assert staff_token.tenant_of(ev("Bearer " + TOKEN)) is None
    monkeypatch.setattr(staff_token, "_cognito", lambda: FakeCognito({"UserAttributes": []}))
    assert staff_token.tenant_of(ev("Bearer " + TOKEN)) is None
    untouched = FakeCognito()
    monkeypatch.setattr(staff_token, "_cognito", lambda: untouched)
    assert staff_token.tenant_of(ev(None)) is None and untouched.tokens == []


TENANT_USER = {"UserAttributes": [{"Name": "custom:tenant_id", "Value": "t-victim"}]}


@pytest.mark.parametrize("token", [
    # A pool the attacker owns, with a custom:tenant_id they chose.
    jwt(iss="https://cognito-idp.eu-north-1.amazonaws.com/eu-north-1_Attacker"),
    jwt(iss=None),
    # An ID token, not an access token.
    jwt(token_use="id"),
    # Another app client in the right pool.
    jwt(client_id="other-client"),
    jwt(client_id=None),
    "not-a-jwt",
    "a.!!!.c",
    "a." + base64.urlsafe_b64encode(b"[1,2]").decode() + ".c",
])
def test_tokens_not_from_the_staff_client_are_refused_before_cognito(monkeypatch, token):
    untouched = FakeCognito(TENANT_USER)
    monkeypatch.setattr(staff_token, "_cognito", lambda: untouched)
    assert staff_token.tenant_of(ev("Bearer " + token)) is None
    assert untouched.tokens == []


@pytest.mark.parametrize("missing", ["TENANT_USER_POOL_ID", "TENANT_APP_CLIENT_ID"])
def test_nobody_is_staff_without_the_pool_config(monkeypatch, missing):
    monkeypatch.delenv(missing)
    untouched = FakeCognito(TENANT_USER)
    monkeypatch.setattr(staff_token, "_cognito", lambda: untouched)
    assert staff_token.tenant_of(ev("Bearer " + TOKEN)) is None
    assert untouched.tokens == []

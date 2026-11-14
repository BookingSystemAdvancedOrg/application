"""shared/staff_token.py: only a token Cognito accepts names a tenant."""

from botocore.exceptions import ClientError

from shared import staff_token


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
    assert staff_token.tenant_of(ev("Bearer tok")) == "t-1"
    assert fake.tokens == ["tok"]


def test_no_tenant_when_cognito_refuses_or_has_none(monkeypatch):
    refused = FakeCognito(error=ClientError(
        {"Error": {"Code": "NotAuthorizedException", "Message": "expired"}}, "GetUser"))
    monkeypatch.setattr(staff_token, "_cognito", lambda: refused)
    assert staff_token.tenant_of(ev("Bearer tok")) is None
    monkeypatch.setattr(staff_token, "_cognito", lambda: FakeCognito({"UserAttributes": []}))
    assert staff_token.tenant_of(ev("Bearer tok")) is None
    untouched = FakeCognito()
    monkeypatch.setattr(staff_token, "_cognito", lambda: untouched)
    assert staff_token.tenant_of(ev(None)) is None and untouched.tokens == []

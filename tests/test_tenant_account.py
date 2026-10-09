"""tenant-account: GET/PATCH /tenant and the Stripe onboarding link - always
the token's own tenant, never one named in the request."""

import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from shared import dynamo as shared_dynamo
from shared import tenant as shared_tenant
from tenant_support import (OWNER_A_SUB, REGION, STAFF_A_SUB, TENANT_A, TENANT_B, TENANT_TABLE,
                            seed_tenancy, tenant_claims, tenant_row)

APP_PATH = Path(__file__).parents[1] / "functions" / "tenant-account" / "app.py"
ADMIN_URL = "https://admin.example.se"


def make_event(method="GET", path="/tenant", body=None, *, tenant_id=TENANT_A, role="owner_user"):
    sub = OWNER_A_SUB if role == "owner_user" else STAFF_A_SUB
    event = {
        "routeKey": f"{method} {path}",
        "requestContext": {
            "http": {"method": method, "path": path},
            "authorizer": {"jwt": {"claims": tenant_claims(sub, f'["{role}"]', tenant_id)}},
        },
    }
    if body is not None:
        event["body"] = body if isinstance(body, str) else json.dumps(body)
    return event


def body_of(response):
    return json.loads(response["body"])


@pytest.fixture
def app(monkeypatch):
    for key, value in {
        "ENVIRONMENT": "dev", "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": REGION,
        "STRIPE_SECRET_ARN": "arn:aws:secretsmanager:eu-north-1:1:secret:stripe",
        "STRIPE_API_VERSION": "2026-07-29.dahlia", "ADMIN_APP_URL": ADMIN_URL + "/",
    }.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        seed_tenancy(
            monkeypatch,
            tenant_a=tenant_row(
                TENANT_A, slug="roma", planId="growth", locationCount=2, primaryDomain="www.roma.se",
                stripe={"accountId": "acct_A", "chargesEnabled": True, "taxRates": {"food": "txr_1"}},
                lastError="operator-only detail", onboardingExecutionArn="arn:secret",
                senderName="Roma",
            ),
            tenant_b=tenant_row(TENANT_B, stripe={"accountId": "acct_B"}),
        )
        tenants = boto3.resource("dynamodb", region_name=REGION).Table(TENANT_TABLE)
        tenants.put_item(Item={"PK": f"TENANT#{TENANT_A}", "SK": "DOMAIN#www.roma.se",
                               "domain": "www.roma.se", "kind": "custom", "status": "active",
                               "cnameTarget": "d1.cloudfront.net"})
        tenants.put_item(Item={"PK": f"TENANT#{TENANT_B}", "SK": "DOMAIN#www.other.se",
                               "domain": "www.other.se", "kind": "custom", "status": "active"})
        spec = importlib.util.spec_from_file_location("tenant_account_app", APP_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module._stripe_key = "sk_test_x"
        yield module, tenants
        shared_dynamo._resource = None
        shared_dynamo._client = None
        shared_tenant.reset_caches()


def test_get_returns_own_tenant_without_operator_fields(app):
    module, _ = app

    response = module.handler(make_event(), None)

    assert response["statusCode"] == 200 and response["headers"]["Cache-Control"] == "no-store"
    body = body_of(response)
    assert body["tenantId"] == TENANT_A and body["slug"] == "roma" and body["role"] == "owner_user"
    assert body["stripe"] == {"connected": True, "chargesEnabled": True, "payoutsEnabled": False,
                              "detailsSubmitted": False}
    assert body["domains"] == [{"domain": "www.roma.se", "kind": "custom", "status": "active",
                                "cnameTarget": "d1.cloudfront.net", "primary": True}]
    for secret in ("operator-only detail", "arn:secret", "txr_1", "acct_A", "www.other.se"):
        assert secret not in response["body"]


def test_staff_can_read(app):
    module, _ = app
    assert body_of(module.handler(make_event(role="staff_user"), None))["role"] == "staff_user"


def test_tenant_comes_only_from_the_token(app):
    module, _ = app
    event = make_event(tenant_id=TENANT_B)
    event["queryStringParameters"] = {"tenantId": TENANT_A}
    event["headers"] = {"x-tenant-id": TENANT_A}

    assert body_of(module.handler(event, None))["tenantId"] == TENANT_B


def test_suspended_tenant_is_refused(app):
    module, tenants = app
    tenants.put_item(Item=tenant_row(TENANT_A, status="suspended"))
    shared_tenant.reset_caches()

    response = module.handler(make_event(), None)

    assert response["statusCode"] == 403 and body_of(response) == {"error": "tenant_inactive"}


def test_owner_edits_only_self_service_fields(app):
    module, tenants = app

    response = module.handler(make_event("PATCH", body={
        "senderName": " Roma Bar ", "replyToEmail": "Hej@Roma.SE", "branding": {"color": "#aa0000"}}), None)

    assert response["statusCode"] == 200
    assert body_of(response)["senderName"] == "Roma Bar"
    row = tenants.get_item(Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"})["Item"]
    assert row["replyToEmail"] == "hej@roma.se" and row["branding"] == {"color": "#aa0000"}
    assert row["updatedBy"] == OWNER_A_SUB and row["planId"] == "growth"


def test_empty_value_removes_the_field(app):
    module, tenants = app
    assert module.handler(make_event("PATCH", body={"senderName": ""}), None)["statusCode"] == 200
    assert "senderName" not in tenants.get_item(Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"})["Item"]


@pytest.mark.parametrize(("body", "error"), [
    ({"planId": "growth"}, "unsupported fields: planId"),
    ({"status": "active", "senderName": "x"}, "unsupported fields: status"),
    ({}, "nothing to update"),
    ({"senderName": "Way too long name"}, "senderName must be 1-11 letters, digits or spaces"),
    ({"senderName": "Röma!"}, "senderName must be 1-11 letters, digits or spaces"),
    ({"replyToEmail": "nope"}, "replyToEmail must be a valid email address"),
    ({"branding": "red"}, "branding must be an object of at most 4 KB"),
    ({"branding": {"x": "y" * 5000}}, "branding must be an object of at most 4 KB"),
    ("[]", "request body must be a JSON object"),
    ("{", "request body must be valid JSON"),
])
def test_invalid_edits_are_rejected(app, body, error):
    module, tenants = app
    before = tenants.get_item(Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"})["Item"]

    response = module.handler(make_event("PATCH", body=body), None)

    assert response["statusCode"] == 400 and body_of(response) == {"error": error}
    assert tenants.get_item(Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"})["Item"] == before


def test_update_never_asks_for_the_whole_row(app, monkeypatch):
    """IAM allows only ReturnValues NONE/UPDATED_* on this table."""
    module, tenants = app
    spy = Mock(wraps=tenants)
    monkeypatch.setattr(module, "table", lambda _: spy)

    module.handler(make_event("PATCH", body={"senderName": "Roma"}), None)

    assert spy.update_item.call_args.kwargs["ReturnValues"] == "NONE"
    names = set(spy.update_item.call_args.kwargs["ExpressionAttributeNames"].values())
    assert names <= {"senderName", "replyToEmail", "branding"}


@pytest.mark.parametrize(("method", "path"), [("PATCH", "/tenant"),
                                              ("POST", "/tenant/stripe/account-link")])
def test_staff_cannot_change_anything(app, method, path):
    module, _ = app
    response = module.handler(make_event(method, path, body={"senderName": "x"}, role="staff_user"), None)
    assert response["statusCode"] == 403 and body_of(response) == {"error": "owner_only"}


def test_account_link_is_for_the_callers_stripe_account(app, monkeypatch):
    module, _ = app
    sent = []

    def fake_urlopen(request, timeout):
        sent.append(request)
        return io.BytesIO(b'{"url": "https://connect.stripe.com/x", "expires_at": "2026-10-09T10:00:00Z"}')

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)

    response = module.handler(make_event("POST", "/tenant/stripe/account-link"), None)

    assert response["statusCode"] == 200 and body_of(response)["url"] == "https://connect.stripe.com/x"
    (request,) = sent
    assert request.full_url == "https://api.stripe.com/v2/core/account_links"
    payload = json.loads(request.data)
    assert payload["account"] == "acct_A"
    onboarding = payload["use_case"]["account_onboarding"]
    assert onboarding["configurations"] == ["merchant"]
    assert onboarding["return_url"] == f"{ADMIN_URL}/settings/payments?stripe=return"


def test_account_link_without_stripe_account_is_409(app):
    module, tenants = app
    tenants.put_item(Item=tenant_row(TENANT_A))
    shared_tenant.reset_caches()

    response = module.handler(make_event("POST", "/tenant/stripe/account-link"), None)

    assert response["statusCode"] == 409 and body_of(response) == {"error": "no_stripe_account"}


def test_stripe_failure_is_sanitized_503(app, monkeypatch):
    module, _ = app

    def boom(request, timeout):
        raise module.urllib.error.URLError("secret detail")

    monkeypatch.setattr(module.urllib.request, "urlopen", boom)

    response = module.handler(make_event("POST", "/tenant/stripe/account-link"), None)

    assert response["statusCode"] == 503 and "secret detail" not in response["body"]


def test_dynamodb_failure_is_sanitized_503(app, monkeypatch):
    module, _ = app
    failing = Mock()
    failing.get_item.side_effect = ClientError({"Error": {"Code": "InternalServerError",
                                                         "Message": "secret detail"}}, "GetItem")
    monkeypatch.setattr(shared_dynamo, "table", lambda _: failing)

    response = module.handler(make_event(), None)

    assert response["statusCode"] == 503 and "secret detail" not in response["body"]


def test_unknown_route_and_method(app):
    module, _ = app
    assert module.handler(make_event("GET", "/tenant/other"), None)["statusCode"] == 404
    response = module.handler(make_event("DELETE", "/tenant"), None)
    assert response["statusCode"] == 405 and response["headers"]["Allow"] == "GET, PATCH"


def test_no_token_is_401(app):
    module, _ = app
    event = make_event()
    del event["requestContext"]["authorizer"]
    assert module.handler(event, None)["statusCode"] == 401


def test_branding_with_decimal_numbers_is_stored(app):
    module, tenants = app

    response = module.handler(make_event("PATCH", body='{"branding": {"opacity": 0.5, "logo": {"w": 120}}}'), None)

    assert response["statusCode"] == 200
    assert body_of(response)["branding"] == {"opacity": 0.5, "logo": {"w": 120}}


@pytest.mark.parametrize("raw", ['{"branding": {"x": NaN}}', '{"branding": {"x": Infinity}}',
                                 '{"branding": {"": 1}}'])
def test_unstorable_branding_is_400(app, raw):
    module, _ = app
    assert module.handler(make_event("PATCH", body=raw), None)["statusCode"] == 400


def test_get_after_patch_and_after_external_change_is_fresh(app):
    module, tenants = app
    module.handler(make_event(), None)  # warm the per-container cache
    module.handler(make_event("PATCH", body={"senderName": "Nya"}), None)
    assert body_of(module.handler(make_event(), None))["senderName"] == "Nya"

    # e.g. an operator raised the plan / onboarding saved the Stripe account
    row = tenants.get_item(Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"})["Item"]
    row["locationCount"] = 3
    tenants.put_item(Item=row)
    assert body_of(module.handler(make_event(), None))["locationCount"] == 3


def test_account_link_sees_a_stripe_account_saved_a_moment_ago(app, monkeypatch):
    module, tenants = app
    tenants.put_item(Item=tenant_row(TENANT_A))  # no Stripe account yet
    shared_tenant.reset_caches()
    assert module.handler(make_event("POST", "/tenant/stripe/account-link"), None)["statusCode"] == 409

    tenants.put_item(Item=tenant_row(TENANT_A, stripe={"accountId": "acct_new"}))  # onboarding saved it
    monkeypatch.setattr(module.urllib.request, "urlopen",
                        lambda request, timeout: io.BytesIO(b'{"url": "https://connect.stripe.com/y"}'))

    assert module.handler(make_event("POST", "/tenant/stripe/account-link"), None)["statusCode"] == 200

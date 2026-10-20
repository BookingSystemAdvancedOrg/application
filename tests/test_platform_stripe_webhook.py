"""platform-stripe-webhook: signed Connect events keep PROFILE.stripe.* in
sync. DynamoDB and Secrets Manager are moto; signatures are real HMACs."""

import hashlib
import hmac
import importlib.util
import json
import time
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from shared import dynamo as shared_dynamo
from tenant_support import REGION, TENANT_A, TENANT_B, TENANT_TABLE, seed_tenancy, tenant_row

APP = Path(__file__).parents[1] / "functions" / "platform-stripe-webhook" / "app.py"
CONNECT_SECRET = "whsec_connect_test"
BILLING_SECRET = "whsec_billing_test"
ACCT_A = "acct_1AAAAAAAAAAAAAAA"
ACCT_B = "acct_1BBBBBBBBBBBBBBB"


def _stripe(account_id):
    return {"accountId": account_id, "chargesEnabled": False, "payoutsEnabled": False,
            "detailsSubmitted": False, "taxRates": {}}


@pytest.fixture
def app(monkeypatch):
    for key, value in {"ENVIRONMENT": "dev", "AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t",
                       "AWS_SESSION_TOKEN": "t", "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        sm = boto3.client("secretsmanager", region_name=REGION)
        monkeypatch.setenv("CONNECT_WEBHOOK_SECRET_ARN",
                           sm.create_secret(Name="connect", SecretString=CONNECT_SECRET)["ARN"])
        monkeypatch.setenv("BILLING_WEBHOOK_SECRET_ARN",
                           sm.create_secret(Name="billing", SecretString=BILLING_SECRET)["ARN"])
        seed_tenancy(monkeypatch,
                     tenant_a=tenant_row(TENANT_A, stripe=_stripe(ACCT_A)),
                     tenant_b=tenant_row(TENANT_B, stripe=_stripe(ACCT_B)))
        table = boto3.resource("dynamodb", region_name=REGION).Table(TENANT_TABLE)
        table.put_item(Item={"PK": f"STRIPE_ACCOUNT#{ACCT_A}", "SK": "TENANT", "tenantId": TENANT_A})
        table.put_item(Item={"PK": f"STRIPE_ACCOUNT#{ACCT_B}", "SK": "TENANT", "tenantId": TENANT_B})
        spec = importlib.util.spec_from_file_location("platform_stripe_webhook_app", APP)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.reset_cache()
        yield module


def _profile(tenant_id):
    item = boto3.resource("dynamodb", region_name=REGION).Table(TENANT_TABLE).get_item(
        Key={"PK": f"TENANT#{tenant_id}", "SK": "PROFILE"})["Item"]
    return item["stripe"]


def _event(evt, *, path="/connect", secret=CONNECT_SECRET, signature=None, ts=None):
    body = json.dumps(evt)
    ts = int(time.time()) if ts is None else ts
    sig = signature or hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return {"rawPath": path, "body": body, "isBase64Encoded": False,
            "headers": {"stripe-signature": f"t={ts},v1={sig}"}}


def _updated(account_id, *, created=1_800_000_000, charges=True, payouts=True, details=True,
             due=(), livemode=False, account_field=True):
    evt = {"id": f"evt_{created}", "type": "account.updated", "created": created, "livemode": livemode,
           "data": {"object": {"id": account_id, "object": "account", "charges_enabled": charges,
                               "payouts_enabled": payouts, "details_submitted": details,
                               "requirements": {"currently_due": list(due)}}}}
    if account_field:
        evt["account"] = account_id
    return evt


def test_account_updated_enables_charges(app):
    res = app.handler(_event(_updated(ACCT_A)), None)
    assert res["statusCode"] == 200 and json.loads(res["body"])["result"] == "applied"
    stripe = _profile(TENANT_A)
    assert stripe["chargesEnabled"] is True and stripe["payoutsEnabled"] is True
    assert stripe["detailsSubmitted"] is True and stripe["requirementsDue"] == []
    assert stripe["disconnected"] is False and stripe["accountId"] == ACCT_A
    assert stripe["taxRates"] == {}  # untouched
    assert _profile(TENANT_B)["chargesEnabled"] is False


def test_account_id_falls_back_to_object_id(app):
    res = app.handler(_event(_updated(ACCT_A, account_field=False)), None)
    assert json.loads(res["body"])["result"] == "applied"
    assert _profile(TENANT_A)["chargesEnabled"] is True


def test_requirements_due_are_stored(app):
    app.handler(_event(_updated(ACCT_A, charges=False, details=False,
                                due=["external_account", "representative.dob.day"])), None)
    stripe = _profile(TENANT_A)
    assert stripe["chargesEnabled"] is False
    assert stripe["requirementsDue"] == ["external_account", "representative.dob.day"]


def test_older_event_does_not_overwrite_newer(app):
    app.handler(_event(_updated(ACCT_A, created=1_800_000_100, charges=True)), None)
    res = app.handler(_event(_updated(ACCT_A, created=1_800_000_050, charges=False)), None)
    assert json.loads(res["body"])["result"] == "ignored:stale"
    assert _profile(TENANT_A)["chargesEnabled"] is True


def test_same_event_redelivered_is_harmless(app):
    evt = _updated(ACCT_A)
    app.handler(_event(evt), None)
    res = app.handler(_event(evt), None)
    assert res["statusCode"] == 200
    assert _profile(TENANT_A)["chargesEnabled"] is True


def test_unknown_account_is_ignored(app):
    res = app.handler(_event(_updated("acct_1UNKNOWNUNKNOWN0")), None)
    assert res["statusCode"] == 200 and json.loads(res["body"])["result"] == "ignored:unknown_account"


def test_account_claimed_by_another_tenant_cannot_cross_tenants(app):
    # The claim row says A, but A's profile now points at a different account.
    boto3.resource("dynamodb", region_name=REGION).Table(TENANT_TABLE).update_item(
        Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"},
        UpdateExpression="SET stripe.accountId = :a", ExpressionAttributeValues={":a": "acct_1REPLACED0000000"})
    res = app.handler(_event(_updated(ACCT_A)), None)
    assert json.loads(res["body"])["result"] == "ignored:stale"
    assert _profile(TENANT_A)["chargesEnabled"] is False


def test_deauthorized_marks_disconnected(app):
    app.handler(_event(_updated(ACCT_A, created=1_800_000_000)), None)
    evt = {"id": "evt_d", "type": "account.application.deauthorized", "created": 1_800_000_200,
           "livemode": False, "account": ACCT_A, "data": {"object": {"id": "ca_x", "object": "application"}}}
    res = app.handler(_event(evt), None)
    assert json.loads(res["body"])["result"] == "applied"
    stripe = _profile(TENANT_A)
    assert stripe["disconnected"] is True and stripe["chargesEnabled"] is False
    assert stripe["payoutsEnabled"] is False


def test_bad_signature_is_rejected(app):
    res = app.handler(_event(_updated(ACCT_A), signature="0" * 64), None)
    assert res["statusCode"] == 400
    assert _profile(TENANT_A)["chargesEnabled"] is False


def test_billing_secret_does_not_verify_connect(app):
    res = app.handler(_event(_updated(ACCT_A), secret=BILLING_SECRET), None)
    assert res["statusCode"] == 400


def test_old_timestamp_is_rejected(app):
    res = app.handler(_event(_updated(ACCT_A), ts=int(time.time()) - 3600), None)
    assert res["statusCode"] == 400


def test_live_event_in_dev_is_ignored(app):
    res = app.handler(_event(_updated(ACCT_A, livemode=True)), None)
    assert res["statusCode"] == 200 and json.loads(res["body"])["result"] == "ignored:livemode"
    assert _profile(TENANT_A)["chargesEnabled"] is False


def test_test_event_in_prod_is_ignored(app, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    res = app.handler(_event(_updated(ACCT_A)), None)
    assert json.loads(res["body"])["result"] == "ignored:livemode"


def test_billing_events_for_unknown_customers_are_acknowledged(app):
    # The real billing handling is in test_finance_billing.py; here only
    # that /billing verifies with its own secret and answers 200.
    evt = {"id": "evt_b", "type": "customer.subscription.updated", "created": 1, "livemode": False,
           "data": {"object": {"id": "sub_1", "customer": "cus_unknown"}}}
    res = app.handler(_event(evt, path="/billing", secret=BILLING_SECRET), None)
    assert res["statusCode"] == 200 and json.loads(res["body"])["result"] == "ignored:unknown_customer"


def test_other_event_types_are_ignored(app):
    evt = {"id": "evt_x", "type": "capability.updated", "created": 1_800_000_000, "livemode": False,
           "account": ACCT_A, "data": {"object": {"id": "card_payments"}}}
    res = app.handler(_event(evt), None)
    assert json.loads(res["body"])["result"] == "ignored"


def test_unknown_path_is_404(app):
    res = app.handler(_event(_updated(ACCT_A), path="/other"), None)
    assert res["statusCode"] == 404


def test_trailing_slash_and_base64_body(app):
    import base64
    event = _event(_updated(ACCT_A), path="/connect/")
    event["body"] = base64.b64encode(event["body"].encode()).decode()
    event["isBase64Encoded"] = True
    res = app.handler(event, None)
    assert json.loads(res["body"])["result"] == "applied"

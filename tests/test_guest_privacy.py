"""guest-privacy: owners edit the privacy notice and withdraw a guest's
marketing consent; site-config serves the notice and the consent wording."""

import importlib.util
import json
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from shared import consent, privacy_notice
from shared import marketing as m
from shared import tenant as shared_tenant
from tenant_support import (ALL_FEATURES, LOC_A, REGION, TENANT_A, location_row, seed_tenancy, tenant_row,
                            with_tenant_claims)
from test_consent import consent_table

ROOT = Path(__file__).parents[1] / "functions"


def load(name):
    spec = importlib.util.spec_from_file_location(f"gp_{name.replace('-', '_')}", ROOT / name / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def env(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
        monkeypatch.setenv("ENVIRONMENT", "dev")
        seed_tenancy(monkeypatch, tenant_a=tenant_row(
            TENANT_A, name="Bistro Linnea", legalName="Linnea AB", orgNumber="556000-1111",
            contactEmail="hej@linnea.se", contactPhone="+4681234567", slug="linnea",
            address={"street": "Storgatan 1", "postalCode": "11122", "city": "Stockholm"},
            features={**ALL_FEATURES, "marketing": True}),
            locations_a=[location_row(TENANT_A, LOC_A)])
        monkeypatch.setenv("CONSENT_TABLE_NAME", "test-consent")
        monkeypatch.setenv("MARKETING_TABLE_NAME", "test-marketing")
        res = boto3.resource("dynamodb", region_name=REGION)
        log = consent_table(res)
        res.create_table(TableName="test-marketing", BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                                    {"AttributeName": "SK", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                               {"AttributeName": "SK", "AttributeType": "S"}])
        shared_tenant.reset_caches()
        yield {"fn": load("guest-privacy"), "log": log, "marketing": res.Table("test-marketing")}
        shared_tenant.reset_caches()


def call(env, method, path, body=None, query=None, role="owner_user"):
    event = {"routeKey": f"{method} {path}", "requestContext": {"http": {"method": method, "path": path}},
             "queryStringParameters": query, "body": json.dumps(body) if body is not None else None}
    out = env["fn"].handler(with_tenant_claims(event, TENANT_A, role), None)
    return out["statusCode"], json.loads(out["body"]) if out.get("body") else None


def test_staff_cannot_use_it(env):
    assert call(env, "GET", "/privacy/notice", role="staff_user")[0] == 403


def test_template_is_prefilled_with_the_restaurant(env):
    status, body = call(env, "GET", "/privacy/notice")
    assert status == 200 and body["notice"]["custom"] is False
    text = body["template"]
    for part in ("Linnea AB", "556000-1111", "Storgatan 1", "hej@linnea.se", "+4681234567", "Erbjudanden",
                 "13 månader", "7 år"):
        assert part in text
    assert "\u2014" not in text  # house style: no em dash in copy


def test_save_versions_and_reset(env):
    status, body = call(env, "PUT", "/privacy/notice", {"body": "## Vår policy\nText."})
    assert status == 200 and body["notice"] == {**body["notice"], "body": "## Vår policy\nText.", "version": 1,
                                                "custom": True}
    assert call(env, "PUT", "/privacy/notice", {"body": "## Ny\nText 2."})[1]["notice"]["version"] == 2
    assert call(env, "GET", "/privacy/notice")[1]["notice"]["body"] == "## Ny\nText 2."
    status, body = call(env, "DELETE", "/privacy/notice")
    assert status == 200 and body["notice"]["custom"] is False


@pytest.mark.parametrize("raw", ["", "   ", "x" * (privacy_notice.MAX_LENGTH + 1), "<script>alert(1)</script>", 5])
def test_bad_notice_is_refused(env, raw):
    assert call(env, "PUT", "/privacy/notice", {"body": raw})[0] == 400


def test_site_config_serves_the_saved_notice(env, monkeypatch):
    call(env, "PUT", "/privacy/notice", {"body": "## Egen text"})
    boto3.resource("dynamodb", region_name=REGION).Table("test-tenant").put_item(
        Item={"PK": "SLUG#linnea", "SK": "TENANT", "tenantId": TENANT_A})
    site = load("tenant-site-config")
    out = site.handler({"routeKey": "GET /site-config", "queryStringParameters": {"slug": "linnea"},
                        "requestContext": {"http": {"method": "GET"}}}, None)
    privacy = json.loads(out["body"])["privacy"]
    assert privacy["notice"]["body"] == "## Egen text"
    assert privacy["retention"]["bookingMonths"] == 13
    assert privacy["marketingConsent"]["label"] == consent.render(consent.CURRENT, "Bistro Linnea")


def test_lookup_and_withdraw_on_the_guests_behalf(env):
    consent.grant(env["log"], tenant_id=TENANT_A, restaurant="Bistro Linnea", version=consent.CURRENT,
                  channels=["email", "sms"], email="anna@example.se", phone="+46701234567", source="booking")
    env["marketing"].put_item(Item={**m.sub_key(TENANT_A, m.subscriber_id(TENANT_A, "anna@example.se")),
                                    "status": "subscribed", "email": "anna@example.se"})
    status, body = call(env, "GET", "/privacy/consents", query={"email": "Anna@example.se", "phone": "070-123 45 67"})
    assert status == 200 and body["email"]["state"] == "granted" and body["sms"]["state"] == "granted"
    assert body["email"]["history"][0]["text"] == consent.render(consent.CURRENT, "Bistro Linnea")
    status, body = call(env, "POST", "/privacy/consents/withdraw",
                        {"email": "anna@example.se", "phone": "0701234567", "channels": ["email"]})
    assert status == 200 and body["withdrawn"] == ["email"]
    assert body["email"]["state"] == "withdrawn" and body["sms"]["state"] == "granted"
    assert body["email"]["history"][0]["by"] == "restaurant"
    assert len(body["email"]["history"]) == 2  # the grant is still there: nothing overwritten
    sub = env["marketing"].get_item(Key=m.sub_key(TENANT_A, m.subscriber_id(TENANT_A, "anna@example.se")))["Item"]
    assert sub["status"] == "unsubscribed"
    # a second withdrawal adds nothing
    assert call(env, "POST", "/privacy/consents/withdraw",
                {"email": "anna@example.se", "channels": ["email"]})[1]["withdrawn"] == []


def test_lookup_needs_a_contact_detail(env):
    assert call(env, "GET", "/privacy/consents", query={})[0] == 400
    assert call(env, "POST", "/privacy/consents/withdraw", {"email": "a@b.se", "channels": ["fax"]})[0] == 400

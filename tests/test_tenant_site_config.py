"""GET /site-config: hostname (or dev slug) -> the restaurant's public site
configuration. Only active domains of active tenants; only public fields."""

import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from shared import dynamo as shared_dynamo
from shared import tenant as shared_tenant
from tenant_support import (LOC_A, LOC_A2, REGION, TENANT_A, TENANT_B, TENANT_TABLE,
                            LOCATION_TABLE_DEFAULT, seed_tenancy, tenant_row, location_row)

APP = Path(__file__).parents[1] / "functions" / "tenant-site-config" / "app.py"


@pytest.fixture
def site(monkeypatch):
    for key, value in {
        "ENVIRONMENT": "dev", "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": REGION,
        "STRIPE_PUBLISHABLE_KEY": "pk_test_123", "TURNSTILE_SITE_KEY": "0x4AAA",
    }.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        seed_tenancy(
            monkeypatch,
            tenant_a=tenant_row(
                TENANT_A, name="Roma", slug="roma", legalName="Roma AB", orgNumber="556677-8899",
                contactEmail="info@roma.se",
                address={"street": "Drottninggatan 1", "postalCode": "11151", "city": "Stockholm",
                         "country": "SE"},
                ownerEmail="owner-private@roma.se", ownerPhone="+46700000000",
                lastError="boom", stripe={"accountId": "acct_A", "chargesEnabled": True,
                                          "taxRates": {"food": "txr_1"}},
                branding={"primaryColor": "#AA3300", "logoUrl": "https://cdn.roma.se/logo.png",
                          "tagline": "Pizza", "accentColor": "red", "heroImageUrl": "javascript:alert(1)"},
            ),
            tenant_b=tenant_row(TENANT_B, slug="other"),
            locations_a=[
                location_row(TENANT_A, LOC_A2, name="Södermalm", createdAt="2026-10-02T00:00:00Z",
                             stripeAccountId="acct_LOC", bookingDurationHours=Decimal("2")),
                location_row(TENANT_A, LOC_A, name="Hagastan", createdAt="2026-10-01T00:00:00Z",
                             address="Gatan 1", timezone="Europe/Stockholm", phoneNumber="+46812345678",
                             bookingDurationHours=Decimal("1.5"), businessHours={"monday": []}),
            ],
        )
        tenants = boto3.resource("dynamodb", region_name=REGION).Table(TENANT_TABLE)
        for host, tenant_id, status in (("www.roma.se", TENANT_A, "active"),
                                        ("new.roma.se", TENANT_A, "pending_dns"),
                                        ("www.other.se", TENANT_B, "active")):
            tenants.put_item(Item={"PK": f"DOMAIN#{host}", "SK": "TENANT", "tenantId": tenant_id})
            tenants.put_item(Item={"PK": f"TENANT#{tenant_id}", "SK": f"DOMAIN#{host}",
                                   "domain": host, "status": status, "tenantId": tenant_id})
        for slug, tenant_id in (("roma", TENANT_A), ("other", TENANT_B)):
            tenants.put_item(Item={"PK": f"SLUG#{slug}", "SK": "TENANT", "tenantId": tenant_id})
        spec = importlib.util.spec_from_file_location("tenant_site_config_app", APP)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module, tenants
        shared_dynamo._resource = None
        shared_dynamo._client = None
        shared_tenant.reset_caches()


def call(module, query, method="GET"):
    return module.handler({"routeKey": f"{method} /site-config", "queryStringParameters": query}, None)


def test_host_resolves_to_the_restaurant_with_public_fields_only(site):
    module, _ = site

    response = call(module, {"host": "WWW.Roma.se:443"})

    assert response["statusCode"] == 200
    assert response["headers"]["Cache-Control"] == "public, max-age=300"
    body = json.loads(response["body"])
    assert body["tenant"]["name"] == "Roma" and body["tenant"]["slug"] == "roma"
    assert body["tenant"]["features"] == {"reservations": True, "ordering": True,
                                          "catering": True, "terminal": True}
    assert body["tenant"]["branding"] == {"primaryColor": "#aa3300",
                                          "logoUrl": "https://cdn.roma.se/logo.png", "tagline": "Pizza"}
    assert body["legal"]["legalName"] == "Roma AB" and body["legal"]["orgNumber"] == "556677-8899"
    assert body["legal"]["address"] == "Drottninggatan 1, 11151 Stockholm"
    assert [loc["name"] for loc in body["locations"]] == ["Hagastan", "Södermalm"]
    assert body["locations"][0]["bookingDurationHours"] == 1.5
    assert body["stripe"] == {"publishableKey": "pk_test_123", "accountId": "acct_A"}
    assert body["turnstile"] == {"siteKey": "0x4AAA"}
    for secret in ("owner-private", "+46700000000", "boom", "txr_1", "acct_LOC", "chargesEnabled"):
        assert secret not in response["body"]


def test_dev_slug_works_but_not_in_prod(site, monkeypatch):
    module, _ = site
    assert json.loads(call(module, {"slug": "roma"})["body"])["tenant"]["tenantId"] == TENANT_A
    monkeypatch.setenv("ENVIRONMENT", "prod")
    assert call(module, {"slug": "roma"})["statusCode"] == 400


@pytest.mark.parametrize("query", [{"host": "new.roma.se"}, {"host": "unknown.se"}, {"slug": "nope"}])
def test_pending_or_unknown_domains_are_not_found(site, query):
    module, _ = site
    response = call(module, query)
    assert response["statusCode"] == 404 and response["headers"]["Cache-Control"] == "no-store"


def test_suspended_tenant_is_not_found(site):
    module, tenants = site
    tenants.put_item(Item=tenant_row(TENANT_A, status="suspended", slug="roma"))
    assert call(module, {"host": "www.roma.se"})["statusCode"] == 404


@pytest.mark.parametrize("query", [{}, {"host": "a b"}, {"host": "x" * 300}, {"host": "a.se", "slug": "a"},
                                   {"slug": "Bad_Slug"}, {"other": "1"}])
def test_bad_requests(site, query):
    module, _ = site
    assert call(module, query)["statusCode"] == 400


def test_another_tenants_domain_never_returns_this_tenant(site):
    module, _ = site
    body = json.loads(call(module, {"host": "www.other.se"})["body"])
    assert body["tenant"]["tenantId"] == TENANT_B and "Roma" not in json.dumps(body)


def test_method_and_path(site):
    module, _ = site
    assert call(module, {"host": "www.roma.se"}, method="POST")["statusCode"] == 405
    assert module.handler({"routeKey": "GET /other"}, None)["statusCode"] == 404


@pytest.mark.parametrize("raw, shown", [
    ("Gatan 1, Stockholm", "Gatan 1, Stockholm"),
    ({"street": "Main St 1", "city": "Oslo", "country": "NO"}, "Main St 1, Oslo, NO"),
    ({}, None), (None, None), (42, None),
])
def test_address_is_always_one_line_of_text(site, raw, shown):
    module, _ = site
    assert module._address(raw) == shown


def test_card_guarantee_terms_are_public_only_when_stripe_can_charge(site):
    module, tenants = site
    locations = boto3.resource("dynamodb", region_name=REGION).Table(LOCATION_TABLE_DEFAULT)
    locations.update_item(
        Key={"PK": f"TENANT#{TENANT_A}", "SK": f"LOCATION#{LOC_A}"},
        UpdateExpression="SET guarantee = :g",
        ExpressionAttributeValues={":g": {"enabled": True, "minPartySize": Decimal(4),
                                          "noShowFeePerPerson": Decimal(200),
                                          "lateCancelFeePerPerson": Decimal(0),
                                          "cancelCutoffHours": Decimal(12)}})
    shared_tenant.reset_caches()
    body = json.loads(call(module, {"host": "www.roma.se"})["body"])
    haga = next(loc for loc in body["locations"] if loc["locationId"] == LOC_A)
    assert haga["guarantee"] == {"minPartySize": 4, "noShowFeePerPerson": 200,
                                 "lateCancelFeePerPerson": 0, "cancelCutoffHours": 12}
    assert next(loc for loc in body["locations"] if loc["locationId"] == LOC_A2)["guarantee"] is None

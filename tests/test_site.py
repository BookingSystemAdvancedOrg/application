"""shared/site.py: guest links go to the own domain once attached, else the
slug address of this environment, else (dev only) the shared site."""

import boto3
import pytest
from moto import mock_aws

from shared import catering_signing
from shared import dynamo as shared_dynamo
from shared import site
from tenant_support import REGION, TENANT_A, TENANT_TABLE, seed_tenancy, tenant_row

ZONES = {"prod": "booqy.se", "dev": "dev.booqy.se"}
ITEM = {"locationId": "loc-a-1", "requestId": "req-1"}


@pytest.fixture
def tenants(monkeypatch):
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": REGION,
        "CUSTOMER_SITE_URL": "https://dev123.cloudfront.net/",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SITE_DOMAIN", raising=False)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        seed_tenancy(monkeypatch)
        yield boto3.resource("dynamodb", region_name=REGION).Table(TENANT_TABLE)
        shared_dynamo._resource = None
        shared_dynamo._client = None


def env(monkeypatch, environment, *, zone=True):
    monkeypatch.setenv("ENVIRONMENT", environment)
    if zone:
        monkeypatch.setenv("SITE_DOMAIN", ZONES[environment])


def domain(tenants, host, *, kind="custom", status="active"):
    tenants.put_item(Item={"PK": f"TENANT#{TENANT_A}", "SK": f"DOMAIN#{host}",
                           "domain": host, "kind": kind, "status": status})


def slug_row(tenants, environment):
    domain(tenants, f"roma.{ZONES[environment]}", kind="platform")


@pytest.mark.parametrize("environment", ["prod", "dev"])
def test_without_a_domain_links_go_to_the_slug_address(tenants, monkeypatch, environment):
    env(monkeypatch, environment)
    slug_row(tenants, environment)
    row = tenant_row(TENANT_A, slug="roma", primaryDomain=f"roma.{ZONES[environment]}")
    assert site.base(TENANT_A, row) == (f"https://roma.{ZONES[environment]}", None)
    assert site.hosts(TENANT_A, row) == [f"roma.{ZONES[environment]}"]


@pytest.mark.parametrize("environment", ["prod", "dev"])
def test_a_tenant_without_its_slug_row_still_gets_the_slug_address(tenants, monkeypatch, environment):
    env(monkeypatch, environment)
    row = tenant_row(TENANT_A, slug="roma")
    assert site.base(TENANT_A, row) == (f"https://roma.{ZONES[environment]}", None)
    assert site.hosts(TENANT_A, row) == [f"roma.{ZONES[environment]}"]


@pytest.mark.parametrize("environment", ["prod", "dev"])
def test_an_attached_domain_wins_over_the_slug_address(tenants, monkeypatch, environment):
    env(monkeypatch, environment)
    slug_row(tenants, environment)
    # onboarding set primaryDomain to the slug address; the own domain still wins
    row = tenant_row(TENANT_A, slug="roma", primaryDomain=f"roma.{ZONES[environment]}")
    domain(tenants, "www.roma.se", status="pending_dns")
    assert site.base(TENANT_A, row)[0] == f"https://roma.{ZONES[environment]}"
    domain(tenants, "www.roma.se")
    assert site.base(TENANT_A, row) == ("https://www.roma.se", None)
    assert site.hosts(TENANT_A, row) == sorted([f"roma.{ZONES[environment]}", "www.roma.se"])


def test_the_primary_own_domain_comes_first(tenants, monkeypatch):
    env(monkeypatch, "prod")
    domain(tenants, "roma.example.app")
    domain(tenants, "www.roma.se")
    assert site.base(TENANT_A, tenant_row(TENANT_A, slug="roma", primaryDomain="www.roma.se"))[0] == \
        "https://www.roma.se"
    assert site.base(TENANT_A, tenant_row(TENANT_A, slug="roma"))[0] == "https://roma.example.app"


def test_an_old_domain_row_without_kind_counts_as_own(tenants, monkeypatch):
    env(monkeypatch, "prod")
    tenants.put_item(Item={"PK": f"TENANT#{TENANT_A}", "SK": "DOMAIN#www.roma.se",
                           "domain": "www.roma.se", "status": "active"})
    assert site.base(TENANT_A, tenant_row(TENANT_A, slug="roma"))[0] == "https://www.roma.se"


def test_dev_without_subdomains_falls_back_to_the_shared_site(tenants, monkeypatch):
    env(monkeypatch, "dev", zone=False)
    row = tenant_row(TENANT_A, slug="roma")
    assert site.base(TENANT_A, row) == ("https://dev123.cloudfront.net", "roma")
    assert site.hosts(TENANT_A, row) == ["dev123.cloudfront.net"]
    assert site.page(*site.base(TENANT_A, row), "/bestall") == "https://dev123.cloudfront.net/bestall?restaurang=roma"


def test_prod_without_subdomains_or_domain_has_no_site(tenants, monkeypatch):
    env(monkeypatch, "prod", zone=False)
    row = tenant_row(TENANT_A, slug="roma")
    assert site.base(TENANT_A, row) == (None, None)
    assert site.hosts(TENANT_A, row) == []


def test_a_bad_slug_is_never_put_in_a_host(tenants, monkeypatch):
    env(monkeypatch, "prod")
    assert site.base(TENANT_A, tenant_row(TENANT_A, slug="evil.example/x")) == (None, None)


@pytest.mark.parametrize("environment", ["prod", "dev"])
def test_scrive_return_url_without_a_domain(tenants, monkeypatch, environment):
    env(monkeypatch, environment)
    slug_row(tenants, environment)
    url = catering_signing.customer_return_url(TENANT_A, tenant_row(TENANT_A, slug="roma"), ITEM)
    assert url == f"https://roma.{ZONES[environment]}/catering/loc-a-1/req-1?signering=klar"


@pytest.mark.parametrize("environment", ["prod", "dev"])
def test_scrive_return_url_with_a_domain(tenants, monkeypatch, environment):
    env(monkeypatch, environment)
    slug_row(tenants, environment)
    domain(tenants, "www.roma.se")
    url = catering_signing.customer_return_url(TENANT_A, tenant_row(TENANT_A, slug="roma"), ITEM)
    assert url == "https://www.roma.se/catering/loc-a-1/req-1?signering=klar"

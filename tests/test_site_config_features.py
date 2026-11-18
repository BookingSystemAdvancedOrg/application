"""/site-config Run 0 additions (CONTRACTS.md 7): locations[].waitlist,
present only when the feature is on and the attribute is written."""

import json

import boto3
import pytest

from shared import tenant as shared_tenant
from tenant_support import (LOC_A, LOC_B, LOCATION_TABLE_DEFAULT, REGION, TENANT_A, TENANT_B, features_on,
                            location_row, tenant_row)
from test_tenant_site_config import call, site  # fixture `site` is used by name

def _set(tenants, tenant_id, slug, features, **extra):
    tenants.put_item(Item=tenant_row(tenant_id, slug=slug, features=features, **extra))
    shared_tenant.reset_caches()


def _locations(table_rows):
    table = boto3.resource("dynamodb", region_name=REGION).Table(LOCATION_TABLE_DEFAULT)
    for row in table_rows:
        table.put_item(Item=row)


def _body(module, slug="roma"):
    response = call(module, {"slug": slug})
    assert response["statusCode"] == 200
    return json.loads(response["body"])


def _hagastan(body):
    return next(loc for loc in body["locations"] if loc["locationId"] == LOC_A)


def test_waitlist_absent_when_off_or_not_written(site):
    module, tenants = site
    _set(tenants, TENANT_A, "roma", features_on())
    _locations([location_row(TENANT_A, LOC_A, waitlist={"publicJoin": True})])
    assert all("waitlist" not in loc for loc in _body(module)["locations"])
    _set(tenants, TENANT_A, "roma", features_on("waitlist"))
    _locations([location_row(TENANT_A, LOC_A)])
    assert all("waitlist" not in loc for loc in _body(module)["locations"])


def test_waitlist_present_when_on_and_written(site):
    module, tenants = site
    _set(tenants, TENANT_A, "roma", features_on("waitlist"))
    _locations([location_row(TENANT_A, LOC_A, waitlist={"publicJoin": True, "secret": "x"})])
    body = _body(module)
    assert _hagastan(body)["waitlist"] == {"publicJoin": True}
    assert "secret" not in json.dumps(body)


def test_removed_site_builder_leftovers_never_show(site):
    """The site builder was removed: a stored siteBuilder entitlement and a
    PROFILE.site pointer from it are ignored, never sent to the guest site."""
    module, tenants = site
    row = tenant_row(TENANT_A, slug="roma", features=features_on(), site={"mode": "builder", "version": 3})
    row["entitlements"]["features"]["siteBuilder"] = True
    tenants.put_item(Item=row)
    shared_tenant.reset_caches()
    body = _body(module)
    assert "site" not in body["tenant"] and "siteBuilder" not in body["tenant"]["features"]


def test_another_tenants_waitlist_never_shows(site):
    module, tenants = site
    _set(tenants, TENANT_A, "roma", features_on())
    _set(tenants, TENANT_B, "other", features_on("waitlist"))
    _locations([location_row(TENANT_B, LOC_B, waitlist={"publicJoin": True})])
    body = _body(module, "roma")
    assert body["tenant"]["tenantId"] == TENANT_A
    assert all("waitlist" not in loc for loc in body["locations"])
    other = _body(module, "other")
    assert other["locations"][0]["waitlist"] == {"publicJoin": True}


@pytest.mark.parametrize("features, expected", [(None, False), (features_on(), False), (features_on("events"), True)])
def test_events_flag_follows_the_entitlement(site, features, expected):
    """Run 0 addendum (ADR-K): tenant.features.events, and nothing else."""
    module, tenants = site
    _set(tenants, TENANT_A, "roma", features)
    body = _body(module)
    assert body["tenant"]["features"]["events"] is expected
    assert "phoneAgent" not in body["tenant"]["features"]
    assert not any(k.startswith("event") for k in body["tenant"] if k != "features")


def test_events_on_for_another_tenant_does_not_leak(site):
    module, tenants = site
    _set(tenants, TENANT_A, "roma", features_on())
    _set(tenants, TENANT_B, "other", features_on("events"))
    assert _body(module, "roma")["tenant"]["features"]["events"] is False
    assert _body(module, "other")["tenant"]["features"]["events"] is True

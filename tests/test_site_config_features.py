"""/site-config Run 0 additions (CONTRACTS.md 7): tenant.site and
locations[].waitlist, present only when the feature is on and the
attribute is written."""

import json

import boto3
import pytest

from shared import tenant as shared_tenant
from tenant_support import (LOC_A, LOC_B, LOCATION_TABLE_DEFAULT, REGION, TENANT_A, TENANT_B, features_on,
                            location_row, tenant_row)
from test_tenant_site_config import call, site  # fixture `site` is used by name

SITE = {"mode": "builder", "version": 3}


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


@pytest.mark.parametrize("features, with_attrs", [(None, True), (features_on(), True),
                                                   (features_on("siteBuilder", "waitlist"), False)])
def test_fields_absent_when_off_or_not_written(site, features, with_attrs):
    module, tenants = site
    extra = {"site": SITE} if with_attrs else {}
    _set(tenants, TENANT_A, "roma", features, **extra)
    if with_attrs:
        _locations([location_row(TENANT_A, LOC_A, waitlist={"publicJoin": True})])
    body = _body(module)
    assert "site" not in body["tenant"]
    assert all("waitlist" not in loc for loc in body["locations"])


def test_fields_present_when_on_and_written(site):
    module, tenants = site
    _set(tenants, TENANT_A, "roma", features_on("siteBuilder", "waitlist"), site=SITE)
    _locations([location_row(TENANT_A, LOC_A, waitlist={"publicJoin": True, "secret": "x"})])
    body = _body(module)
    assert body["tenant"]["site"] == {"mode": "builder", "version": 3}
    assert _hagastan(body)["waitlist"] == {"publicJoin": True}
    assert "secret" not in json.dumps(body)


@pytest.mark.parametrize("bad", [{"mode": "other", "version": 1}, {"mode": "custom", "version": "x"},
                                 {"mode": "custom", "version": -1}, "builder"])
def test_malformed_site_is_left_out(site, bad):
    module, tenants = site
    _set(tenants, TENANT_A, "roma", features_on("siteBuilder"), site=bad)
    assert "site" not in _body(module)["tenant"]


def test_another_tenants_site_and_waitlist_never_show(site):
    module, tenants = site
    _set(tenants, TENANT_A, "roma", features_on())
    _set(tenants, TENANT_B, "other", features_on("siteBuilder", "waitlist"), site=SITE)
    _locations([location_row(TENANT_B, LOC_B, waitlist={"publicJoin": True})])
    body = _body(module, "roma")
    assert body["tenant"]["tenantId"] == TENANT_A and "site" not in body["tenant"]
    assert all("waitlist" not in loc for loc in body["locations"])
    other = _body(module, "other")
    assert other["tenant"]["site"] == SITE and other["locations"][0]["waitlist"] == {"publicJoin": True}

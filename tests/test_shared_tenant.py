"""The tenant rules every handler relies on (shared/tenant.py)."""

import json

import boto3
import pytest
from moto import mock_aws

from shared import dynamo as shared_dynamo
from shared import tenant
from tenant_support import (LOC_A, LOC_A2, LOC_B, STAFF_A_SUB, TENANT_A, TENANT_B, TENANT_TABLE,
                            REGION, location_row, seed_tenancy, tenant_row, with_tenant_claims)


@pytest.fixture
def aws(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                 "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        seed_tenancy(monkeypatch)
        yield
        shared_dynamo._resource = None
        shared_dynamo._client = None
        tenant.reset_caches()


def ev(tenant_id=TENANT_A, role="owner_user", sub=None):
    return with_tenant_claims({}, tenant_id, role, sub)


def err(fn, *a, **kw):
    with pytest.raises(tenant.TenantError) as exc:
        fn(*a, **kw)
    return exc.value.status, exc.value.error


def test_owner_gets_own_location(aws):
    ctx = tenant.for_jwt(ev(), location_id=LOC_A)
    assert ctx.tenant_id == TENANT_A and ctx.location_id == LOC_A and ctx.is_owner
    assert ctx.location_key == {"PK": f"TENANT#{TENANT_A}", "SK": f"LOCATION#{LOC_A}"}


def test_other_tenants_location_is_404_not_403(aws):
    # B's location must look exactly like a location that doesn't exist.
    assert err(tenant.for_jwt, ev(TENANT_A), location_id=LOC_B) == (404, "not found")
    assert err(tenant.for_jwt, ev(TENANT_A), location_id="nope") == (404, "not found")
    assert err(tenant.for_jwt, ev(TENANT_B), location_id=LOC_A) == (404, "not found")


def test_tenant_only_from_claims(aws):
    event = ev(TENANT_A)
    event["pathParameters"] = {"tenantId": TENANT_B}
    event["body"] = json.dumps({"tenantId": TENANT_B})
    event["headers"] = {"x-tenant-id": TENANT_B}
    assert tenant.for_jwt(event).tenant_id == TENANT_A


@pytest.mark.parametrize("claims", [
    {},                                                    # no tenant at all (old super_user tokens)
    {"sub": "x", "role": "owner_user"},                    # role but no tenant
    {"sub": "x", "tenant_id": TENANT_A, "cognito:groups": ["super_user"]},  # super_user is gone
    {"sub": "x", "tenant_id": TENANT_A, "role": "admin"},  # unknown role
])
def test_tokens_without_tenant_or_role_are_refused(aws, claims):
    event = {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}
    assert err(tenant.for_jwt, event) == (403, "no_tenant")


def test_missing_authorizer_is_401(aws):
    assert err(tenant.for_jwt, {}) == (401, "unauthorized")


def test_role_from_group_when_claim_missing(aws):
    event = {"requestContext": {"authorizer": {"jwt": {"claims": {
        "sub": "s", "tenant_id": TENANT_A, "cognito:groups": "[owner_user]"}}}}}
    assert tenant.for_jwt(event).role == "owner_user"


@pytest.mark.parametrize("status", ["provisioning", "suspended", "offboarding", "offboarded",
                                    "provisioning_failed"])
def test_inactive_tenant_is_refused(aws, status):
    boto3.resource("dynamodb", region_name=REGION).Table(TENANT_TABLE).put_item(
        Item=tenant_row(TENANT_A, status=status))
    tenant.reset_caches()
    assert err(tenant.for_jwt, ev(), location_id=LOC_A) == (403, "tenant_inactive")
    assert err(tenant.for_public, LOC_A) == (404, "not found")


def test_unknown_tenant_is_refused(aws):
    assert err(tenant.for_jwt, ev("01zzzzzzzzzzzzzzzzzzzzzzzz")) == (403, "tenant_inactive")


def test_feature_gate(aws):
    boto3.resource("dynamodb", region_name=REGION).Table(TENANT_TABLE).put_item(
        Item=tenant_row(TENANT_A, features={"reservations": False, "ordering": True}))
    tenant.reset_caches()
    assert err(tenant.for_jwt, ev(), location_id=LOC_A, feature="reservations") == (403, "feature_not_in_plan")
    assert tenant.for_jwt(ev(), location_id=LOC_A, feature="ordering").tenant_id == TENANT_A
    assert err(tenant.for_public, LOC_A, feature="reservations") == (404, "not found")


def test_owner_only(aws):
    assert err(tenant.for_jwt, ev(role="staff_user"), owner_only=True) == (403, "owner_only")
    assert tenant.for_jwt(ev(), owner_only=True).is_owner


def test_staff_limited_to_assigned_location(aws, monkeypatch):
    monkeypatch.setenv("USER_TABLE_NAME", "test-user")
    assert tenant.for_jwt(ev(role="staff_user"), location_id=LOC_A).sub == STAFF_A_SUB
    assert err(tenant.for_jwt, ev(role="staff_user"), location_id=LOC_A2) == (404, "not found")


def test_public_route_resolves_tenant_from_location(aws):
    ctx = tenant.for_public(LOC_B)
    assert ctx.tenant_id == TENANT_B and ctx.role is None and ctx.location_id == LOC_B
    assert err(tenant.for_public, "nope") == (404, "not found")
    assert err(tenant.for_public, "") == (404, "not found")


def test_list_locations_only_own_partition(aws):
    ids = sorted(i["locationId"] for i in tenant.list_locations(TENANT_A))
    assert ids == [LOC_A, LOC_A2]
    assert [i["locationId"] for i in tenant.list_locations(TENANT_B)] == [LOC_B]


def test_suspension_takes_effect_after_cache_ttl(aws, monkeypatch):
    monkeypatch.setenv("TENANT_CACHE_SECONDS", "0")
    assert tenant.for_jwt(ev()).tenant_id == TENANT_A
    boto3.resource("dynamodb", region_name=REGION).Table(TENANT_TABLE).put_item(
        Item=tenant_row(TENANT_A, status="suspended"))
    assert err(tenant.for_jwt, ev()) == (403, "tenant_inactive")


def test_stripe_account_prefers_location_override(aws):
    res = boto3.resource("dynamodb", region_name=REGION)
    res.Table(TENANT_TABLE).put_item(Item=tenant_row(TENANT_A, stripe={"accountId": "acct_tenant"}))
    res.Table("test-location").put_item(Item=location_row(TENANT_A, LOC_A2, stripeAccountId="acct_loc"))
    tenant.reset_caches()
    assert tenant.for_jwt(ev(), location_id=LOC_A).stripe_account() == "acct_tenant"
    assert tenant.for_jwt(ev(), location_id=LOC_A2).stripe_account() == "acct_loc"


def test_error_response_shape(aws):
    try:
        tenant.for_jwt(ev(TENANT_A), location_id=LOC_B)
    except tenant.TenantError as exc:
        r = exc.response()
    assert r["statusCode"] == 404 and json.loads(r["body"]) == {"error": "not found"}


def test_old_platform_row_with_same_id_does_not_confuse_resolution(aws):
    boto3.resource("dynamodb", region_name=REGION).Table("test-location").put_item(
        Item={"PK": "PLATFORM", "SK": f"LOCATION#{LOC_B}", "locationId": LOC_B, "name": "old"})
    assert tenant.for_public(LOC_B).tenant_id == TENANT_B


def test_location_id_claimed_by_two_tenants_is_refused(aws):
    boto3.resource("dynamodb", region_name=REGION).Table("test-location").put_item(
        Item=location_row(TENANT_A, LOC_B))
    tenant.reset_caches()
    assert err(tenant.for_public, LOC_B) == (404, "not found")


def test_row_whose_tenant_attribute_disagrees_with_its_key_is_ignored(aws):
    boto3.resource("dynamodb", region_name=REGION).Table("test-location").put_item(
        Item={**location_row(TENANT_A, "forged"), "tenantId": TENANT_B})
    assert err(tenant.for_public, "forged") == (404, "not found")


def test_disabled_or_foreign_staff_profile_gets_no_location(aws, monkeypatch):
    monkeypatch.setenv("USER_TABLE_NAME", "test-user")
    users = boto3.resource("dynamodb", region_name=REGION).Table("test-user")
    row = users.get_item(Key={"PK": f"USER#{STAFF_A_SUB}", "SK": "PROFILE"})["Item"]
    users.put_item(Item={**row, "status": "disabled"})
    assert err(tenant.for_jwt, ev(role="staff_user"), location_id=LOC_A) == (404, "not found")
    users.put_item(Item={**row, "tenantId": TENANT_B})
    assert err(tenant.for_jwt, ev(role="staff_user"), location_id=LOC_A) == (404, "not found")


def test_unknown_tenant_is_not_cached(aws):
    new = "01cccccccccccccccccccccccc"
    assert err(tenant.for_jwt, ev(new)) == (403, "tenant_inactive")
    boto3.resource("dynamodb", region_name=REGION).Table(TENANT_TABLE).put_item(Item=tenant_row(new))
    assert tenant.for_jwt(ev(new)).tenant_id == new  # onboarded a moment ago


def test_fresh_read_and_invalidate(aws):
    tenant.get_tenant(TENANT_A)
    boto3.resource("dynamodb", region_name=REGION).Table(TENANT_TABLE).put_item(
        Item=tenant_row(TENANT_A, name="Renamed"))
    assert tenant.get_tenant(TENANT_A)["name"] != "Renamed"          # cached
    assert tenant.get_tenant(TENANT_A, fresh=True)["name"] == "Renamed"
    tenant.invalidate(TENANT_A)
    assert tenant.get_tenant(TENANT_A)["name"] == "Renamed"

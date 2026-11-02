"""Feature keys from the contract, any-of gates and plan limits (shared/tenant.py)."""

import pytest
from moto import mock_aws

from shared import contracts
from shared import dynamo as shared_dynamo
from shared import tenant
from tenant_support import (ALL_FEATURES, LOC_A, LOC_B, NEW_FEATURES, REGION, TENANT_A, TENANT_B,
                            features_on, seed_tenancy, tenant_row, with_tenant_claims)


@pytest.fixture
def aws(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                 "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        seed_tenancy(monkeypatch, features_a=features_on("socialPosting"), features_b=features_on())
        yield
        shared_dynamo._resource = None
        shared_dynamo._client = None
        tenant.reset_caches()


def err(fn, *a, **kw):
    with pytest.raises(tenant.TenantError) as exc:
        fn(*a, **kw)
    return exc.value.status, exc.value.error


def test_features_come_from_the_contract():
    assert tenant.FEATURES == contracts.FEATURE_KEYS
    assert {"marketing", "kassa"} <= set(tenant.FEATURES)


def test_any_of_gate_on_jwt(aws):
    a = with_tenant_claims({}, TENANT_A)
    b = with_tenant_claims({}, TENANT_B)
    assert tenant.for_jwt(a, feature=("metaAds", "socialPosting")).tenant_id == TENANT_A
    assert err(tenant.for_jwt, b, feature=("metaAds", "socialPosting")) == (403, "feature_not_in_plan")
    assert err(tenant.for_jwt, a, feature=()) == (403, "feature_not_in_plan")
    assert err(tenant.for_jwt, a, feature="metaAds") == (403, "feature_not_in_plan")
    assert tenant.for_jwt(a, feature="reservations").tenant_id == TENANT_A


def test_any_of_gate_on_public(aws):
    assert tenant.for_public(LOC_A, feature=("waitlist", "socialPosting")).tenant_id == TENANT_A
    assert err(tenant.for_public, LOC_B, feature=("waitlist", "socialPosting")) == (404, "not found")


def test_limit_default_stored_and_unknown():
    assert tenant.limit(tenant_row(TENANT_A), "siteBuilderAiEditsPerMonth") == 0
    assert tenant.limit({}, "siteBuilderAiEditsPerMonth") == 0
    row = tenant_row(TENANT_A, limits={"siteBuilderAiEditsPerMonth": 10})
    assert tenant.limit(row, "siteBuilderAiEditsPerMonth") == 10
    with pytest.raises(KeyError):
        tenant.limit(row, "aiEditsPerMonth")


def test_fixture_defaults_keep_every_new_feature_off():
    assert set(NEW_FEATURES) == set(contracts.NEW_FEATURE_KEYS) and not any(NEW_FEATURES.values())
    assert tenant_row(TENANT_A)["entitlements"] == {"maxLocations": 3, "features": ALL_FEATURES}
    assert features_on("giftCards")["giftCards"] is True
    assert not features_on().get("kassa")
    with pytest.raises(KeyError):
        features_on("giftCard")


def test_addendum_keys_and_phone_minutes_limit():
    """Run 0 addendum (ADR-K, ADR-L): both keys off in the fixtures, gates
    refuse them, and the minutes limit defaults to 0 like every limit."""
    assert len(NEW_FEATURES) == 12 and NEW_FEATURES["events"] is False and NEW_FEATURES["phoneAgent"] is False
    assert features_on("phoneAgent")["phoneAgent"] is True and features_on("phoneAgent")["events"] is False
    assert tenant.limit(tenant_row(TENANT_A), "phoneAgentMinutesPerMonth") == 0
    assert tenant.limit({}, "phoneAgentMinutesPerMonth") == 0
    row = tenant_row(TENANT_A, limits={"phoneAgentMinutesPerMonth": 300})
    assert row["entitlements"]["limits"] == {"phoneAgentMinutesPerMonth": 300}
    assert tenant.limit(row, "phoneAgentMinutesPerMonth") == 300
    assert tenant.limit(row, "siteBuilderAiEditsPerMonth") == 0


def test_addendum_features_refused_when_off(aws):
    a = with_tenant_claims({}, TENANT_A)
    assert err(tenant.for_jwt, a, feature="events") == (403, "feature_not_in_plan")
    assert err(tenant.for_jwt, a, feature="phoneAgent") == (403, "feature_not_in_plan")
    assert err(tenant.for_public, LOC_A, feature="events") == (404, "not found")

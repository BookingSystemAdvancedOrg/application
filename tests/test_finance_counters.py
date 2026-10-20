"""shared/finance.py counters and the per-request tenant log line."""

import logging

import boto3
import pytest
from moto import mock_aws

from shared import dynamo as shared_dynamo
from shared import finance, tenant
from tenant_support import LOC_A, REGION, TENANT_A, location_row, seed_tenancy, tenant_row, with_tenant_claims

TABLE = "test-finance"


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        tenant.reset_caches()
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(TableName=TABLE, BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                               {"AttributeName": "SK", "AttributeType": "S"}])
        seed_tenancy(monkeypatch, tenant_a=tenant_row(TENANT_A), locations_a=[location_row(TENANT_A, LOC_A)])
        yield boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
        shared_dynamo._resource = None
        shared_dynamo._client = None
        tenant.reset_caches()


def test_count_adds_to_the_day_row(aws, monkeypatch):
    monkeypatch.setenv("FINANCE_TABLE_NAME", TABLE)
    assert finance.count(TENANT_A, emails=1, day="2026-10-19") is True
    assert finance.count(TENANT_A, emails=2, sms=1, day="2026-10-19") is True
    row = aws.get_item(Key={"PK": f"TENANT#{TENANT_A}", "SK": "DAY#2026-10-19"})["Item"]
    assert row["emails"] == 3 and row["sms"] == 1 and row["countedAt"]


def test_count_is_off_without_a_table(aws, monkeypatch):
    monkeypatch.delenv("FINANCE_TABLE_NAME", raising=False)
    assert finance.count(TENANT_A, emails=1) is False


def test_count_ignores_unknown_counters_and_never_raises(aws, monkeypatch):
    monkeypatch.setenv("FINANCE_TABLE_NAME", TABLE)
    assert finance.count(TENANT_A, bogus=4) is False
    monkeypatch.setenv("FINANCE_TABLE_NAME", "no-such-table")
    assert finance.count(TENANT_A, sms=1) is False


def test_resolved_requests_log_the_tenant(aws, caplog):
    caplog.set_level(logging.INFO, logger="shared.tenant.requests")
    tenant.for_jwt(with_tenant_claims({}, TENANT_A), location_id=LOC_A)
    tenant.for_public(LOC_A)
    lines = [r.getMessage() for r in caplog.records if r.name == "shared.tenant.requests"]
    assert lines == [f"TENANT_REQUEST tenant={TENANT_A} kind=jwt", f"TENANT_REQUEST tenant={TENANT_A} kind=public"]


def test_refused_requests_log_nothing(aws, caplog):
    caplog.set_level(logging.INFO, logger="shared.tenant.requests")
    with pytest.raises(tenant.TenantError):
        tenant.for_public("no-such-location")
    assert not [r for r in caplog.records if r.name == "shared.tenant.requests"]

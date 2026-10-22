"""tenant-purge also deletes an offboarded restaurant's consent log, and
leaves every other restaurant's records alone."""

from datetime import timedelta

import boto3

from shared import consent
from test_consent import consent_table
from test_tenant_purge import (GONE, KEEP, LOC_GONE, LOC_KEEP, NOW, REGION, Context, env,  # noqa: F401
                               seed_tenant)


def test_consent_log_is_purged_with_the_restaurant(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CONSENT_TABLE_NAME", "test-consent")
    log = consent_table(boto3.resource("dynamodb", region_name=REGION))
    for tenant_id in (GONE, KEEP):
        consent.grant(log, tenant_id=tenant_id, restaurant="R", version=consent.CURRENT, channels=["email", "sms"],
                      email="a@b.se", phone="+46701234567", source="booking")
    seed_tenant(env, GONE, LOC_GONE, status="offboarded", purge_at=NOW - timedelta(minutes=1),
                offboarded_at=NOW - timedelta(days=90), slug="gone")
    seed_tenant(env, KEEP, LOC_KEEP, slug="keep")
    assert env["app"].handler({}, Context())["purged"] == [GONE]
    assert {i["tenantId"] for i in log.scan()["Items"]} == {KEEP}
    assert len(log.scan()["Items"]) == 2

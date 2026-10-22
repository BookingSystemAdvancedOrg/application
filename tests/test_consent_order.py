"""Orders and the consent log: an order never needs consent; a ticked box is
logged once the order exists."""

import boto3
import pytest

from shared import consent, tenant as shared_tenant
from tenant_support import ALL_FEATURES, TENANT_A, TENANT_TABLE, tenant_row
from test_consent import consent_table
from test_orders import ACCOUNT, REGION, body_of, env, post_order, stored  # noqa: F401 - env is a fixture

CURRENT = {"channels": ["email", "sms"], "textVersion": consent.CURRENT}


@pytest.fixture
def log(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CONSENT_TABLE_NAME", "test-consent")
    table = consent_table(boto3.resource("dynamodb", region_name=REGION))
    env["res"].Table(TENANT_TABLE).put_item(Item=tenant_row(
        TENANT_A, name="Roma", slug="roma", stripe={"accountId": ACCOUNT, "chargesEnabled": True},
        legalName="Roma AB", orgNumber="556000-0000", notifications={"sms": False},
        features={**ALL_FEATURES, "marketing": True}))
    shared_tenant.reset_caches()
    return table


def test_order_without_consent(env, log):  # noqa: F811
    response = post_order(env)
    assert response["statusCode"] == 201
    assert log.scan()["Items"] == []
    assert "marketingOptIn" not in stored(env, body_of(response)["orderId"])


def test_order_with_consent_is_logged_with_its_id(env, log):  # noqa: F811
    response = post_order(env, marketingConsent=CURRENT)
    order_id = body_of(response)["orderId"]
    items = sorted(log.scan()["Items"], key=lambda i: i["channel"])
    assert [(i["channel"], i["source"], i["ref"]) for i in items] == [("email", "order", order_id),
                                                                        ("sms", "order", order_id)]
    assert stored(env, order_id)["marketingOptIn"] is True


def test_refused_order_logs_nothing(env, log):  # noqa: F811
    response = post_order(env, marketingConsent=CURRENT, items=[{"menuItemId": "old", "quantity": 1}])
    assert response["statusCode"] in (400, 409)
    assert log.scan()["Items"] == []

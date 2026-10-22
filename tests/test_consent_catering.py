"""Catering requests and the consent log: a request never needs consent; a
ticked box is logged with the request id (only with marketing on)."""

import json

import boto3

from shared import consent, tenant as shared_tenant
from tenant_support import TENANT_A, TENANT_TABLE, tenant_row
from test_catering import ACCOUNT, REGION, create, env  # noqa: F401 - env is a fixture
from test_consent import consent_table

CURRENT = {"channels": ["email", "sms"], "textVersion": consent.CURRENT}


def _log(env, marketing=True):  # noqa: F811
    env.monkeypatch.setenv("CONSENT_TABLE_NAME", "test-consent")
    table = consent_table(boto3.resource("dynamodb", region_name=REGION))
    features = {"catering": True, "marketing": marketing}
    env.res.Table(TENANT_TABLE).put_item(Item=tenant_row(
        TENANT_A, name="Roma", slug="roma", stripe={"accountId": ACCOUNT, "chargesEnabled": True}, features=features))
    shared_tenant.reset_caches()
    return table


def test_request_without_consent_logs_nothing(env):  # noqa: F811
    log = _log(env)
    assert create(env)["statusCode"] == 201
    assert log.scan()["Items"] == []


def test_ticked_box_is_logged_per_channel_with_the_request_id(env):  # noqa: F811
    log = _log(env)
    response = create(env, marketingConsent=CURRENT)
    assert response["statusCode"] == 201
    request_id = json.loads(response["body"])["requestId"]
    items = sorted(log.scan()["Items"], key=lambda i: i["channel"])
    assert [(i["channel"], i["source"], i["ref"]) for i in items] == [("email", "catering", request_id),
                                                                        ("sms", "catering", request_id)]
    assert items[0]["text"] == consent.render(consent.CURRENT, "Roma")


def test_no_log_without_marketing_and_bad_wording_refused(env):  # noqa: F811
    log = _log(env, marketing=False)
    assert create(env, marketingConsent=CURRENT)["statusCode"] == 201
    assert log.scan()["Items"] == []
    assert create(env, marketingConsent={"channels": ["email"], "textVersion": "x"})["statusCode"] == 400

"""Audit #21: a guest erasure removes the guest's address from the consent
log at once, without waiting for data-retention (dry run in prod)."""

from unittest import mock

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from shared import consent
from tenant_support import TENANT_A
from test_guest_privacy import call, env, guest  # noqa: F401  (fixtures)


def _records(env, channel, identifier):
    pk = consent.pk(TENANT_A, consent.subject(TENANT_A, channel, identifier))
    return env["log"].query(KeyConditionExpression=Key("PK").eq(pk))["Items"]


def test_erase_leaves_only_a_suppression_record_without_the_address(env, guest):  # noqa: F811
    consent.withdraw(env["log"], tenant_id=TENANT_A, channel="email", identifier="anna@example.se",
                     source="withdraw_page")
    consent.grant(env["log"], tenant_id=TENANT_A, restaurant="Bistro Linnea", version=consent.CURRENT,
                  channels=["email"], email="anna@example.se", source="order")
    assert len(_records(env, "email", "anna@example.se")) == 3
    status, body = call(env, "POST", "/privacy/guests/erase", {"email": "anna@example.se"})
    assert status == 200 and body["consentWithdrawn"] == ["email"]
    (only,) = _records(env, "email", "anna@example.se")
    assert only["action"] == consent.SUPPRESSED and only["SK"].endswith("#S") and int(only["ttl"]) > 0
    assert "identifier" not in only and "actor" not in only
    assert not consent.may_send(env["log"], TENANT_A, "email", "anna@example.se")
    # Again: the suppression stays the only record.
    assert call(env, "POST", "/privacy/guests/erase", {"email": "anna@example.se"})[0] == 200
    (again,) = _records(env, "email", "anna@example.se")
    assert again["SK"] == only["SK"]


def test_erase_without_delete_permission_falls_back_to_the_withdrawal(env, guest):  # noqa: F811
    denied = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "DeleteItem")
    real = env["fn"].dynamo.table

    def table(name):
        t = real(name)
        if name == "test-consent":
            t = mock.Mock(wraps=t)
            t.delete_item.side_effect = denied
        return t

    with mock.patch.object(env["fn"].dynamo, "table", side_effect=table):
        status, body = call(env, "POST", "/privacy/guests/erase", {"email": "anna@example.se"})
    assert status == 200 and body["consentWithdrawn"] == ["email"]
    latest = consent.latest(env["log"], TENANT_A, consent.subject(TENANT_A, "email", "anna@example.se"))
    # As before the fix: a withdrawal by subject that data-retention collapses later.
    assert latest["action"] == consent.WITHDRAW and latest["source"] == "erasure" and "identifier" not in latest

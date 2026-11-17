"""Audit #36 and #37: marketing-send stops (paused, SCHEDULE row removed)
instead of retrying forever when a segment export is gone, and never sends
for a tenant that is not active or no longer has marketing."""

import boto3
import pytest

from hooks_fake import install_hook
from shared import marketing as m
from shared import tenant as shared_tenant
from tenant_support import REGION, TENANT_A
from test_marketing import admin, body_of, draft, env, load, make_subscribers, outbox  # env: fixture
from test_marketing_segment import CTX, guest_crm_on, segment_campaign, segment_hook, sid


def stored(env, campaign_id):
    return env.table.get_item(Key={"PK": m.cmp_pk(TENANT_A), "SK": f"CMP#{campaign_id}"})["Item"]


def schedule_rows(env):
    return env.table.query(KeyConditionExpression="PK = :p",
                           ExpressionAttributeValues={":p": "SCHEDULE"}).get("Items") or []


def queue_segment(env, monkeypatch):
    guest_crm_on()
    fn, sender = load("marketing"), load("marketing-send")
    make_subscribers(env, 1)
    install_hook(monkeypatch, "guest-crm", "marketing.segment", segment_hook([sid("guest0@example.se")]))
    cmp = body_of(segment_campaign(fn))
    admin(fn, "POST", f"/marketing/campaigns/{cmp['campaignId']}/send", {})
    return sender, cmp


@pytest.mark.parametrize("status", [400, 404])
def test_expired_segment_export_is_stopped_not_retried(env, monkeypatch, status):
    sender, cmp = queue_segment(env, monkeypatch)
    install_hook(monkeypatch, "guest-crm", "marketing.segment",
                 lambda req: {"ok": False, "status": status, "code": "not_found"})
    assert sender.handler({"source": "schedule"}, CTX) == {"campaigns": 1}
    item = stored(env, cmp["campaignId"])
    assert item["status"] == "paused" and item["pausedReason"] == "segment_unavailable"
    assert schedule_rows(env) == [] and outbox(env) == []
    assert sender.handler({"source": "schedule"}, CTX) == {"campaigns": 0}


def test_a_temporary_feature_error_still_retries(env, monkeypatch):
    sender, cmp = queue_segment(env, monkeypatch)
    install_hook(monkeypatch, "guest-crm", "marketing.segment",
                 lambda req: {"ok": False, "status": 503, "code": "busy"})
    sender.handler({"source": "schedule"}, CTX)
    assert stored(env, cmp["campaignId"])["status"] == "sending"
    assert len(schedule_rows(env)) == 1


def set_tenant(path, value):
    names = {f"#n{i}": part for i, part in enumerate(path.split("."))}
    boto3.resource("dynamodb", region_name=REGION).Table("test-tenant").update_item(
        Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"}, UpdateExpression=f"SET {'.'.join(names)} = :v",
        ExpressionAttributeNames=names, ExpressionAttributeValues={":v": value})
    shared_tenant.reset_caches()


@pytest.mark.parametrize("path,value", [("status", "suspended"), ("status", "offboarded"),
                                        ("entitlements.features.marketing", False)])
def test_inactive_tenant_or_plan_without_marketing_is_not_sent(env, path, value):
    fn, sender = load("marketing"), load("marketing-send")
    make_subscribers(env, 2)
    _, cmp = draft(env, fn)
    admin(fn, "POST", f"/marketing/campaigns/{cmp['campaignId']}/send", {})
    set_tenant(path, value)
    assert sender.handler({"source": "schedule"}, CTX) == {"campaigns": 1}
    item = stored(env, cmp["campaignId"])
    assert item["status"] == "paused" and item["pausedReason"] == "tenant_inactive"
    assert "leaseUntil" not in item
    assert outbox(env) == [] and schedule_rows(env) == []
    assert sender.handler({"source": "schedule"}, CTX) == {"campaigns": 0}


def test_active_tenant_with_marketing_still_sends(env):
    fn, sender = load("marketing"), load("marketing-send")
    make_subscribers(env, 2)
    _, cmp = draft(env, fn)
    admin(fn, "POST", f"/marketing/campaigns/{cmp['campaignId']}/send", {})
    assert sender.handler({"source": "schedule"}, CTX) == {"campaigns": 1}
    assert stored(env, cmp["campaignId"])["status"] == "sent"
    assert len(outbox(env)) == 2

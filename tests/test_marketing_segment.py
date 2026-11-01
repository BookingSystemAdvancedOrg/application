"""Guest CRM segment audiences in marketing (AP15): accepted only with
guestCrm on, described by the guest-crm feature on save and send, paged by
marketing-send with consent still checked per recipient. Existing
audiences are unchanged."""

from types import SimpleNamespace

import boto3
import pytest

from hooks_fake import install_hook, no_hooks
from shared import consent, dynamo
from shared import marketing as m
from shared import tenant as shared_tenant
from tenant_support import REGION, TENANT_A, TENANT_B
from test_marketing import TEMPLATE, admin, body_of, env, load, make_subscribers, outbox  # env: fixture

SEGMENT = {"type": "segment", "exportId": "exp-1"}
CTX = SimpleNamespace(get_remaining_time_in_millis=lambda: 120000)


def guest_crm_on(tenant_id=TENANT_A):
    boto3.resource("dynamodb", region_name=REGION).Table("test-tenant").update_item(
        Key={"PK": f"TENANT#{tenant_id}", "SK": "PROFILE"},
        UpdateExpression="SET entitlements.features.guestCrm = :t", ExpressionAttributeValues={":t": True})
    shared_tenant.reset_caches()


def sid(email):
    return m.subscriber_id(TENANT_A, email)


def segment_hook(ids, page_size=1):
    def answer(req):
        if req["op"] == "describe":
            return {"ok": True, "count": len(ids)}
        start = int(req.get("cursor") or 0)
        nxt = start + page_size
        return {"ok": True, "subscriberIds": ids[start:nxt], **({"cursor": str(nxt)} if nxt < len(ids) else {})}
    return answer


def segment_campaign(fn):
    tpl = body_of(admin(fn, "POST", "/marketing/templates", TEMPLATE))
    return admin(fn, "POST", "/marketing/campaigns", {"name": "Stamgäster", "templateId": tpl["templateId"],
                                                      "audience": SEGMENT})


def test_off_refuses_a_segment_without_a_hook_call(env, monkeypatch):
    fn = load("marketing")
    calls = install_hook(monkeypatch, "guest-crm", "marketing.segment", segment_hook([]))
    response = segment_campaign(fn)
    assert response["statusCode"] == 403 and body_of(response)["error"] == "feature_not_in_plan"
    assert admin(fn, "GET", "/marketing/audience", query={"type": "segment", "exportId": "exp-1"})["statusCode"] == 403
    assert calls == []


def test_save_describes_and_send_pages_skipping_withdrawn_and_unsubscribed(env, monkeypatch):
    guest_crm_on()
    fn, sender = load("marketing"), load("marketing-send")
    make_subscribers(env, 4)
    consent.withdraw(dynamo.table("test-consent"), tenant_id=TENANT_A, channel="email",
                     identifier="guest1@example.se", source="admin")
    env.table.update_item(Key=m.sub_key(TENANT_A, sid("guest2@example.se")), UpdateExpression="SET #s = :u",
                          ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":u": "unsubscribed"})
    ids = [sid(f"guest{i}@example.se") for i in (0, 1, 2)] + ["0" * 24]  # guest3 not in the segment
    calls = install_hook(monkeypatch, "guest-crm", "marketing.segment", segment_hook(ids))
    response = segment_campaign(fn)
    assert response["statusCode"] == 201, response
    cmp = body_of(response)
    assert cmp["audience"] == SEGMENT
    count = body_of(admin(fn, "GET", "/marketing/audience", query={"type": "segment", "exportId": "exp-1"}))
    assert count == {"audience": SEGMENT, "count": 4}
    queued = body_of(admin(fn, "POST", f"/marketing/campaigns/{cmp['campaignId']}/send", {}))
    assert queued["audienceSize"] == 4
    assert sender.handler({"source": "schedule"}, CTX) == {"campaigns": 1}
    assert [x["to"] for x in outbox(env)] == ["guest0@example.se"]
    pages = [c for c in calls if c["op"] == "page"]
    assert [c.get("cursor") for c in pages] == [None, "1", "2", "3"]
    assert all(c["tenantId"] == TENANT_A and c["exportId"] == "exp-1" for c in calls)
    assert body_of(admin(fn, "GET", f"/marketing/campaigns/{cmp['campaignId']}"))["status"] == "sent"


def test_a_failing_feature_pauses_and_the_next_run_resumes_without_resending(env, monkeypatch):
    guest_crm_on()
    fn, sender = load("marketing"), load("marketing-send")
    make_subscribers(env, 2)
    ids = [sid("guest0@example.se"), sid("guest1@example.se")]
    healthy = segment_hook(ids)
    install_hook(monkeypatch, "guest-crm", "marketing.segment", healthy)
    cmp = body_of(segment_campaign(fn))
    admin(fn, "POST", f"/marketing/campaigns/{cmp['campaignId']}/send", {})
    state = {"pages": 0}

    def flaky(req):
        if req["op"] == "page" and req.get("cursor") == "1" and not state["pages"]:
            state["pages"] += 1
            raise RuntimeError("down")
        return healthy(req)
    install_hook(monkeypatch, "guest-crm", "marketing.segment", flaky)
    sender.handler({"source": "schedule"}, CTX)
    assert [x["to"] for x in outbox(env)] == ["guest0@example.se"]
    sender.handler({"source": "schedule"}, CTX)
    assert sorted(x["to"] for x in outbox(env)) == ["guest0@example.se", "guest1@example.se"]


@pytest.mark.parametrize("audience", [{"type": "segment"}, {"type": "segment", "exportId": "a/b"},
                                      {"type": "segment", "exportId": "x", "days": 7}])
def test_bad_segment_is_400(env, monkeypatch, audience):
    guest_crm_on()
    fn = load("marketing")
    install_hook(monkeypatch, "guest-crm", "marketing.segment", segment_hook([]))
    tpl = body_of(admin(fn, "POST", "/marketing/templates", TEMPLATE))
    response = admin(fn, "POST", "/marketing/campaigns", {"name": "x", "templateId": tpl["templateId"],
                                                          "audience": audience})
    assert response["statusCode"] == 400


def test_refused_unknown_or_not_deployed(env, monkeypatch):
    guest_crm_on()
    fn = load("marketing")
    install_hook(monkeypatch, "guest-crm", "marketing.segment", lambda r: {"ok": False, "status": 404, "code": "x"})
    assert segment_campaign(fn)["statusCode"] == 404
    no_hooks(monkeypatch)
    assert segment_campaign(fn)["statusCode"] == 503


def test_wrong_role_and_other_tenant(env, monkeypatch):
    guest_crm_on()
    guest_crm_on(TENANT_B)
    fn = load("marketing")
    calls = install_hook(monkeypatch, "guest-crm", "marketing.segment", segment_hook(["a"]))
    assert admin(fn, "GET", "/marketing/audience", query={"type": "segment", "exportId": "exp-1"},
                 role="staff_user")["statusCode"] == 403
    assert calls == []
    admin(fn, "GET", "/marketing/audience", query={"type": "segment", "exportId": "exp-1"}, tenant_id=TENANT_B)
    assert calls[-1]["tenantId"] == TENANT_B


def test_existing_audiences_unchanged_with_guest_crm_on(env, monkeypatch):
    guest_crm_on()
    fn = load("marketing")
    calls = install_hook(monkeypatch, "guest-crm", "marketing.segment", segment_hook([]))
    make_subscribers(env, 2)
    assert body_of(admin(fn, "GET", "/marketing/audience", query={"type": "all"})) == {
        "audience": {"type": "all"}, "count": 2}
    assert calls == []

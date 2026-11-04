"""Social posting (feature G, ADR-G, CONTRACTS.md 6.G): routes G1-G8, the
publish worker (per-channel state, IG container reuse, FB needs_check), the
MetaConnection.Disconnected consumer, purge and the two-tenant isolation
suite. Meta is A's simulator; the connection is A's, read only."""

import importlib.util
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from shared import events
from shared import tenant as shared_tenant
from shared.features import meta_ads as ads
from shared.features import social_posting as sp
from shared.meta_graph import SimulatedMetaGraph
from tenant_support import (LOC_A, LOC_B, REGION, TENANT_A, TENANT_B, TENANT_TABLE, features_on, seed_tenancy,
                            tenant_row, with_tenant_claims)

ROOT = Path(__file__).parents[1] / "functions"
POSTS, ADS, MENU, BUCKET, GROUP = "test-social", "test-meta-ads", "test-menu", "test-menu-images", "dev-social-posting"
P1, P2 = "01POSTAAAAAAAAAAAAAAAAAAAA", "01POSTBBBBBBBBBBBBBBBBBBBB"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
SELF_ARN = "arn:aws:lambda:eu-north-1:123456789012:function:dev-social-posting:live"
SCOPES = list(ads.ADS_SCOPES) + list(ads.SOCIAL_SCOPES)


def load(name):
    spec = importlib.util.spec_from_file_location(f"sp_{name.replace('-', '_')}", ROOT / name / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create(ddb, name):
    ddb.create_table(
        TableName=name, BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                              {"AttributeName": "SK", "AttributeType": "S"}])


class FakeLambda:
    def __init__(self):
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(json.loads(kwargs["Payload"]))
        return {"StatusCode": 202}


def image(tenant_id, loc):
    return f"menu-images/{tenant_id}/locations/{loc}/menu/pasta.jpg"


@pytest.fixture
def env(monkeypatch):
    with mock_aws():
        for key, value in {"AWS_DEFAULT_REGION": REGION, "ENVIRONMENT": "dev", "SOCIAL_POSTING_TABLE_NAME": POSTS,
                           "META_ADS_TABLE_NAME": ADS, "MENU_TABLE_NAME": MENU, "MENU_IMAGES_BUCKET_NAME": BUCKET,
                           "SCHEDULE_GROUP_NAME": GROUP,
                           "SCHEDULER_INVOKE_ROLE_ARN": "arn:aws:iam::123456789012:role/dev-sp-scheduler",
                           "SELF_FUNCTION_ARN": SELF_ARN}.items():
            monkeypatch.setenv(key, value)
        seed_tenancy(monkeypatch, features_a=features_on("socialPosting"), features_b=features_on("socialPosting"))
        ddb = boto3.client("dynamodb", region_name=REGION)
        for name in (POSTS, ADS, MENU):
            _create(ddb, name)
        res = boto3.resource("dynamodb", region_name=REGION)
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        secrets = boto3.client("secretsmanager", region_name=REGION)
        for tenant_id, loc in ((TENANT_A, LOC_A), (TENANT_B, LOC_B)):
            s3.put_object(Bucket=BUCKET, Key=image(tenant_id, loc), Body=b"jpeg")
            menu = res.Table(MENU)
            menu.put_item(Item={"PK": f"LOCATION#{loc}", "SK": "MENU#dish1", "name": "Pasta", "price": Decimal("149"),
                                "description": "Hemlagad", "imageKey": image(tenant_id, loc), "alcohol": False})
            menu.put_item(Item={"PK": f"LOCATION#{loc}", "SK": "MENU#wine", "name": "Vin", "price": Decimal("95.5"),
                                "imageKey": image(tenant_id, loc), "alcohol": True})
            res.Table(ADS).put_item(Item={**ads.connection_key(tenant_id), "tenantId": tenant_id, "status": "connected",
                                          "pageId": f"page-{tenant_id[2]}", "instagramId": f"ig-{tenant_id[2]}",
                                          "scopes": SCOPES, "version": 1})
            secrets.create_secret(Name=ads.secret_name(tenant_id),
                                  SecretString=json.dumps({"accessToken": f"tok-{tenant_id}", "scopes": SCOPES}))
        boto3.client("scheduler", region_name=REGION).create_schedule_group(Name=GROUP)
        graph = SimulatedMetaGraph()
        monkeypatch.setattr(ads, "_graph", graph)
        clock = SimpleNamespace(value=NOW)
        monkeypatch.setattr(sp, "now", lambda: clock.value)
        published = []
        monkeypatch.setattr(events, "publish", lambda f, t, d: published.append((t, d)) or "id")
        api = load("social-posting")
        fake = FakeLambda()
        api._clients["lambda"] = fake
        yield SimpleNamespace(api=api, graph=graph, res=res, published=published, invoked=fake, clock=clock)


def call(e, method, template, *, tenant=TENANT_A, role="owner_user", body=None, post=None, query=None):
    event = {"routeKey": f"{method} {template}", "pathParameters": {"postId": post} if post else {},
             "queryStringParameters": query, "body": json.dumps(body) if body is not None else None}
    with_tenant_claims(event, tenant, role)
    response = e.api.handler(event, None)
    return response["statusCode"], (json.loads(response["body"]) if response.get("body") else None)


BASE = "/tenant/social-posts"
ONE = BASE + "/{postId}"


def draft(e, post_id=P1, tenant=TENANT_A, loc=LOC_A, **over):
    body = {"postId": post_id, "locationId": loc, "caption": "Fredagspasta!", "imageKey": image(tenant, loc),
            "channels": ["facebook", "instagram"], "source": {"kind": "dish", "menuItemId": "dish1"}, **over}
    return call(e, "POST", BASE, tenant=tenant, body=body)


def schedule(e, post_id=P1, version=None, tenant=TENANT_A, **body):
    if version is None:
        version = call(e, "GET", ONE, tenant=tenant, post=post_id)[1]["version"]
    return call(e, "POST", ONE + "/schedule", tenant=tenant, post=post_id, body={"version": version, **body})


def run(e, post_id=P1, tenant=TENANT_A):
    return e.api.handler({"socialPostJob": {"tenantId": tenant, "postId": post_id}})


def disconnect_meta(e, tenant=TENANT_A):
    e.res.Table(ADS).update_item(Key=ads.connection_key(tenant), UpdateExpression="SET #s = :d",
                                 ExpressionAttributeNames={"#s": "status"},
                                 ExpressionAttributeValues={":d": "disconnected"})


def schedules():
    return [s["Name"] for s in boto3.client("scheduler").list_schedules(GroupName=GROUP)["Schedules"]]


def meta_calls(e, name):
    return [c for c in e.graph.calls if c[0] == name]


# --- G1 / G2 / G4 ---------------------------------------------------------------------------

def test_create_list_get_and_retry(env):
    status, body = draft(env)
    assert status == 201 and body["status"] == "draft"
    assert set(body["channels"]) == {"facebook", "instagram"}
    assert draft(env, caption="annan")[1]["caption"] == "Fredagspasta!"  # same postId -> same draft
    status, listing = call(env, "GET", BASE)
    assert status == 200 and [p["postId"] for p in listing["posts"]] == [P1]
    assert listing["connection"] == {"missingPermissions": [], "page": "page-a", "instagram": "ig-a"}
    assert call(env, "GET", BASE, query={"status": "nope"})[0] == 400
    assert call(env, "GET", BASE, query={"status": "published"})[1]["posts"] == []
    assert call(env, "GET", ONE, post=P1)[1]["postId"] == P1
    assert call(env, "GET", ONE, post="bad id!")[0] in (400, 404)


@pytest.mark.parametrize("over,code", [({"caption": "x" * 2001}, "caption_too_long"),
                                       ({"channels": ["tiktok"]}, "invalid_channels"),
                                       ({"imageKey": image(TENANT_B, LOC_B)}, "image_not_allowed")])
def test_create_validation(env, over, code):
    status, body = draft(env, **over)
    assert (status, body["code"]) == (400, code)


def test_foreign_location_and_menu_item_404(env):
    assert draft(env, loc=LOC_B, imageKey=image(TENANT_A, LOC_B))[0] == 404
    assert draft(env, source={"kind": "dish", "menuItemId": "nope"})[0] == 404


def test_staff_and_entitlement_off_refused(env):
    assert call(env, "GET", BASE, role="staff_user")[1]["error"] == "owner_only"
    boto3.resource("dynamodb").Table(TENANT_TABLE).put_item(
        Item=tenant_row(TENANT_A, features=features_on("metaAds")))
    shared_tenant.reset_caches()
    assert call(env, "GET", BASE)[1]["error"] == "feature_not_in_plan"
    assert draft(env)[1]["error"] == "feature_not_in_plan"


# --- G3 ----------------------------------------------------------------------------------------

def test_drafts_from_menu_skip_alcohol_and_are_deterministic(env):
    status, body = call(env, "POST", BASE + "/drafts", body={"locationId": LOC_A, "menuItemIds": ["dish1", "wine"]})
    assert status == 200 and [p["postId"] for p in body["posts"]] == ["dish1-20261104"]
    assert body["posts"][0]["caption"] == "Pasta\n\nHemlagad\n\n149 kr"
    again = call(env, "POST", BASE + "/drafts", body={"locationId": LOC_A, "menuItemIds": ["dish1"]})[1]
    assert [p["postId"] for p in again["posts"]] == ["dish1-20261104"]
    assert len(call(env, "GET", BASE)[1]["posts"]) == 1
    eleven = [f"d{i}" for i in range(11)]
    too_many = call(env, "POST", BASE + "/drafts", body={"locationId": LOC_A, "menuItemIds": eleven})
    assert too_many[0] == 400
    assert call(env, "POST", BASE + "/drafts", body={"locationId": LOC_B, "menuItemIds": ["dish1"]})[0] == 404
    assert call(env, "POST", BASE + "/drafts", body={"locationId": LOC_A, "menuItemIds": ["ghost"]})[0] == 404


# --- G5 ----------------------------------------------------------------------------------------

def test_update_version_and_state(env):
    draft(env)
    status, body = call(env, "PUT", ONE, post=P1, body={"caption": "Ny text", "version": 1})
    assert status == 200 and body["caption"] == "Ny text" and body["version"] == 2
    assert call(env, "PUT", ONE, post=P1, body={"caption": "x", "version": 1})[1]["code"] == "changed_retry"
    assert call(env, "PUT", ONE, post=P2, body={"caption": "x", "version": 1})[0] == 404
    schedule(env, now=True)
    run(env)
    status, body = call(env, "PUT", ONE, post=P1, body={"caption": "x", "version": 5})
    assert (status, body["code"]) == (409, "invalid_state")


# --- G6 / G7 / G8 ------------------------------------------------------------------------------

def test_schedule_validation_and_one_schedule(env):
    draft(env)
    assert schedule(env, scheduledAt="2026-11-04T11:00:00Z")[1]["code"] == "invalid_schedule"  # past
    assert schedule(env, scheduledAt="2027-02-03T12:00:00Z")[1]["code"] == "invalid_schedule"  # +91 days
    assert schedule(env, now=False)[1]["code"] == "invalid_schedule"
    status, body = schedule(env, scheduledAt="2026-11-05T09:00:00Z")
    assert status == 200 and body["status"] == "scheduled"
    status, again = schedule(env, version=1, scheduledAt="2026-11-05T09:00:00Z")
    assert status == 200 and again["version"] == body["version"]
    assert schedules() == [f"post-{P1}"]
    assert ("SocialPost.Scheduled", {"tenantId": TENANT_A, "postId": P1, "locationId": LOC_A,
                                     "scheduledAt": "2026-11-05T09:00:00Z",
                                     "channels": ["facebook", "instagram"]}) in env.published


def test_schedule_needs_meta_and_permissions_and_alcohol(env):
    draft(env, source={"kind": "dish", "menuItemId": "wine"})
    status, body = schedule(env, now=True)
    assert (status, body["code"]) == (400, "alcohol_confirmation_required")
    call(env, "PUT", ONE, post=P1, body={"alcoholConfirmed": True, "version": 1})
    env.res.Table(ADS).update_item(Key=ads.connection_key(TENANT_A), UpdateExpression="SET scopes = :s",
                                   ExpressionAttributeValues={":s": list(ads.ADS_SCOPES)})
    status, body = schedule(env, now=True)
    assert (status, body["code"], body["field"]) == (409, "meta_permission_missing", "facebook")
    disconnect_meta(env)
    assert schedule(env, now=True)[1]["code"] == "meta_not_connected"


def test_cancel_and_delete(env):
    draft(env)
    assert call(env, "POST", ONE + "/cancel", post=P1, body={"version": 1})[1]["code"] == "invalid_state"
    schedule(env, scheduledAt="2026-11-05T09:00:00Z")
    status, body = call(env, "POST", ONE + "/cancel", post=P1, body={"version": 2})
    assert status == 200 and body["status"] == "cancelled" and schedules() == []
    assert call(env, "POST", ONE + "/cancel", post=P1, body={"version": 2})[1]["status"] == "cancelled"
    assert call(env, "DELETE", ONE, post=P1)[1]["code"] == "invalid_state"  # only drafts
    draft(env, post_id=P2)
    assert call(env, "DELETE", ONE, post=P2)[0] == 204
    assert call(env, "DELETE", ONE, post=P2)[0] == 204
    assert call(env, "DELETE", ONE, tenant=TENANT_B, post=P1)[0] == 204  # foreign: nothing there for B


# --- the worker ----------------------------------------------------------------------------------

def test_publish_now_both_channels_once(env):
    draft(env)
    schedule(env, now=True)
    assert env.invoked.calls == [{"socialPostJob": {"tenantId": TENANT_A, "postId": P1}}]
    assert run(env) == {"result": "published"}
    assert run(env) == {"result": "noop"}  # run twice: one Meta call per channel
    assert len(meta_calls(env, "publish_page_photo")) == 1 and len(meta_calls(env, "publish_ig_container")) == 1
    post = call(env, "GET", ONE, post=P1)[1]
    assert post["status"] == "published"
    assert post["channels"]["facebook"]["metaPostId"].startswith("sim-post-")
    assert post["channels"]["instagram"]["metaPostId"].startswith("sim-media-")
    kinds = [t for t, _ in env.published]
    assert kinds.count("SocialPost.Published") == 2


def test_ig_crash_after_container_reuses_it(env):
    draft(env)
    schedule(env, now=True)
    env.graph._fail["publish_ig_container"] = 1
    assert run(env) == {"result": "partly_published"}
    ig = call(env, "GET", ONE, post=P1)[1]["channels"]["instagram"]
    assert ig["state"] == "failed" and ig["containerId"]
    schedule(env, now=True)  # retry touches only the failed channel
    assert run(env) == {"result": "published"}
    assert len(meta_calls(env, "create_ig_container")) == 1
    assert len(meta_calls(env, "publish_page_photo")) == 1


def test_fb_unknown_outcome_goes_to_needs_check_and_is_never_reposted(env):
    draft(env, channels=["facebook"])
    schedule(env, now=True)
    env.graph._fail["publish_page_photo"] = 1
    assert run(env) == {"result": "failed"}
    assert call(env, "GET", ONE, post=P1)[1]["channels"]["facebook"]["state"] == "needs_check"
    # The post reached Meta but the answer was lost: it is on the Page, created after the first attempt.
    env.graph.posts.append({"id": "fb-already", "message": "Fredagspasta!", "permalink": "https://example.invalid/p",
                            "created_time": "2026-11-04T12:00:05+0000"})
    schedule(env, now=True)
    assert run(env) == {"result": "published"}
    fb = call(env, "GET", ONE, post=P1)[1]["channels"]["facebook"]
    assert fb["metaPostId"] == "fb-already"
    assert len(meta_calls(env, "recent_page_posts")) == 1
    assert len(meta_calls(env, "publish_page_photo")) == 1  # only the lost first attempt


def test_fb_needs_check_never_matches_an_older_post_with_the_same_caption(env):
    # Last week's post with the same caption, and one without created_time: neither proves this post went out.
    env.graph.posts.append({"id": "fb-last-week", "message": "Fredagspasta!", "permalink": "https://example.invalid/o",
                            "created_time": "2026-10-28T12:00:00+0000"})
    env.graph.posts.append({"id": "fb-no-time", "message": "Fredagspasta!", "permalink": "https://example.invalid/n"})
    draft(env, channels=["facebook"])
    schedule(env, now=True)
    env.graph._fail["publish_page_photo"] = 1
    assert run(env) == {"result": "failed"}
    schedule(env, now=True)
    assert run(env) == {"result": "published"}
    fb = call(env, "GET", ONE, post=P1)[1]["channels"]["facebook"]
    assert fb["metaPostId"] not in ("fb-last-week", "fb-no-time")
    assert len(meta_calls(env, "recent_page_posts")) == 1
    assert len(meta_calls(env, "publish_page_photo")) == 2


def test_schedule_retry_with_the_same_time_creates_the_lost_schedule(env, monkeypatch):
    draft(env)
    real = env.api._create_schedule

    def dies(post, at):
        raise RuntimeError("Lambda timed out before the schedule")

    monkeypatch.setattr(env.api, "_create_schedule", dies)
    with pytest.raises(RuntimeError):
        schedule(env, scheduledAt="2026-11-05T09:00:00Z")
    assert call(env, "GET", ONE, post=P1)[1]["status"] == "scheduled" and schedules() == []
    monkeypatch.setattr(env.api, "_create_schedule", real)
    status, body = schedule(env, version=1, scheduledAt="2026-11-05T09:00:00Z")
    assert status == 200 and body["status"] == "scheduled"
    assert schedules() == [f"post-{P1}"]
    assert schedule(env, version=1, scheduledAt="2026-11-05T09:00:00Z")[0] == 200  # Conflict = already there
    assert schedules() == [f"post-{P1}"]


def test_entitlement_off_before_timer_fails_without_meta_call(env):
    draft(env)
    schedule(env, scheduledAt="2026-11-05T09:00:00Z")
    boto3.resource("dynamodb").Table(TENANT_TABLE).put_item(Item=tenant_row(TENANT_A, features=features_on()))
    shared_tenant.reset_caches()
    assert run(env) == {"result": "failed"}
    post = call(env, "GET", ONE, post=P1, tenant=TENANT_A)
    assert env.graph.calls == []
    assert post[0] == 403  # feature off for the routes too


def test_meta_disconnected_at_publish_fails_channels(env):
    draft(env)
    schedule(env, scheduledAt="2026-11-05T09:00:00Z")
    disconnect_meta(env)
    assert run(env) == {"result": "failed"}
    assert call(env, "GET", ONE, post=P1)[1]["channels"]["facebook"]["lastError"] == {"code": "meta_not_connected"}


def test_disconnect_event_twice_moves_scheduled_to_draft(env):
    draft(env)
    schedule(env, scheduledAt="2026-11-05T09:00:00Z")
    event = {"source": "booqy.meta-ads", "detail-type": "MetaConnection.Disconnected",
             "detail": {"tenantId": TENANT_A, "reason": "owner"}}
    assert env.api.handler(event) == {"result": "drafted", "count": 1}
    assert env.api.handler(event) == {"result": "drafted", "count": 0}
    assert call(env, "GET", ONE, post=P1)[1]["status"] == "draft" and schedules() == []


def test_purge_and_unknown_events(env):
    draft(env)
    draft(env, tenant=TENANT_B, loc=LOC_B)
    assert env.api.handler({"hook": "tenant.purge", "tenantId": TENANT_A, "locationIds": [LOC_A], "round": 1}) == {
        "ok": True, "done": True, "counts": {"items": 1}}
    assert [i["PK"] for i in env.res.Table(POSTS).scan()["Items"]] == [f"TENANT#{TENANT_B}"]
    assert env.api.handler({"hook": "privacy.export", "tenantId": TENANT_A})["code"] == "unknown_hook"
    assert env.api.handler({"x": 1})["code"] == "unknown_event"


def test_two_tenant_isolation_and_worker_reads_own_secret(env, monkeypatch):
    draft(env)
    draft(env, tenant=TENANT_B, loc=LOC_B)
    assert call(env, "GET", ONE, tenant=TENANT_B, post=P1)[1]["locationId"] == LOC_B
    assert [p["locationId"] for p in call(env, "GET", BASE, tenant=TENANT_B)[1]["posts"]] == [LOC_B]
    reads = []
    real = ads.get_token
    monkeypatch.setattr(ads, "get_token", lambda t: reads.append(t) or real(t))
    schedule(env, now=True)
    run(env)
    assert reads == [TENANT_A]
    assert all(c[1] in ("page-a", "ig-a") or c[0] == "publish_ig_container" for c in env.graph.calls)


def test_simulator_schedule_name_and_caption_format():
    assert sp.schedule_name(P1) == f"post-{P1}"
    assert len(sp.schedule_name("x" * 80)) <= 64
    assert sp.format_price(Decimal("95.5")) == "95,50 kr"
    assert sp.overall({"facebook": {"state": "published"}, "instagram": {"state": "failed"}}) == "partly_published"

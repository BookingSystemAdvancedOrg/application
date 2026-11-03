"""Meta ads (feature A, ADR-A, CONTRACTS.md 6.A): routes A1-A17, the
publish workflow tasks, nightly insights, caps, kill switch and the
two-tenant isolation suite. Meta and Bedrock are the simulators."""

import importlib.util
import json
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from shared import events
from shared import marketing as mkt
from shared import tenant as shared_tenant
from shared.bedrock import SimulatedTextModel
from shared.features import meta_ads as ads
from shared.meta_graph import SimulatedMetaGraph
from tenant_support import (LOC_A, LOC_A2, LOC_B, REGION, TENANT_A, TENANT_B, features_on, location_row,
                            seed_tenancy, tenant_row, with_tenant_claims)

ROOT = Path(__file__).parents[1] / "functions"
ADS_TABLE, MENU_TABLE, MKT_TABLE, BUCKET = "test-meta-ads", "test-menu", "test-marketing", "test-menu-images"
CAMP = "01CAMPAIGNAAAAAAAAAAAAAAAA"
CAMP2 = "01CAMPAIGNBBBBBBBBBBBBBBBB"
POSITION = [Decimal("18.06"), Decimal("59.33")]


def load(name):
    spec = importlib.util.spec_from_file_location(f"ads_{name.replace('-', '_')}", ROOT / name / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create(ddb, name, gsis=(), extra=()):
    ddb.create_table(
        TableName=name, BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("PK", "SK", *extra)],
        **({"GlobalSecondaryIndexes": list(gsis)} if gsis else {}))


@pytest.fixture
def env(monkeypatch):
    with mock_aws():
        for key, value in {"AWS_DEFAULT_REGION": REGION, "ENVIRONMENT": "dev", "META_ADS_TABLE_NAME": ADS_TABLE,
                           "MENU_TABLE_NAME": MENU_TABLE, "MARKETING_TABLE_NAME": MKT_TABLE,
                           "MENU_IMAGES_BUCKET_NAME": BUCKET, "USER_TABLE_NAME": "test-user",
                           "META_REDIRECT_URI": "https://admin.example/marknadsforing?flik=annonser",
                           "PUBLISH_STATE_MACHINE_ARN":
                               "arn:aws:states:eu-north-1:123456789012:stateMachine:dev-meta-ads-publish",
                           "NOTIFICATION_FUNCTION_NAME": "dev-notification:live"}.items():
            monkeypatch.setenv(key, value)
        loc_extra = {"cateringOrigin": {"text": "Gatan 1", "position": POSITION}}
        seed_tenancy(monkeypatch,
                     tenant_a=tenant_row(TENANT_A, features=features_on("metaAds"), legalName="Bistro A AB",
                                         slug="bistro-a"),
                     tenant_b=tenant_row(TENANT_B, features=features_on("metaAds")),
                     locations_a=[location_row(TENANT_A, LOC_A, **loc_extra), location_row(TENANT_A, LOC_A2)],
                     locations_b=[location_row(TENANT_B, LOC_B, **loc_extra)])
        ddb = boto3.client("dynamodb", region_name=REGION)
        _create(ddb, ADS_TABLE, extra=("GSI1PK", "GSI1SK"), gsis=[{
            "IndexName": "byConnected", "Projection": {"ProjectionType": "ALL"},
            "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"},
                          {"AttributeName": "GSI1SK", "KeyType": "RANGE"}]}])
        _create(ddb, MENU_TABLE)
        _create(ddb, MKT_TABLE)
        res = boto3.resource("dynamodb", region_name=REGION)
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        for tenant_id, loc in ((TENANT_A, LOC_A), (TENANT_B, LOC_B)):
            key = f"menu-images/{tenant_id}/locations/{loc}/dish.jpg"
            s3.put_object(Bucket=BUCKET, Key=key, Body=b"jpeg-" + tenant_id.encode())
            res.Table(MENU_TABLE).put_item(Item={"PK": f"LOCATION#{loc}", "SK": "MENU#dish1", "name": "Pasta",
                                                 "description": "Hemlagad", "imageKey": key, "alcohol": False})
            res.Table(MENU_TABLE).put_item(Item={"PK": f"LOCATION#{loc}", "SK": "MENU#wine", "name": "Vin",
                                                 "imageKey": key, "alcohol": True})
        graph, text = SimulatedMetaGraph(), SimulatedTextModel()
        monkeypatch.setattr(ads, "_graph", graph)
        monkeypatch.setattr(ads, "_text", text)
        published, started, metrics, notices = [], [], [], []
        monkeypatch.setattr(events, "publish", lambda f, t, d: published.append((t, d)) or "id")
        api, jobs = load("meta-ads"), load("meta-ads-jobs")
        monkeypatch.setattr(api, "_start_execution", lambda name, payload: started.append(payload) or f"arn:{name}")
        monkeypatch.setattr(api, "_invoke_jobs", lambda payload: started.append(payload))
        monkeypatch.setattr(jobs, "_put_metric", lambda v: metrics.append(v))
        monkeypatch.setattr(jobs, "_notify", lambda n: notices.append(n))
        yield SimpleNamespace(api=api, jobs=jobs, graph=graph, text=text, published=published, started=started,
                              metrics=metrics, notices=notices, res=res)


# --- helpers ---------------------------------------------------------------------------

def call(e, method, template, *, tenant=TENANT_A, role="owner_user", body=None, query=None, params=None):
    event = {"routeKey": f"{method} {template}", "pathParameters": dict(params or {}),
             "queryStringParameters": query, "body": json.dumps(body) if body is not None else None}
    with_tenant_claims(event, tenant, role)
    response = e.api.handler(event, None)
    return response["statusCode"], (json.loads(response["body"]) if response.get("body") else None)


def connect(e, tenant=TENANT_A, cap=3000):
    url = call(e, "POST", "/tenant/meta-ads/connection/start", tenant=tenant, body={})[1]["authorizeUrl"]
    state = re.search(r"state=([^&]+)", url).group(1)
    status, body = call(e, "POST", "/tenant/meta-ads/connection/callback", tenant=tenant,
                        body={"code": "simulated", "state": state})
    assert status == 200, body
    status, body = call(e, "PUT", "/tenant/meta-ads/connection", tenant=tenant, body={
        "adAccountId": "act_sim1", "pageId": "sim-page-1", "instagramId": "sim-ig-1",
        "dsa": {"beneficiary": "Bistro", "payor": "Bistro"}, "monthlyCapSek": cap, "version": body["version"]})
    assert status == 200, body
    return body


CAMPAIGNS = "/locations/{locationId}/meta-ads/campaigns"
ONE = CAMPAIGNS + "/{campaignId}"


def new_campaign(e, campaign_id=CAMP, *, tenant=TENANT_A, loc=LOC_A, **overrides):
    body = {"campaignId": campaign_id, "goal": "bookings", "menuItemId": "dish1", "headline": "Pasta på fredag",
            "primaryText": "Boka bord i kväll.", "budgetSek": 500, "days": 7, "radiusKm": 5, **overrides}
    return call(e, "POST", CAMPAIGNS, tenant=tenant, params={"locationId": loc}, body=body)


def run_workflow(e, tenant_id=TENANT_A, campaign_id=CAMP, *, stop_after=None):
    for task in ("CheckGuards", "UploadImage", "CreateCampaign", "CreateAdSet", "CreateCreative", "CreateAd",
                 "MarkPaused"):
        e.jobs.handler({"task": task, "tenantId": tenant_id, "campaignId": campaign_id})
        if task == stop_after:
            return


def published_campaign(e, campaign_id=CAMP, tenant=TENANT_A, loc=LOC_A):
    status, body = new_campaign(e, campaign_id, tenant=tenant, loc=loc)
    assert status == 202, body
    run_workflow(e, tenant, campaign_id)
    return ads.get_campaign(tenant, campaign_id)


def ads_off(e, tenant_id=TENANT_A, *features):
    e.res.Table("test-tenant").put_item(Item=tenant_row(tenant_id, features=features_on(*features)))
    shared_tenant.reset_caches()


# --- A1-A6 connection -----------------------------------------------------------------

def test_a1_to_a5_connect_flow(env):
    status, body = call(env, "GET", "/tenant/meta-ads/connection")
    assert status == 200 and body["status"] == "disconnected"
    assert body["dsa"] == {"beneficiary": "Bistro A AB", "payor": "Bistro A AB"} and body["monthlyCapSek"] == 3000
    body = connect(env)
    assert body["status"] == "connected" and body["assets"]["adAccountId"] == "act_sim1"
    assert body["killSwitch"] is False and body["missingPermissions"] == [
        s for s in ads.ADS_SCOPES if s not in ("ads_management",)]
    assert ("MetaConnection.Connected", {"tenantId": TENANT_A, "adAccountId": "act_sim1", "pageId": "sim-page-1",
                                         "instagramId": "sim-ig-1"}) in env.published
    secret = boto3.client("secretsmanager", region_name=REGION).get_secret_value(
        SecretId=f"booqy/dev/meta-ads/{TENANT_A}")["SecretString"]
    assert "sim-token" in secret and "sim-token" not in json.dumps(body)


def test_a2_a3_state_is_single_use_and_bound_to_tenant_and_owner(env):
    url = call(env, "POST", "/tenant/meta-ads/connection/start", body={})[1]["authorizeUrl"]
    state = re.search(r"state=([^&]+)", url).group(1)
    assert call(env, "POST", "/tenant/meta-ads/connection/start", body={"x": 1})[0] == 400
    assert call(env, "POST", "/tenant/meta-ads/connection/callback", tenant=TENANT_B,
                body={"code": "simulated", "state": state})[1]["error"] == "invalid_state"
    assert call(env, "POST", "/tenant/meta-ads/connection/callback", body={"code": "simulated", "state": state})[0] \
        == 200
    assert call(env, "POST", "/tenant/meta-ads/connection/callback", body={"code": "simulated", "state": state})[1][
        "error"] == "invalid_state"
    assert call(env, "POST", "/tenant/meta-ads/connection/callback", body={"code": "x", "state": "short"})[0] == 400


def test_a4_assets_lists_missing_with_deep_links(env, monkeypatch):
    assert call(env, "GET", "/tenant/meta-ads/assets")[1]["error"] == "not_connected"
    connect(env)
    monkeypatch.setattr(ads, "_graph", SimulatedMetaGraph(missing=("ad_account", "instagram")))
    status, body = call(env, "GET", "/tenant/meta-ads/assets")
    assert status == 200 and {m["kind"] for m in body["missing"]} == {"ad_account", "instagram"}
    assert all(m["deepLink"].startswith("https://") for m in body["missing"])


def test_a5_validation_and_stale_version(env):
    version = connect(env)["version"]
    base = {"adAccountId": "act_sim1", "pageId": "sim-page-1", "dsa": {"beneficiary": "B", "payor": "B"},
            "monthlyCapSek": 3000}
    assert call(env, "PUT", "/tenant/meta-ads/connection", body={**base, "monthlyCapSek": 999999,
                                                                  "version": version})[1]["error"] == "invalid_cap"
    assert call(env, "PUT", "/tenant/meta-ads/connection", body={**base, "adAccountId": "act_B",
                                                                  "version": version})[1]["error"] == "unknown_asset"
    assert call(env, "PUT", "/tenant/meta-ads/connection", body={**base, "version": version - 1})[1][
        "error"] == "changed_retry"


def test_a6_disconnect_pauses_deletes_secret_and_is_idempotent(env):
    connect(env)
    item = published_campaign(env)
    call(env, "POST", ONE + "/activate", params={"locationId": LOC_A, "campaignId": CAMP},
         body={"version": int(item["version"])})
    assert call(env, "DELETE", "/tenant/meta-ads/connection")[0] == 204
    assert ads.get_campaign(TENANT_A, CAMP)["status"] == "paused"
    assert ads.get_token(TENANT_A) is None
    assert call(env, "DELETE", "/tenant/meta-ads/connection")[0] == 204
    assert ("MetaConnection.Disconnected", {"tenantId": TENANT_A, "reason": "owner"}) in env.published


@pytest.mark.parametrize("method, path", [
    ("GET", "/tenant/meta-ads/connection"), ("POST", "/tenant/meta-ads/connection/start"),
    ("GET", "/tenant/meta-ads/assets"), ("PUT", "/tenant/meta-ads/connection"),
    ("DELETE", "/tenant/meta-ads/connection"), ("POST", "/tenant/meta-ads/connection/callback")])
def test_connection_routes_staff_403_and_off_403_but_social_posting_is_enough(env, method, path):
    assert call(env, method, path, role="staff_user", body={})[1]["error"] == "owner_only"
    ads_off(env)
    assert call(env, method, path, body={})[1]["error"] == "feature_not_in_plan"
    ads_off(env, TENANT_A, "socialPosting")
    assert call(env, method, path, body={})[0] != 403


# --- A7 copy --------------------------------------------------------------------------

COPY = "/locations/{locationId}/meta-ads/copy"


def test_a7_copy_suggestions_and_quota(env, monkeypatch):
    prompts = []
    real = env.text.complete
    monkeypatch.setattr(env.text, "complete", lambda system, prompt, **kw: prompts.append(prompt) or real(
        system, prompt, **kw))
    status, body = call(env, "POST", COPY, params={"locationId": LOC_A},
                        body={"menuItemId": "dish1", "goal": "bookings"})
    assert status == 200 and len(body["suggestions"]) == 3
    assert all(len(s["headline"]) <= 40 and len(s["primaryText"]) <= 125 for s in body["suggestions"])
    assert "Pasta" in prompts[-1] and "@" not in prompts[-1] and "Hemlagad" in prompts[-1]
    monkeypatch.setattr(ads, "COPY_QUOTA_PER_MONTH", 1)
    assert call(env, "POST", COPY, params={"locationId": LOC_A},
                body={"menuItemId": "dish1", "goal": "bookings"})[1]["error"] == "quota_exceeded"


def test_a7_validation_alcohol_foreign_role_off_and_ai_down(env, monkeypatch):
    p = {"locationId": LOC_A}
    assert call(env, "POST", COPY, params=p, body={"menuItemId": "nope", "goal": "bookings"})[0] == 404
    assert call(env, "POST", COPY, params=p, body={"menuItemId": "wine", "goal": "bookings"})[1][
        "error"] == "alcohol_not_allowed"
    assert call(env, "POST", COPY, params={"locationId": LOC_B}, body={"menuItemId": "dish1", "goal": "bookings"})[0] \
        == 404
    assert call(env, "POST", COPY, params=p, role="staff_user", body={"menuItemId": "dish1", "goal": "x"})[0] == 403
    monkeypatch.setattr(ads, "_text", SimulatedTextModel(fail={"complete": 1}))
    assert call(env, "POST", COPY, params=p, body={"menuItemId": "dish1", "goal": "bookings"})[1][
        "error"] == "ai_unavailable"
    ads_off(env)
    assert call(env, "POST", COPY, params=p, body={"menuItemId": "dish1", "goal": "bookings"})[0] == 403


# --- A8-A12 campaigns and the publish workflow ----------------------------------------

def test_a8_create_is_paused_after_workflow_with_lifetime_budget_end_date_and_dsa(env):
    connect(env)
    status, body = new_campaign(env)
    assert status == 202 and body["status"] == "publishing" and env.started == [
        {"tenantId": TENANT_A, "campaignId": CAMP}]
    run_workflow(env)
    item = ads.get_campaign(TENANT_A, CAMP)
    assert item["status"] == "paused" and set(item["meta"]) == {"imageHash", "campaignId", "adSetId", "creativeId",
                                                                "adId"}
    adset = env.graph.objects[item["meta"]["adSetId"]]
    assert adset["budget"] == 50000 and all(o["status"] == "PAUSED" for o in env.graph.objects.values())
    adset_call = next(c for c in env.graph.calls if c[0] == "create_ad_set")
    assert adset_call
    assert item["endDate"] == (date.fromisoformat(item["startDate"]) + timedelta(days=7)).isoformat()
    assert "utm_medium=paid_social" in item["link"] and f"utm_campaign=booqy-{CAMP[:10].lower()}" in item["link"]
    assert ("MetaCampaign.Created", {"tenantId": TENANT_A, "campaignId": CAMP, "locationId": LOC_A,
                                     "goal": "bookings", "budgetSek": 500, "endDate": item["endDate"]}) \
        in env.published


def test_ad_set_gets_dsa_and_end_date(env, monkeypatch):
    seen = {}
    real = env.graph.create_ad_set

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)
    monkeypatch.setattr(env.graph, "create_ad_set", spy)
    connect(env)
    item = published_campaign(env)
    assert seen["dsa_beneficiary"] == "Bistro" and seen["dsa_payor"] == "Bistro" and seen["age_min"] == 18
    assert seen["end"] == item["endDate"] and seen["lifetime_budget_ore"] == 50000
    assert seen["geo"]["radiusKm"] == 5


def test_a8_retry_same_campaign_id_gives_one_item_and_one_execution(env):
    connect(env)
    assert new_campaign(env)[0] == 202
    status, body = new_campaign(env, budgetSek=900)
    assert status == 202 and body["budgetSek"] == 500 and len(env.started) == 1


def test_a8_start_execution_failure_is_started_by_the_retry(env, monkeypatch):
    from botocore.exceptions import ClientError
    connect(env)
    ok, calls = env.api._start_execution, []

    def flaky(name, payload):
        calls.append(name)
        if len(calls) == 1:
            raise ClientError({"Error": {"Code": "ServiceUnavailable"}}, "StartExecution")
        return ok(name, payload)

    monkeypatch.setattr(env.api, "_start_execution", flaky)
    assert new_campaign(env)[0] == 503
    assert "executionArn" not in ads.get_campaign(TENANT_A, CAMP) and env.started == []
    status, body = new_campaign(env)
    assert status == 202 and body["status"] == "publishing" and len(env.started) == 1
    assert ads.get_campaign(TENANT_A, CAMP)["executionArn"] == f"arn:{CAMP}"
    assert new_campaign(env)[0] == 202 and len(env.started) == 1  # started once: no second execution
    run_workflow(env)
    assert ads.get_campaign(TENANT_A, CAMP)["status"] == "paused"


@pytest.mark.parametrize("override, code", [({"radiusKm": 50}, "invalid_radius"), ({"days": 40}, "invalid_days"),
                                            ({"budgetSek": 50}, "invalid_budget"),
                                            ({"menuItemId": "wine"}, "alcohol_not_allowed"),
                                            ({"headline": "x" * 41}, "text_too_long")])
def test_a8_validation(env, override, code):
    connect(env)
    status, body = new_campaign(env, **override)
    assert status == 400 and body["error"] == code


def test_a8_guards_not_connected_cap_kill_switch(env):
    assert new_campaign(env)[1]["error"] == "not_connected"
    connect(env, cap=600)
    assert new_campaign(env, budgetSek=700)[1]["error"] == "cap_exceeded"
    env.api.handler({"hook": "platform", "op": "meta-ads.settings.put", "tenantId": None,
                     "body": {"killSwitch": True, "maxMonthlyCapSek": 20000}})
    assert new_campaign(env)[1]["error"] == "kill_switch"


def test_a8_location_without_position_is_assets_missing(env):
    connect(env)
    env.res.Table("test-menu").put_item(Item={"PK": f"LOCATION#{LOC_A2}", "SK": "MENU#dish1", "name": "P",
                                              "imageKey": "k"})
    status, body = new_campaign(env, loc=LOC_A2)
    assert (status, body["error"], body["field"]) == (409, "assets_missing", "position")


def test_a8_role_foreign_off(env):
    connect(env)
    assert new_campaign(env, loc=LOC_B)[0] == 404
    status, _ = call(env, "POST", CAMPAIGNS, role="staff_user", params={"locationId": LOC_A}, body={})
    assert status == 403
    ads_off(env)
    assert new_campaign(env)[0] == 403


def test_workflow_rerun_of_every_task_creates_no_duplicates(env):
    connect(env)
    published_campaign(env)
    before = dict(env.graph.objects)
    for task in ("UploadImage", "CreateCampaign", "CreateAdSet", "CreateCreative", "CreateAd", "MarkPaused"):
        env.jobs.handler({"task": task, "tenantId": TENANT_A, "campaignId": CAMP})  # every task again
    with pytest.raises(env.jobs.GuardFailed):  # the start guard refuses a campaign that is not publishing
        env.jobs.handler({"task": "CheckGuards", "tenantId": TENANT_A, "campaignId": CAMP})
    assert env.graph.objects == before
    assert [c[0] for c in env.graph.calls].count("create_campaign") == 1


def test_workflow_resumes_after_failure_and_after_a_lost_write(env, monkeypatch):
    connect(env)
    new_campaign(env)
    run_workflow(env, stop_after="CreateAdSet")
    # CreateCreative succeeded at Meta but the write of its id was lost:
    item = ads.get_campaign(TENANT_A, CAMP)
    env.graph.create_creative(None, "act_sim1", f"booqy-{CAMP}-creative", "p", None, "h", "t", "x", "l", "c")
    run_workflow(env)  # redrive from the start
    item = ads.get_campaign(TENANT_A, CAMP)
    assert item["status"] == "paused" and [c[0] for c in env.graph.calls].count("create_creative") == 1


def test_workflow_meta_down_raises_for_retry_and_guard_failure_marks_failed(env, monkeypatch):
    connect(env)
    new_campaign(env)
    monkeypatch.setattr(ads, "_graph", SimulatedMetaGraph(fail={"create_campaign": 1}))
    env.jobs.handler({"task": "UploadImage", "tenantId": TENANT_A, "campaignId": CAMP})
    with pytest.raises(Exception):
        env.jobs.handler({"task": "CreateCampaign", "tenantId": TENANT_A, "campaignId": CAMP})
    env.jobs.handler({"task": "CreateCampaign", "tenantId": TENANT_A, "campaignId": CAMP})  # retried: ok
    env.api.handler({"hook": "platform", "op": "meta-ads.settings.put", "tenantId": None,
                     "body": {"killSwitch": True, "maxMonthlyCapSek": 20000}})
    with pytest.raises(env.jobs.GuardFailed):
        env.jobs.handler({"task": "CheckGuards", "tenantId": TENANT_A, "campaignId": CAMP})
    env.jobs.handler({"task": "MarkFailed", "tenantId": TENANT_A, "campaignId": CAMP,
                      "error": {"Error": "GuardFailed", "Cause": "kill_switch"}})
    item = ads.get_campaign(TENANT_A, CAMP)
    assert item["status"] == "failed" and item["error"] == {"code": "kill_switch"}


def test_upload_refuses_another_tenants_image_key(env):
    connect(env)
    new_campaign(env)
    env.res.Table(ADS_TABLE).update_item(
        Key=ads.campaign_key(TENANT_A, CAMP), UpdateExpression="SET creative.imageKey = :k",
        ExpressionAttributeValues={":k": f"menu-images/{TENANT_B}/locations/{LOC_B}/dish.jpg"})
    with pytest.raises(env.jobs.GuardFailed):
        env.jobs.handler({"task": "UploadImage", "tenantId": TENANT_A, "campaignId": CAMP})


def test_a9_a10_list_and_get(env):
    connect(env)
    published_campaign(env)
    status, body = call(env, "GET", CAMPAIGNS, params={"locationId": LOC_A})
    assert status == 200 and [c["campaignId"] for c in body["campaigns"]] == [CAMP]
    assert "cursor" not in body  # one page: the optional cursor is omitted, not null
    assert call(env, "GET", CAMPAIGNS, params={"locationId": LOC_A2})[1]["campaigns"] == []
    assert call(env, "GET", CAMPAIGNS, params={"locationId": LOC_A}, query={"cursor": "zz"})[0] == 400
    status, body = call(env, "GET", ONE, params={"locationId": LOC_A, "campaignId": CAMP})
    assert status == 200 and body["previewUrl"].startswith("https://")
    assert call(env, "GET", ONE, params={"locationId": LOC_A2, "campaignId": CAMP})[0] == 404
    assert call(env, "GET", CAMPAIGNS, role="staff_user", params={"locationId": LOC_A})[0] == 403
    ads_off(env)
    assert call(env, "GET", CAMPAIGNS, params={"locationId": LOC_A})[0] == 403


def test_a11_a12_activate_pause_with_retries(env):
    connect(env)
    item = published_campaign(env)
    p = {"locationId": LOC_A, "campaignId": CAMP}
    status, body = call(env, "POST", ONE + "/activate", params=p, body={"version": int(item["version"])})
    assert status == 200 and body["status"] == "active"
    assert all(env.graph.objects[i]["status"] == "ACTIVE" for i in ads.meta_ids(item))
    assert call(env, "POST", ONE + "/activate", params=p, body={"version": int(item["version"])})[1]["status"] \
        == "active"
    status, body = call(env, "POST", ONE + "/pause", params=p, body={"version": body["version"]})
    assert status == 200 and body["status"] == "paused"
    assert call(env, "POST", ONE + "/pause", params=p, body={"version": 1})[1]["status"] == "paused"
    assert call(env, "POST", ONE + "/activate", params=p, body={"version": 1})[1]["error"] == "changed_retry"
    assert ("MetaCampaign.Paused", {"tenantId": TENANT_A, "campaignId": CAMP, "by": "owner"}) in env.published


def test_a11_lost_race_puts_meta_back_to_paused(env, monkeypatch):
    connect(env)
    item = published_campaign(env)
    p = {"locationId": LOC_A, "campaignId": CAMP}
    real = ads.graph().set_status

    def racing(token, object_id, status):  # a pause lands between the Meta call and the write
        real(token, object_id, status)
        if status == "ACTIVE" and object_id == ads.meta_ids(item)[0]:
            env.res.Table(ADS_TABLE).update_item(Key=ads.campaign_key(TENANT_A, CAMP),
                                                 UpdateExpression="SET version = version + :one",
                                                 ExpressionAttributeValues={":one": 1})

    monkeypatch.setattr(ads.graph(), "set_status", racing)
    status, body = call(env, "POST", ONE + "/activate", params=p, body={"version": int(item["version"])})
    assert (status, body["error"]) == (409, "changed_retry")
    assert all(env.graph.objects[i]["status"] == "PAUSED" for i in ads.meta_ids(item))
    assert ads.get_campaign(TENANT_A, CAMP)["status"] == "paused"


def test_a11_cap_and_kill_switch_and_invalid_status(env):
    connect(env)
    status, _ = new_campaign(env)
    p = {"locationId": LOC_A, "campaignId": CAMP}
    assert call(env, "POST", ONE + "/activate", params=p, body={"version": 1})[1]["error"] == "invalid_status"
    run_workflow(env)
    version = int(ads.get_campaign(TENANT_A, CAMP)["version"])
    call(env, "PUT", "/tenant/meta-ads/connection", body={
        "adAccountId": "act_sim1", "pageId": "sim-page-1", "dsa": {"beneficiary": "B", "payor": "B"},
        "monthlyCapSek": 400, "version": int(ads.get_connection(TENANT_A)["version"])})
    assert call(env, "POST", ONE + "/activate", params=p, body={"version": version})[1]["error"] == "cap_exceeded"
    env.api.handler({"hook": "platform", "op": "meta-ads.tenant.pause", "tenantId": TENANT_A,
                     "body": {"paused": True}})
    assert call(env, "POST", ONE + "/activate", params=p, body={"version": version})[1]["error"] == "kill_switch"


def test_a11_a12_foreign_role_off(env):
    connect(env)
    connect(env, TENANT_B)
    published_campaign(env, CAMP2, tenant=TENANT_B, loc=LOC_B)
    p = {"locationId": LOC_A, "campaignId": CAMP2}
    assert call(env, "POST", ONE + "/activate", params=p, body={"version": 2})[0] == 404
    assert call(env, "POST", ONE + "/pause", params=p, body={"version": 2})[0] == 404
    assert call(env, "POST", ONE + "/activate", params={"locationId": LOC_B, "campaignId": CAMP2},
                body={"version": 2})[0] == 404
    assert ads.get_campaign(TENANT_B, CAMP2)["status"] == "paused"
    assert call(env, "POST", ONE + "/pause", params=p, role="staff_user", body={"version": 2})[0] == 403
    ads_off(env)
    assert call(env, "POST", ONE + "/pause", params=p, body={"version": 2})[0] == 403


# --- insights, caps, kill switch ------------------------------------------------------

def _active(env, campaign_id=CAMP):
    item = published_campaign(env, campaign_id)
    call(env, "POST", ONE + "/activate", params={"locationId": LOC_A, "campaignId": campaign_id},
         body={"version": int(item["version"])})
    return ads.get_campaign(TENANT_A, campaign_id)


def test_nightly_insights_rerun_overwrites_and_attributes_by_utm(env):
    connect(env)
    item = _active(env)
    day = item["startDate"]
    env.res.Table(MKT_TABLE).put_item(Item={"PK": mkt.stats_pk(TENANT_A),
                                            "SK": mkt.stats_sk(day, LOC_A, "facebook", item["utmCampaign"]),
                                            "bookings": 3, "orders": 1, "revenueOre": 90000})
    env.res.Table(MKT_TABLE).put_item(Item={"PK": mkt.stats_pk(TENANT_A),
                                            "SK": mkt.stats_sk(day, LOC_A, "facebook", "other"), "bookings": 9})
    first = env.jobs.handler({"job": "insights", "day": day})
    again = env.jobs.handler({"job": "insights", "day": day})
    assert first["spendSek"] == again["spendSek"] > 0
    rows = ads.insights(TENANT_A, CAMP)
    assert len(rows) == 1 and rows[0]["bookings"] == 3 and rows[0]["orders"] == 1
    assert ads.spend_this_month_ore(TENANT_A, day[:7]) == rows[0]["spendOre"]
    status, body = call(env, "GET", "/locations/{locationId}/meta-ads/insights", params={"locationId": LOC_A},
                        query={"from": day, "to": day})
    assert status == 200 and body["rows"][0]["costPerBookingSek"] == round(int(rows[0]["spendOre"]) / 100 / 3)
    assert env.metrics[-1] == first["spendSek"]


def test_a13_validation_role_foreign_off(env):
    path, p = "/locations/{locationId}/meta-ads/insights", {"locationId": LOC_A}
    assert call(env, "GET", path, params=p, query={"from": "2026-10-10", "to": "2026-10-01"})[1][
        "error"] == "invalid_range"
    assert call(env, "GET", path, params=p, query={"from": "x", "to": "2026-10-01"})[0] == 400
    assert call(env, "GET", path, params={"locationId": LOC_B}, query={"from": "2026-10-01", "to": "2026-10-02"})[0] \
        == 404
    assert call(env, "GET", path, params=p, role="staff_user", query={})[0] == 403
    ads_off(env)
    assert call(env, "GET", path, params=p, query={"from": "2026-10-01", "to": "2026-10-02"})[0] == 403


def test_cap_80_percent_mails_once_and_100_percent_pauses_all(env, monkeypatch):
    connect(env, cap=1000)
    item = _active(env)
    day = item["startDate"]
    spends = iter([85000, 85000, 120000])
    monkeypatch.setattr(env.graph, "insights", lambda token, acct, ids, d: [
        {"campaign_id": ids[0], "day": d, "impressions": 1, "clicks": 1, "spend_ore": next(spends)}])
    env.jobs.handler({"job": "insights", "day": day})
    env.jobs.handler({"job": "insights", "day": day})
    assert len([n for n in env.notices if n["kind"] == "meta-ads.cap-warning"]) == 1
    assert env.notices[0]["to"] == "owner@a.example"
    assert ads.get_campaign(TENANT_A, CAMP)["status"] == "active"
    env.jobs.handler({"job": "insights", "day": day})
    assert ads.get_campaign(TENANT_A, CAMP)["status"] == "paused"
    assert any(t == "MetaSpend.CapReached" for t, _ in env.published)
    assert ("MetaCampaign.Paused", {"tenantId": TENANT_A, "campaignId": CAMP, "by": "cap"}) in env.published


def test_kill_switch_sweep_pauses_every_active_campaign(env):
    connect(env)
    _active(env)
    assert env.api.handler({"hook": "platform", "op": "meta-ads.settings.put", "tenantId": None,
                            "body": {"killSwitch": True, "maxMonthlyCapSek": 20000}})["ok"]
    assert env.started[-1] == {"job": "sweep"}
    assert env.jobs.handler({"job": "sweep"}) == {"paused": 1}
    assert ads.get_campaign(TENANT_A, CAMP)["status"] == "paused"


# --- A14-A17 operator hooks and purge -------------------------------------------------

def test_a14_to_a17_platform_ops(env):
    get = env.api.handler({"hook": "platform", "op": "meta-ads.settings.get", "tenantId": None, "body": {}})
    assert get == {"ok": True, "status": 200, "body": {"killSwitch": False, "maxMonthlyCapSek": 20000}}
    bad = env.api.handler({"hook": "platform", "op": "meta-ads.settings.put", "body": {"killSwitch": False,
                                                                                       "maxMonthlyCapSek": 5}})
    assert bad["ok"] is False and bad["code"] == "invalid_cap"
    same = [env.api.handler({"hook": "platform", "op": "meta-ads.settings.put", "body": {
        "killSwitch": False, "maxMonthlyCapSek": 5000}}) for _ in range(2)]
    assert same[0] == same[1]
    assert env.api.handler({"hook": "platform", "op": "meta-ads.tenant.get", "tenantId": "nope"})["status"] == 404
    connect(env)
    _active(env)
    paused = env.api.handler({"hook": "platform", "op": "meta-ads.tenant.pause", "tenantId": TENANT_A,
                              "body": {"paused": True}})
    assert paused["body"] == {"paused": True} and ads.get_campaign(TENANT_A, CAMP)["status"] == "paused"
    info = env.api.handler({"hook": "platform", "op": "meta-ads.tenant.get", "tenantId": TENANT_A})["body"]
    assert info["paused"] is True and info["adAccountId"] == "act_sim1" and "token" not in json.dumps(info).lower()


def test_tenant_purge_deletes_rows_and_secret_only_for_that_tenant(env):
    connect(env)
    connect(env, TENANT_B)
    published_campaign(env)
    answer = env.api.handler({"hook": "tenant.purge", "tenantId": TENANT_A, "locationIds": [LOC_A], "round": 1})
    assert answer["ok"] and answer["done"]
    assert ads._query_all(boto3.dynamodb.conditions.Key("PK").eq(ads.pk(TENANT_A))) == []
    assert ads.get_token(TENANT_A) is None and ads.get_token(TENANT_B) is not None


def test_simulator_refused_in_prod(monkeypatch):
    from shared import external_client
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.delenv("META_CLIENT", raising=False)
    with pytest.raises(RuntimeError):
        external_client.mode("meta")


def test_no_pixel_or_facebook_sdk_in_frontends():
    root = Path(__file__).parents[2]
    pattern = re.compile(r"connect\.facebook\.net|fbq\(|facebook\.com/tr\?")
    hits = []
    for repo in ("admin-front-end", "customer-front-end", "booqy-console", "booqy-website"):
        base = root / repo
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.suffix in (".ts", ".tsx", ".js", ".html") and "node_modules" not in path.parts \
                    and "dist" not in path.parts and path.is_file():
                if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                    hits.append(str(path))
    assert hits == []


# --- two-tenant isolation -------------------------------------------------------------

def test_two_tenant_isolation(env, monkeypatch):
    """A's owner lists, reads, activates and reads insights for B's campaign
    ids and LOC_B: all 404, B's rows unchanged; A's jobs never read B's secret."""
    connect(env)
    connect(env, TENANT_B)
    b_item = published_campaign(env, CAMP2, tenant=TENANT_B, loc=LOC_B)
    for params in ({"locationId": LOC_A, "campaignId": CAMP2}, {"locationId": LOC_B, "campaignId": CAMP2}):
        assert call(env, "GET", ONE, params=params)[0] == 404
        assert call(env, "POST", ONE + "/activate", params=params, body={"version": 2})[0] == 404
    assert call(env, "GET", CAMPAIGNS, params={"locationId": LOC_B})[0] == 404
    assert call(env, "GET", "/locations/{locationId}/meta-ads/insights", params={"locationId": LOC_B},
                query={"from": "2026-10-01", "to": "2026-10-02"})[0] == 404
    assert ads.get_campaign(TENANT_B, CAMP2) == b_item
    read = []
    real = ads.get_token
    monkeypatch.setattr(ads, "get_token", lambda t: read.append(t) or real(t))
    published_campaign(env)
    assert set(read) == {TENANT_A}

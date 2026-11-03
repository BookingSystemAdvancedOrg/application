"""Site builder B1 (feature B, ADR-B, CONTRACTS.md 6.B): routes B1-B8, the
publish workflow tasks, rollback, contrast vectors shared with site-kit,
and the two-tenant isolation suite."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from jsonschema import Draft202012Validator
from moto import mock_aws

from shared import events
from shared import tenant as shared_tenant
from shared.features import site_builder as sb
from tenant_support import (LOC_A, LOC_B, REGION, TENANT_A, TENANT_B, features_on, seed_tenancy, tenant_row,
                            with_tenant_claims)

ROOT = Path(__file__).parents[1]
TABLE, BUCKET = "test-site-builder", "test-sites"
PUB1, PUB2, PUB3 = "01PUBLISHAAAAAAAAAAAAAAAAA", "01PUBLISHBBBBBBBBBBBBBBBBB", "01PUBLISHCCCCCCCCCCCCCCCCC"
VECTORS = ROOT / "shared" / "site_schema" / "contrast_vectors.json"


def load():
    spec = importlib.util.spec_from_file_location("sb_app", ROOT / "functions" / "site-builder" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def env(monkeypatch):
    with mock_aws():
        for k, v in {"AWS_DEFAULT_REGION": REGION, "ENVIRONMENT": "dev", "SITE_BUILDER_TABLE_NAME": TABLE,
                     "SITES_BUCKET_NAME": BUCKET, "SITE_BASE_DOMAIN": "booqy.se",
                     "PUBLISH_STATE_MACHINE_ARN": "arn:aws:states:eu-north-1:1:stateMachine:dev-site-builder-publish"
                     }.items():
            monkeypatch.setenv(k, v)
        seed_tenancy(monkeypatch,
                     tenant_a=tenant_row(TENANT_A, features=features_on("siteBuilder"), slug="bistro-a",
                                         name="Bistro A"),
                     tenant_b=tenant_row(TENANT_B, features=features_on("siteBuilder"), slug="bistro-b"))
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("PK", "SK")])
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        published, started = [], []
        monkeypatch.setattr(events, "publish", lambda f, t, d: published.append((t, d)) or "id")
        app = load()
        monkeypatch.setattr(app, "_start_execution", lambda name, payload: started.append(payload) or f"arn:{name}")
        yield SimpleNamespace(app=app, s3=s3, published=published, started=started,
                              res=boto3.resource("dynamodb", region_name=REGION))


def call(e, method, template, *, tenant=TENANT_A, role="owner_user", body=None, params=None, claims=True):
    event = {"routeKey": f"{method} {template}", "pathParameters": dict(params or {}),
             "body": json.dumps(body) if body is not None else None,
             "requestContext": {"http": {"sourceIp": "203.0.113.9"}}}
    if claims:
        with_tenant_claims(event, tenant, role)
    response = e.app.handler(event, None)
    return response["statusCode"], (json.loads(response["body"]) if response.get("body") else None)


def doc(tenant_id=TENANT_A, **theme):
    d = sb.starter_doc({"name": "Bistro A"})
    d["theme"].update(theme)
    d["pages"].append({"id": "om-oss", "slug": "var-historia", "title": "Om oss",
                       "seo": {"title": "Om oss", "description": "Vilka vi är."},
                       "blocks": [{"id": "t1", "type": "text", "body": "Familjeägt sedan 1999."},
                                  {"id": "i1", "type": "image",
                                   "image": f"menu-images/{tenant_id}/locations/x/a.jpg", "alt": "Matsalen"},
                                  {"id": "h1", "type": "hero", "heading": "Hej",
                                   "cta": {"label": "Boka", "link": "page:start"}}]})
    d["nav"].append({"pageId": "om-oss"})
    return d


def save(e, d=None, version=0, tenant=TENANT_A):
    return call(e, "PUT", "/tenant/site-builder/draft", tenant=tenant,
                body={"doc": d or doc(tenant), "version": version})


def render(e, tenant_id, version_no, d):
    """What the Node render step writes (customer-front-end render-lambda)."""
    base = f"sites/{tenant_id}/v/{version_no}/"
    for page in d["pages"]:
        e.s3.put_object(Bucket=BUCKET, Key=f"{base}{sb.page_path(page)}index.html",
                        Body=f"<html>{page['title']} v{version_no}</html>".encode())
    e.s3.put_object(Bucket=BUCKET, Key=f"{base}site.json", Body=json.dumps(d).encode())


def run(e, tenant_id, publish_id, *, fail_render=False):
    out = e.app.handler({"task": "Validate", "tenantId": tenant_id, "publishId": publish_id})
    if out["render"] and not fail_render:
        render(e, tenant_id, out["versionNo"], sb.plain(sb.get(tenant_id, sb.version_sk(out["versionNo"]))["doc"]))
    return e.app.handler({"task": "Promote", "tenantId": tenant_id, "publishId": publish_id})


def publish(e, publish_id=PUB1, tenant=TENANT_A, draft_version=1):
    status, body = call(e, "POST", "/tenant/site-builder/publish", tenant=tenant,
                        body={"publishId": publish_id, "draftVersion": draft_version})
    assert status == 202, body
    return run(e, tenant, publish_id)


def root(e, tenant_id):
    page = e.s3.list_objects_v2(Bucket=BUCKET, Prefix=f"sites/{tenant_id}/")
    return {o["Key"] for o in page.get("Contents") or [] if "/v/" not in o["Key"]}


def off(e, tenant_id=TENANT_A):
    e.res.Table("test-tenant").put_item(Item=tenant_row(tenant_id, features=features_on()))
    shared_tenant.reset_caches()


# --- schema and contrast --------------------------------------------------------------

def test_schema_file_is_valid_and_starter_doc_passes():
    schema = json.loads(sb.SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(schema)
    sb.validate_doc(sb.starter_doc({"name": "X"}), TENANT_A, {LOC_A})
    sb.contrast_report(sb.starter_doc({"name": "X"}))


@pytest.mark.parametrize("mutate, code, field", [
    (lambda d: d["pages"][0]["blocks"].append({"id": "x", "type": "video"}), "schema_invalid", "/pages/0/blocks/5"),
    (lambda d: d["pages"][1]["blocks"][2]["cta"].update(link="page:nope"), "invalid_link",
     "/pages/1/blocks/2/cta/link"),
    (lambda d: d["pages"][1]["blocks"][2]["cta"].update(link="http://x.se"), "schema_invalid",
     "/pages/1/blocks/2/cta/link"),
    (lambda d: d["pages"][1]["blocks"][1].update(image=f"menu-images/{TENANT_B}/locations/x/a.jpg"), "unknown_image",
     "/pages/1/blocks/1/image"),
    (lambda d: d["nav"].append({"pageId": "ghost"}), "invalid_link", "/nav/2/pageId"),
    (lambda d: d["pages"][1].update(slug=""), "schema_invalid", "/pages/1/slug"),
    (lambda d: d["pages"][0]["blocks"].append({"id": "m", "type": "menu", "locationId": LOC_B}), "schema_invalid",
     "/pages/0/blocks/5/locationId"),
    (lambda d: d["footer"].update(text="x" * 200_000), "doc_too_large", ""),
])
def test_validation_codes_and_json_pointers(mutate, code, field):
    d = doc()
    mutate(d)
    with pytest.raises(sb.SiteError) as exc:
        sb.validate_doc(d, TENANT_A, {LOC_A})
    assert (exc.value.code, exc.value.field) == (code, field)


def test_contrast_vectors_match_the_file_shared_with_site_kit():
    vectors = json.loads(VECTORS.read_text())
    assert len(vectors) >= 10
    for v in vectors:
        assert sb.accent_pair(v["input"], v["mode"]) == {"accent": v["accent"], "onAccent": v["onAccent"]}, v


def test_every_theme_colour_is_nudged_to_readable():
    for color in ("#777777", "#888888", "#ff0000", "#ffff00", "#350001", "#ffffff", "#000000", "#3c8dbc"):
        for row in sb.contrast_report(doc(primaryColor=color, accentColor=color)):
            assert row["ratio"] >= 4.5


def test_contrast_too_low_is_reported_with_its_field(monkeypatch):
    monkeypatch.setattr(sb, "accent_pair", lambda value, mode: {"accent": "#777777", "onAccent": "#888888"})
    with pytest.raises(sb.SiteError) as exc:
        sb.contrast_report(doc())
    assert (exc.value.code, exc.value.field) == ("contrast_too_low", "/theme/primaryColor")


# --- B1, B2 ---------------------------------------------------------------------------

def test_b1_starter_draft_then_saved_draft(env):
    status, body = call(env, "GET", "/tenant/site-builder")
    assert status == 200 and body["mode"] == "builder" and body["draft"]["version"] == 0
    assert body["draft"]["doc"]["pages"][0]["title"] == "Bistro A" and "published" not in body
    assert save(env)[0] == 200
    assert call(env, "GET", "/tenant/site-builder")[1]["draft"]["version"] == 1


def test_b1_b2_role_and_off(env):
    assert call(env, "GET", "/tenant/site-builder", role="staff_user")[1]["error"] == "owner_only"
    assert call(env, "PUT", "/tenant/site-builder/draft", role="staff_user", body={})[0] == 403
    off(env)
    assert call(env, "GET", "/tenant/site-builder")[1]["error"] == "feature_not_in_plan"
    assert save(env)[0] == 403


def test_b2_validation_and_stale_version(env):
    bad = doc()
    bad["pages"][0]["blocks"].append({"id": "x", "type": "video"})
    status, body = save(env, bad)
    assert status == 400 and body["error"] == "schema_invalid" and body["field"] == "/pages/0/blocks/5"
    big = doc()
    big["footer"]["text"] = "x" * 160_000
    assert save(env, big)[1]["error"] == "doc_too_large"
    assert call(env, "PUT", "/tenant/site-builder/draft", body={"doc": doc(), "version": 0, "x": 1})[0] == 400
    assert save(env)[0] == 200
    assert save(env, version=0)[1]["error"] == "changed_retry"
    assert save(env, version=1)[1]["version"] == 2


def test_b2_wrong_mode(env):
    env.res.Table(TABLE).put_item(Item={**sb.key(TENANT_A, "SITE"), "mode": "custom"})
    assert save(env)[1]["error"] == "wrong_mode"


# --- B3, B8 previews ------------------------------------------------------------------

def _token(url):
    return url.split("#", 1)[1]


def test_b3_b8_preview_link_shows_the_draft_and_is_noindex(env):
    save(env)
    status, body = call(env, "POST", "/tenant/site-builder/preview-links", body={})
    assert status == 200 and body["url"].startswith("https://bistro-a.booqy.se/forhandsvisning#")
    second = call(env, "POST", "/tenant/site-builder/preview-links", body={})[1]
    assert _token(second["url"]) != _token(body["url"])
    items = env.res.Table(TABLE).scan()["Items"]
    assert not any(_token(body["url"]) in json.dumps(i, default=str) for i in items)  # stored hashed only
    event = {"routeKey": "POST /public/site-builder/preview", "requestContext": {"http": {"sourceIp": "1.2.3.4"}},
             "body": json.dumps({"tenantId": TENANT_A, "token": _token(body["url"])})}
    response = env.app.handler(event)
    assert response["statusCode"] == 200 and response["headers"]["X-Robots-Tag"] == "noindex"
    assert json.loads(response["body"])["doc"]["pages"][1]["id"] == "om-oss"


def test_b3_validation_role_off(env):
    assert call(env, "POST", "/tenant/site-builder/preview-links", body={"x": 1})[0] == 400
    assert call(env, "POST", "/tenant/site-builder/preview-links", role="staff_user", body={})[0] == 403
    off(env)
    assert call(env, "POST", "/tenant/site-builder/preview-links", body={})[0] == 403


@pytest.mark.parametrize("body", [{"tenantId": TENANT_A, "token": "x" * 43}, {"tenantId": TENANT_A, "token": "#"},
                                  {"tenantId": TENANT_A}, {"tenantId": "../x", "token": "x" * 43},
                                  {"tenantId": TENANT_A, "token": "x" * 43, "extra": 1}])
def test_b8_any_bad_token_is_404(env, body):
    response = env.app.handler({"routeKey": "POST /public/site-builder/preview", "body": json.dumps(body),
                                "requestContext": {"http": {"sourceIp": "1.2.3.4"}}})
    assert response["statusCode"] == 404


def test_b8_token_of_a_with_tenant_b_is_404_and_feature_off_is_404(env):
    url = call(env, "POST", "/tenant/site-builder/preview-links", body={})[1]["url"]
    def event(t):
        return {"routeKey": "POST /public/site-builder/preview", "requestContext": {"http": {}},
                "body": json.dumps({"tenantId": t, "token": _token(url)})}
    assert env.app.handler(event(TENANT_B))["statusCode"] == 404
    assert env.app.handler(event(TENANT_A))["statusCode"] == 200
    off(env)
    assert env.app.handler(event(TENANT_A))["statusCode"] == 404


@pytest.mark.parametrize("slug", sorted(sb.RESERVED_SLUGS))
def test_reserved_slug_is_refused_on_save(env, slug):
    d = doc()
    d["pages"][1]["slug"] = slug
    status, body = save(env, d)
    assert status == 400 and body["error"] == "schema_invalid" and body["field"] == "/pages/1/slug"


def test_reserved_slugs_cover_every_customer_app_route():
    app_tsx = ROOT.parent / "customer-front-end" / "src" / "App.tsx"
    if not app_tsx.exists():
        pytest.skip("customer-front-end is not checked out beside application")
    import re
    routed = {m.split("/")[0] for m in re.findall(r'path="([^"*:]+)', app_tsx.read_text())}
    assert routed <= sb.RESERVED_SLUGS


# --- B4-B7 publish, status, versions, rollback ----------------------------------------

def test_b4_publish_renders_promotes_swaps_pointer_and_mirrors_site_config(env):
    save(env)
    out = publish(env)
    assert out["versionNo"] == 1
    assert root(env, TENANT_A) == {f"sites/{TENANT_A}/index.html", f"sites/{TENANT_A}/var-historia/index.html",
                                   f"sites/{TENANT_A}/site.json"}
    head = env.s3.head_object(Bucket=BUCKET, Key=f"sites/{TENANT_A}/index.html")
    assert head["CacheControl"] == "no-cache"
    profile = env.res.Table("test-tenant").get_item(Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"})["Item"]
    assert profile["site"] == {"mode": "builder", "version": 1}
    status, body = call(env, "GET", "/tenant/site-builder/publish/{publishId}", params={"publishId": PUB1})
    assert (status, body) == (200, {"status": "done", "versionNo": 1})
    assert ("SiteVersion.Published", {"tenantId": TENANT_A, "versionNo": 1, "source": "builder",
                                      "kind": "publish"}) in env.published
    assert call(env, "GET", "/tenant/site-builder")[1]["published"]["versionNo"] == 1


def test_b4_same_publish_id_is_one_execution(env):
    save(env)
    for _ in range(2):
        status, body = call(env, "POST", "/tenant/site-builder/publish", body={"publishId": PUB1, "draftVersion": 1})
        assert status == 202 and body == {"publishId": PUB1, "status": "running"}
    assert len(env.started) == 1


def test_b4_start_execution_failure_unlocks_and_the_retry_publishes(env, monkeypatch):
    from botocore.exceptions import ClientError
    save(env)
    ok = env.app._start_execution
    calls = []

    def flaky(name, payload):
        calls.append(name)
        if len(calls) == 1:
            raise ClientError({"Error": {"Code": "ServiceUnavailable"}}, "StartExecution")
        return ok(name, payload)

    monkeypatch.setattr(env.app, "_start_execution", flaky)
    status, _ = call(env, "POST", "/tenant/site-builder/publish", body={"publishId": PUB1, "draftVersion": 1})
    assert status == 503
    row = sb.get(TENANT_A, f"PUBLISH#{PUB1}")
    assert row["status"] == "failed" and sb.plain(row["error"]) == {"code": "start_failed"}
    assert "running" not in sb.get(TENANT_A, "SITE")  # lock released: another publish is not blocked
    status, body = call(env, "POST", "/tenant/site-builder/publish", body={"publishId": PUB1, "draftVersion": 1})
    assert (status, body) == (202, {"publishId": PUB1, "status": "running"})
    assert sb.get(TENANT_A, f"PUBLISH#{PUB1}")["executionArn"] == f"arn:{PUB1}"
    assert run(env, TENANT_A, PUB1)["versionNo"] == 1
    assert call(env, "GET", "/tenant/site-builder/publish/{publishId}", params={"publishId": PUB1})[1] == {
        "status": "done", "versionNo": 1}
    assert call(env, "POST", "/tenant/site-builder/publish", body={"publishId": PUB1, "draftVersion": 1})[1][
        "status"] == "done"
    assert len(env.started) == 1


def test_b4_row_written_but_never_started_is_started_on_retry(env, monkeypatch):
    save(env)
    start = env.app._start
    monkeypatch.setattr(env.app, "_start", lambda *a: (_ for _ in ()).throw(RuntimeError("lambda died")))
    with pytest.raises(RuntimeError):
        call(env, "POST", "/tenant/site-builder/publish", body={"publishId": PUB1, "draftVersion": 1})
    monkeypatch.setattr(env.app, "_start", start)
    assert sb.get(TENANT_A, "SITE")["running"] == PUB1
    assert call(env, "POST", "/tenant/site-builder/publish", body={"publishId": PUB1, "draftVersion": 1})[0] == 202
    assert len(env.started) == 1 and sb.get(TENANT_A, f"PUBLISH#{PUB1}")["executionArn"] == f"arn:{PUB1}"


def test_b4_one_publish_at_a_time_and_stale_draft(env):
    save(env)
    assert call(env, "POST", "/tenant/site-builder/publish", body={"publishId": PUB1, "draftVersion": 1})[0] == 202
    assert call(env, "POST", "/tenant/site-builder/publish", body={"publishId": PUB2, "draftVersion": 1})[1][
        "error"] == "publish_running"
    run(env, TENANT_A, PUB1)
    assert call(env, "POST", "/tenant/site-builder/publish", body={"publishId": PUB2, "draftVersion": 9})[1][
        "error"] == "changed_retry"


def test_b4_validation_custom_source_role_off(env):
    save(env)
    assert call(env, "POST", "/tenant/site-builder/publish", body={"publishId": "x", "draftVersion": 1})[0] == 400
    assert call(env, "POST", "/tenant/site-builder/publish",
                body={"publishId": PUB1, "source": "custom", "commitSha": "abcdef1"})[1]["error"] == "wrong_mode"
    assert call(env, "POST", "/tenant/site-builder/publish", role="staff_user",
                body={"publishId": PUB1, "draftVersion": 1})[0] == 403
    off(env)
    assert call(env, "POST", "/tenant/site-builder/publish", body={"publishId": PUB1, "draftVersion": 1})[0] == 403


def test_b4_unknown_image_in_draft_is_refused_before_the_workflow(env):
    d = doc()
    env.res.Table(TABLE).put_item(Item={**sb.key(TENANT_A, "DRAFT"), "doc": {**d, "pages": [d["pages"][0], {
        **d["pages"][1], "blocks": [{"id": "i", "type": "image", "image": f"menu-images/{TENANT_B}/locations/x/y.jpg",
                                     "alt": "x"}]}]}, "version": 3})
    status, body = call(env, "POST", "/tenant/site-builder/publish", body={"publishId": PUB1, "draftVersion": 3})
    assert status == 400 and body["error"] == "unknown_image" and env.started == []


def test_workflow_is_idempotent_and_resumable(env):
    save(env)
    call(env, "POST", "/tenant/site-builder/publish", body={"publishId": PUB1, "draftVersion": 1})
    first = env.app.handler({"task": "Validate", "tenantId": TENANT_A, "publishId": PUB1})
    again = env.app.handler({"task": "Validate", "tenantId": TENANT_A, "publishId": PUB1})
    assert first == again and first["versionNo"] == 1
    # render failed: Promote fails cleanly and leaves the root untouched
    with pytest.raises(env.app.TaskFailed):
        env.app.handler({"task": "Promote", "tenantId": TENANT_A, "publishId": PUB1})
    assert root(env, TENANT_A) == set()
    render(env, TENANT_A, 1, sb.plain(sb.get(TENANT_A, sb.version_sk(1))["doc"]))  # redrive after Render
    env.app.handler({"task": "Promote", "tenantId": TENANT_A, "publishId": PUB1})
    snapshot = root(env, TENANT_A)
    env.app.handler({"task": "Promote", "tenantId": TENANT_A, "publishId": PUB1})  # rerun Promote: same objects
    assert root(env, TENANT_A) == snapshot
    assert [t for t, _ in env.published].count("SiteVersion.Published") == 1


def test_mark_failed_records_code_and_releases_the_lock(env):
    save(env)
    call(env, "POST", "/tenant/site-builder/publish", body={"publishId": PUB1, "draftVersion": 1})
    cause = json.dumps({"errorMessage": json.dumps({"code": "render_missing", "field": "/pages/start"})})
    env.app.handler({"task": "MarkFailed", "tenantId": TENANT_A, "publishId": PUB1,
                     "error": {"Error": "TaskFailed", "Cause": cause}})
    body = call(env, "GET", "/tenant/site-builder/publish/{publishId}", params={"publishId": PUB1})[1]
    assert body == {"status": "failed", "error": {"code": "render_missing", "field": "/pages/start"}}
    assert ("SitePublish.Failed", {"tenantId": TENANT_A, "publishId": PUB1, "code": "render_missing"}) in env.published
    assert call(env, "POST", "/tenant/site-builder/publish", body={"publishId": PUB2, "draftVersion": 1})[0] == 202


def test_b5_b6_foreign_and_unknown(env):
    save(env, tenant=TENANT_B)
    publish(env, PUB1, tenant=TENANT_B)
    assert call(env, "GET", "/tenant/site-builder/publish/{publishId}", params={"publishId": PUB1})[0] == 404
    assert call(env, "GET", "/tenant/site-builder/publish/{publishId}", params={"publishId": "bad"})[0] == 404
    assert call(env, "GET", "/tenant/site-builder/versions")[1] == {"versions": []}
    assert call(env, "GET", "/tenant/site-builder/versions", role="staff_user")[0] == 403
    off(env)
    assert call(env, "GET", "/tenant/site-builder/versions")[0] == 403


def test_b7_rollback_is_a_pointer_swap_without_render(env):
    save(env)
    publish(env, PUB1)
    d2 = doc()
    d2["pages"] = d2["pages"][:1]
    d2["nav"] = d2["nav"][:1]
    save(env, d2, version=1)
    publish(env, PUB2, draft_version=2)
    assert f"sites/{TENANT_A}/var-historia/index.html" not in root(env, TENANT_A)  # removed page deleted at root
    versions = call(env, "GET", "/tenant/site-builder/versions")[1]["versions"]
    assert [(v["versionNo"], v["live"]) for v in versions] == [(2, True), (1, False)]
    status, body = call(env, "POST", "/tenant/site-builder/rollback", body={"publishId": PUB3, "versionNo": 1})
    assert status == 202 and env.started[-1]["kind"] == "rollback"
    out = env.app.handler({"task": "Validate", "tenantId": TENANT_A, "publishId": PUB3})
    assert out["render"] is False
    env.app.handler({"task": "Promote", "tenantId": TENANT_A, "publishId": PUB3})
    assert f"sites/{TENANT_A}/var-historia/index.html" in root(env, TENANT_A)
    assert sb.next_version_no(TENANT_A) == 3  # no new version written
    assert env.res.Table("test-tenant").get_item(Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"})["Item"][
        "site"]["version"] == 1


def test_b7_live_version_is_a_noop_and_errors(env):
    save(env)
    publish(env, PUB1)
    assert call(env, "POST", "/tenant/site-builder/rollback", body={"publishId": PUB2, "versionNo": 1}) == (
        200, {"publishId": PUB2, "status": "done", "versionNo": 1})
    assert call(env, "POST", "/tenant/site-builder/rollback", body={"publishId": PUB2, "versionNo": 999})[0] == 404
    assert call(env, "POST", "/tenant/site-builder/rollback", body={"publishId": PUB2, "versionNo": "1"})[0] == 400
    assert call(env, "POST", "/tenant/site-builder/rollback", role="staff_user",
                body={"publishId": PUB2, "versionNo": 1})[0] == 403
    off(env)
    assert call(env, "POST", "/tenant/site-builder/rollback", body={"publishId": PUB2, "versionNo": 1})[0] == 403


def test_hooks_purge_and_platform(env):
    save(env)
    publish(env)
    save(env, tenant=TENANT_B)
    assert env.app.handler({"hook": "tenant.purge", "tenantId": TENANT_A})["done"] is True
    assert sb.get(TENANT_A, "DRAFT") is None and sb.get(TENANT_B, "DRAFT") is not None
    assert env.app.handler({"hook": "platform", "op": "site-builder.get", "tenantId": TENANT_A})["status"] == 404


# --- two-tenant isolation -------------------------------------------------------------

def test_two_tenant_isolation(env):
    """A publishes; every S3 key written starts with sites/<A>/; A's routes
    with B's ids 404; B's site unchanged."""
    save(env, tenant=TENANT_B)
    publish(env, PUB1, tenant=TENANT_B)
    before_b = {o["Key"]: o["ETag"] for o in env.s3.list_objects_v2(Bucket=BUCKET,
                                                                    Prefix=f"sites/{TENANT_B}/")["Contents"]}
    written = []
    real = env.app._s3

    class Spy:
        def __init__(self):
            self.inner = real()

        def __getattr__(self, name):
            fn = getattr(self.inner, name)

            def wrapped(**kw):
                if name in ("copy_object", "delete_object", "put_object"):
                    written.append(kw["Key"])
                return fn(**kw)
            return wrapped
    env.app._s3 = Spy
    save(env)
    publish(env, PUB2)
    assert written and all(k.startswith(f"sites/{TENANT_A}/") for k in written)
    assert call(env, "GET", "/tenant/site-builder/publish/{publishId}", params={"publishId": PUB1})[0] == 404
    assert call(env, "POST", "/tenant/site-builder/rollback", body={"publishId": PUB3, "versionNo": 2})[0] == 404
    after_b = {o["Key"]: o["ETag"] for o in env.s3.list_objects_v2(Bucket=BUCKET,
                                                                   Prefix=f"sites/{TENANT_B}/")["Contents"]}
    assert after_b == before_b
    assert int(sb.get(TENANT_B, "PUBLISHED")["versionNo"]) == 1

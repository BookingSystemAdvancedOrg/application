"""Own background image per user (functions/user-backdrop): presigned POST
upload with strict input, GET answering a presigned URL or null, DELETE,
keys from the token only, and the error shapes."""

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from shared import dynamo as shared_dynamo
from tenant_support import (OWNER_A_SUB, REGION, STAFF_A_SUB, TENANT_A, TENANT_B, TENANT_TABLE,
                            seed_tenancy, tenant_row, with_tenant_claims)

ROOT = Path(__file__).parents[1]
BUCKET = "test-user-backdrops"
MAX = 10 * 1024 * 1024


def load():
    spec = importlib.util.spec_from_file_location("user_backdrop_app", ROOT / "functions" / "user-backdrop" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def app(monkeypatch):
    with mock_aws():
        for key, value in {"AWS_DEFAULT_REGION": REGION, "AWS_REGION": REGION, "ENVIRONMENT": "dev",
                           "USER_BACKDROPS_BUCKET_NAME": BUCKET, "AWS_ACCESS_KEY_ID": "testing",
                           "AWS_SECRET_ACCESS_KEY": "testing"}.items():
            monkeypatch.setenv(key, value)
        shared_dynamo._resource = None
        shared_dynamo._client = None
        seed_tenancy(monkeypatch)
        boto3.client("s3", region_name=REGION).create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        yield load()
        shared_dynamo._resource = None
        shared_dynamo._client = None


def call(app, route, body=None, role="owner_user", tenant_id=TENANT_A, sub=None):
    event = with_tenant_claims({"routeKey": route}, tenant_id, role, sub)
    if body is not None:
        event["body"] = body if isinstance(body, str) else json.dumps(body)
    res = app.handler(event)
    assert res["headers"]["Cache-Control"] == "no-store"
    return res["statusCode"], (json.loads(res["body"]) if res["body"] else None)


def s3():
    return boto3.client("s3", region_name=REGION)


def put(key, content_type="image/webp"):
    s3().put_object(Bucket=BUCKET, Key=key, Body=b"img", ContentType=content_type)


def test_get_without_an_image_answers_nulls(app):
    assert call(app, "GET /me/backdrop") == (200, {"url": None, "updatedAt": None})


def test_get_answers_a_one_hour_presigned_url_of_the_callers_own_key(app):
    put(f"admin/{TENANT_A}/{OWNER_A_SUB}/backdrop")
    status, body = call(app, "GET /me/backdrop")
    assert status == 200
    url = urlsplit(body["url"])
    assert url.netloc == f"{BUCKET}.s3.{REGION}.amazonaws.com"
    assert url.path == f"/admin/{TENANT_A}/{OWNER_A_SUB}/backdrop"
    assert "X-Amz-Expires=3600" in url.query
    assert body["updatedAt"].endswith("Z")
    datetime.fromisoformat(body["updatedAt"].replace("Z", "+00:00"))


def test_staff_have_their_own_image(app):
    put(f"admin/{TENANT_A}/{OWNER_A_SUB}/backdrop")
    assert call(app, "GET /me/backdrop", role="staff_user")[1]["url"] is None
    put(f"admin/{TENANT_A}/{STAFF_A_SUB}/backdrop")
    url = call(app, "GET /me/backdrop", role="staff_user")[1]["url"]
    assert f"/admin/{TENANT_A}/{STAFF_A_SUB}/backdrop" in url


def test_upload_is_a_presigned_post_bound_to_key_type_and_size(app):
    status, body = call(app, "POST /me/backdrop/upload", {"contentType": "image/png", "size": 2048})
    assert status == 200
    assert set(body) == {"url", "fields"}
    assert body["url"] == f"https://{BUCKET}.s3.{REGION}.amazonaws.com/"
    fields = body["fields"]
    assert fields["key"] == f"admin/{TENANT_A}/{OWNER_A_SUB}/backdrop"
    assert fields["Content-Type"] == "image/png"
    policy = json.loads(__import__("base64").b64decode(fields["policy"]))
    conditions = policy["conditions"]
    assert {"Content-Type": "image/png"} in conditions
    assert ["content-length-range", 1, MAX] in conditions
    assert {"key": f"admin/{TENANT_A}/{OWNER_A_SUB}/backdrop"} in conditions
    assert {"bucket": BUCKET} in conditions
    expires = datetime.strptime(policy["expiration"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert 0 < (expires - datetime.now(timezone.utc)).total_seconds() <= 300


@pytest.mark.parametrize("content_type", ["image/jpeg", "image/png", "image/webp", "IMAGE/WEBP"])
def test_upload_accepts_the_three_image_types(app, content_type):
    status, body = call(app, "POST /me/backdrop/upload", {"contentType": content_type, "size": MAX})
    assert status == 200 and body["fields"]["Content-Type"] == content_type.lower()


@pytest.mark.parametrize("body, field", [
    ({"contentType": "image/gif", "size": 10}, "contentType"),
    ({"contentType": "image/svg+xml", "size": 10}, "contentType"),
    ({"contentType": "", "size": 10}, "contentType"),
    ({"size": 10}, "contentType"),
    ({"contentType": "image/png"}, "size"),
    ({"contentType": "image/png", "size": MAX + 1}, "size"),
    ({"contentType": "image/png", "size": 0}, "size"),
    ({"contentType": "image/png", "size": -5}, "size"),
    ({"contentType": "image/png", "size": 12.5}, "size"),
    ({"contentType": "image/png", "size": "100"}, "size"),
    ({"contentType": "image/png", "size": True}, "size"),
])
def test_upload_rejects_bad_input(app, body, field):
    status, error = call(app, "POST /me/backdrop/upload", body)
    assert status == 400
    assert error["error"].startswith(field)


def test_upload_rejects_unknown_fields_and_bad_json(app):
    status, error = call(app, "POST /me/backdrop/upload", {"contentType": "image/png", "size": 1, "key": "x"})
    assert status == 400 and error["code"] == "unsupported_fields"
    assert call(app, "POST /me/backdrop/upload", "{nope")[0] == 400
    assert call(app, "POST /me/backdrop/upload")[0] == 400


def test_delete_removes_only_the_callers_image_and_is_idempotent(app):
    put(f"admin/{TENANT_A}/{OWNER_A_SUB}/backdrop")
    put(f"admin/{TENANT_A}/{STAFF_A_SUB}/backdrop")
    assert call(app, "DELETE /me/backdrop") == (204, None)
    assert call(app, "DELETE /me/backdrop") == (204, None)
    keys = [o["Key"] for o in s3().list_objects_v2(Bucket=BUCKET).get("Contents", [])]
    assert keys == [f"admin/{TENANT_A}/{STAFF_A_SUB}/backdrop"]
    assert call(app, "GET /me/backdrop")[1] == {"url": None, "updatedAt": None}


def test_missing_object_answering_403_reads_as_no_image(app, monkeypatch):
    class Denied:
        def head_object(self, **kwargs):
            raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject")
    monkeypatch.setattr(app, "_s3", Denied())
    assert call(app, "GET /me/backdrop") == (200, {"url": None, "updatedAt": None})


def test_s3_failure_is_a_sanitised_503(app, monkeypatch):
    class Broken:
        def head_object(self, **kwargs):
            raise ClientError({"Error": {"Code": "InternalError", "Message": "boom"}}, "HeadObject")

        def delete_object(self, **kwargs):
            raise ClientError({"Error": {"Code": "InternalError", "Message": "boom"}}, "DeleteObject")
    monkeypatch.setattr(app, "_s3", Broken())
    assert call(app, "GET /me/backdrop") == (503, {"error": "service_unavailable"})
    assert call(app, "DELETE /me/backdrop") == (503, {"error": "service_unavailable"})


def test_no_claims_is_401_and_no_tenant_is_403(app):
    res = app.handler({"routeKey": "GET /me/backdrop"})
    assert res["statusCode"] == 401
    event = {"routeKey": "GET /me/backdrop",
             "requestContext": {"authorizer": {"jwt": {"claims": {"sub": "x", "cognito:groups": ["owner_user"]}}}}}
    assert app.handler(event)["statusCode"] == 403


def test_inactive_tenant_is_refused(app):
    boto3.resource("dynamodb", region_name=REGION).Table(TENANT_TABLE).put_item(
        Item=tenant_row(TENANT_B, status="suspended"))
    from shared import tenant as shared_tenant
    shared_tenant.reset_caches()
    status, body = call(app, "GET /me/backdrop", tenant_id=TENANT_B)
    assert status == 403 and body["error"] == "tenant_inactive"


def test_a_sub_that_could_leave_the_callers_prefix_is_refused(app):
    status, body = call(app, "POST /me/backdrop/upload", {"contentType": "image/png", "size": 1}, sub="../other")
    assert status == 403 and body["error"] == "no_tenant"


def test_unknown_route_is_404(app):
    res = app.handler(with_tenant_claims({"routeKey": "PUT /me/backdrop"}, TENANT_A))
    assert res["statusCode"] == 404

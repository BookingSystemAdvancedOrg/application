"""Personal background image library (functions/user-backdrop): presigned POST
upload with strict input, complete (check, thumbnail, item), the 20 cap,
list newest first, selection, delete, migration of the first version's
single image, keys from the token only, and the error shapes."""

import base64
import importlib.util
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from PIL import Image

from shared import dynamo as shared_dynamo
from tenant_support import (OWNER_A_SUB, REGION, TENANT_A, TENANT_B, TENANT_TABLE,
                            seed_tenancy, tenant_row, with_tenant_claims)

ROOT = Path(__file__).parents[1]
BUCKET = "test-user-backdrops"
TABLE = "test-user-backdrops-table"
MAX = 10 * 1024 * 1024
PREFIX = f"admin/{TENANT_A}/{OWNER_A_SUB}/"


def load():
    spec = importlib.util.spec_from_file_location("user_backdrop_app", ROOT / "functions" / "user-backdrop" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def image_bytes(fmt="JPEG", size=(1200, 800), mode="RGB", exif_orientation=None):
    img = Image.new(mode, size, (120, 30, 30) if mode == "RGB" else (120, 30, 30, 200))
    out = io.BytesIO()
    kwargs = {}
    if exif_orientation:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        kwargs["exif"] = exif.tobytes()
    img.save(out, fmt, **kwargs)
    return out.getvalue()


@pytest.fixture
def app(monkeypatch):
    with mock_aws():
        for key, value in {"AWS_DEFAULT_REGION": REGION, "AWS_REGION": REGION, "ENVIRONMENT": "dev",
                           "USER_BACKDROPS_BUCKET_NAME": BUCKET, "USER_BACKDROPS_TABLE_NAME": TABLE,
                           "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing"}.items():
            monkeypatch.setenv(key, value)
        shared_dynamo._resource = None
        shared_dynamo._client = None
        seed_tenancy(monkeypatch)
        boto3.client("s3", region_name=REGION).create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("PK", "SK", "tenantId")],
            GlobalSecondaryIndexes=[{"IndexName": "byTenant", "Projection": {"ProjectionType": "KEYS_ONLY"},
                                     "KeySchema": [{"AttributeName": "tenantId", "KeyType": "HASH"},
                                                   {"AttributeName": "PK", "KeyType": "RANGE"}]}])
        yield load()
        shared_dynamo._resource = None
        shared_dynamo._client = None


def call(app, route, body=None, role="owner_user", tenant_id=TENANT_A, sub=None, image_id=None):
    event = with_tenant_claims({"routeKey": route}, tenant_id, role, sub)
    if body is not None:
        event["body"] = body if isinstance(body, str) else json.dumps(body)
    if image_id is not None:
        event["pathParameters"] = {"id": image_id}
    res = app.handler(event)
    assert res["headers"]["Cache-Control"] == "no-store"
    return res["statusCode"], (json.loads(res["body"]) if res["body"] else None)


def s3():
    return boto3.client("s3", region_name=REGION)


def keys(prefix=""):
    return sorted(o["Key"] for o in s3().list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("Contents", []))


def upload(app, data=None, content_type="image/jpeg", **claims):
    data = image_bytes() if data is None else data
    status, body = call(app, "POST /me/backdrops/upload",
                        {"contentType": content_type, "size": len(data), "width": 1200, "height": 800}, **claims)
    assert status == 200, body
    s3().put_object(Bucket=BUCKET, Key=body["fields"]["key"], Body=data, ContentType=content_type)
    return body["id"]


def add(app, data=None, content_type="image/jpeg", **claims):
    image_id = upload(app, data, content_type, **claims)
    status, body = call(app, "POST /me/backdrops/{id}/complete", image_id=image_id, **claims)
    assert status == 200, body
    return body["item"]


def test_empty_library(app):
    assert call(app, "GET /me/backdrops") == (200, {"items": [], "selectedId": None, "max": 20})


def test_upload_is_a_presigned_post_bound_to_key_type_and_size(app):
    status, body = call(app, "POST /me/backdrops/upload",
                        {"contentType": "image/png", "size": 2048, "width": 10, "height": 10})
    assert status == 200 and set(body) == {"id", "url", "fields"}
    assert body["url"] == f"https://{BUCKET}.s3.{REGION}.amazonaws.com/"
    key = f"{PREFIX}backdrops/{body['id']}"
    assert body["fields"]["key"] == key and body["fields"]["Content-Type"] == "image/png"
    policy = json.loads(base64.b64decode(body["fields"]["policy"]))
    assert {"Content-Type": "image/png"} in policy["conditions"]
    assert ["content-length-range", 1, MAX] in policy["conditions"]
    assert {"key": key} in policy["conditions"]
    expires = datetime.strptime(policy["expiration"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert 0 < (expires - datetime.now(timezone.utc)).total_seconds() <= 300


@pytest.mark.parametrize("body, field", [
    ({"contentType": "image/gif", "size": 10, "width": 1, "height": 1}, "contentType"),
    ({"size": 10, "width": 1, "height": 1}, "contentType"),
    ({"contentType": "image/png", "width": 1, "height": 1}, "size"),
    ({"contentType": "image/png", "size": MAX + 1, "width": 1, "height": 1}, "size"),
    ({"contentType": "image/png", "size": 0, "width": 1, "height": 1}, "size"),
    ({"contentType": "image/png", "size": 12.5, "width": 1, "height": 1}, "size"),
    ({"contentType": "image/png", "size": True, "width": 1, "height": 1}, "size"),
    ({"contentType": "image/png", "size": 10, "height": 1}, "width"),
    ({"contentType": "image/png", "size": 10, "width": 0, "height": 1}, "width"),
    ({"contentType": "image/png", "size": 10, "width": 1, "height": "1"}, "height"),
])
def test_upload_rejects_bad_input(app, body, field):
    status, error = call(app, "POST /me/backdrops/upload", body)
    assert status == 400 and error["error"].startswith(field)


def test_upload_rejects_unknown_fields_and_bad_json(app):
    status, error = call(app, "POST /me/backdrops/upload",
                         {"contentType": "image/png", "size": 1, "width": 1, "height": 1, "key": "x"})
    assert status == 400 and error["code"] == "unsupported_fields"
    assert call(app, "POST /me/backdrops/upload", "{nope")[0] == 400


def test_complete_makes_a_thumbnail_and_the_item(app):
    item = add(app, image_bytes("PNG", (1600, 900)), "image/png")
    assert set(item) == {"id", "url", "thumbUrl", "uploadedAt", "width", "height"}
    assert (item["width"], item["height"]) == (1600, 900)
    assert urlsplit(item["url"]).path == f"/{PREFIX}backdrops/{item['id']}"
    assert urlsplit(item["thumbUrl"]).path == f"/{PREFIX}backdrops/{item['id']}.thumb.webp"
    assert "X-Amz-Expires=3600" in item["url"]
    datetime.fromisoformat(item["uploadedAt"].replace("Z", "+00:00"))
    thumb = s3().get_object(Bucket=BUCKET, Key=f"{PREFIX}backdrops/{item['id']}.thumb.webp")
    assert thumb["ContentType"] == "image/webp"
    with Image.open(io.BytesIO(thumb["Body"].read())) as img:
        assert img.format == "WEBP" and img.size == (400, 225)


def test_complete_is_idempotent(app):
    image_id = upload(app)
    first = call(app, "POST /me/backdrops/{id}/complete", image_id=image_id)
    second = call(app, "POST /me/backdrops/{id}/complete", image_id=image_id)
    assert first[0] == second[0] == 200 and first[1]["item"]["id"] == second[1]["item"]["id"]
    assert len(call(app, "GET /me/backdrops")[1]["items"]) == 1


def test_exif_rotation_swaps_the_reported_size(app):
    item = add(app, image_bytes("JPEG", (1200, 800), exif_orientation=6))
    assert (item["width"], item["height"]) == (800, 1200)


def test_webp_with_transparency(app):
    item = add(app, image_bytes("WEBP", (300, 200), mode="RGBA"), "image/webp")
    assert (item["width"], item["height"]) == (300, 200)


def test_complete_of_a_missing_upload_is_400(app):
    status, body = call(app, "POST /me/backdrops/upload",
                        {"contentType": "image/png", "size": 10, "width": 1, "height": 1})
    assert call(app, "POST /me/backdrops/{id}/complete", image_id=body["id"]) == (400, {"error": "upload_missing"})


def test_complete_of_a_non_image_deletes_it(app):
    image_id = upload(app, b"<svg>not an image</svg>", "image/png")
    assert call(app, "POST /me/backdrops/{id}/complete", image_id=image_id) == (400, {"error": "not_an_image"})
    assert keys() == []
    assert call(app, "GET /me/backdrops")[1]["items"] == []


def test_complete_of_a_gif_disguised_as_png_is_refused(app):
    image_id = upload(app, image_bytes("GIF", (10, 10), mode="RGB"), "image/png")
    assert call(app, "POST /me/backdrops/{id}/complete", image_id=image_id)[0] == 400
    assert keys() == []


def test_unknown_or_malformed_ids_are_404(app):
    assert call(app, "POST /me/backdrops/{id}/complete", image_id="../x")[0] == 404
    assert call(app, "DELETE /me/backdrops/{id}", image_id="nope")[0] == 404


def test_list_is_newest_first_and_per_user(app):
    a, b = add(app), add(app)
    add(app, role="staff_user")
    items = call(app, "GET /me/backdrops")[1]["items"]
    assert [i["id"] for i in items] == [b["id"], a["id"]]
    assert len(call(app, "GET /me/backdrops", role="staff_user")[1]["items"]) == 1


def test_limit_of_twenty(app, monkeypatch):
    monkeypatch.setattr(app, "MAX_ITEMS", 2)
    add(app)
    pending = upload(app)       # issued while there was room
    add(app)
    status, body = call(app, "POST /me/backdrops/upload",
                        {"contentType": "image/jpeg", "size": 10, "width": 1, "height": 1})
    assert (status, body) == (409, {"error": "limit_reached", "max": 2})
    assert call(app, "POST /me/backdrops/{id}/complete", image_id=pending) == (409, {"error": "limit_reached", "max": 2})
    assert f"{PREFIX}backdrops/{pending}" not in keys()
    assert call(app, "GET /me/backdrops")[1]["max"] == 2


def test_select_and_clear(app):
    item = add(app)
    assert call(app, "PUT /me/backdrops/selected", {"id": item["id"]}) == (200, {"selectedId": item["id"]})
    assert call(app, "GET /me/backdrops")[1]["selectedId"] == item["id"]
    assert call(app, "PUT /me/backdrops/selected", {"id": None}) == (200, {"selectedId": None})
    assert call(app, "GET /me/backdrops")[1]["selectedId"] is None


def test_select_someone_elses_or_unknown_image_is_404(app):
    other = add(app, role="staff_user")
    assert call(app, "PUT /me/backdrops/selected", {"id": other["id"]})[0] == 404
    assert call(app, "PUT /me/backdrops/selected", {"id": "0" * 26})[0] == 404
    assert call(app, "PUT /me/backdrops/selected", {"id": 5})[0] == 404
    assert call(app, "PUT /me/backdrops/selected", {})[0] == 400


def test_delete_removes_image_thumbnail_item_and_selection(app):
    keep, gone = add(app), add(app)
    call(app, "PUT /me/backdrops/selected", {"id": gone["id"]})
    assert call(app, "DELETE /me/backdrops/{id}", image_id=gone["id"]) == (204, None)
    assert call(app, "DELETE /me/backdrops/{id}", image_id=gone["id"]) == (204, None)
    assert keys() == [f"{PREFIX}backdrops/{keep['id']}", f"{PREFIX}backdrops/{keep['id']}.thumb.webp"]
    body = call(app, "GET /me/backdrops")[1]
    assert [i["id"] for i in body["items"]] == [keep["id"]] and body["selectedId"] is None


def test_deleting_another_image_keeps_the_selection(app):
    keep, other = add(app), add(app)
    call(app, "PUT /me/backdrops/selected", {"id": keep["id"]})
    call(app, "DELETE /me/backdrops/{id}", image_id=other["id"])
    assert call(app, "GET /me/backdrops")[1]["selectedId"] == keep["id"]


def test_first_version_image_moves_into_the_library(app):
    s3().put_object(Bucket=BUCKET, Key=f"{PREFIX}backdrop", Body=image_bytes("PNG", (800, 600)),
                    ContentType="image/png")
    body = call(app, "GET /me/backdrops")[1]
    assert len(body["items"]) == 1 and body["selectedId"] == body["items"][0]["id"]
    assert (body["items"][0]["width"], body["items"][0]["height"]) == (800, 600)
    image_id = body["items"][0]["id"]
    assert keys() == [f"{PREFIX}backdrops/{image_id}", f"{PREFIX}backdrops/{image_id}.thumb.webp"]
    # once only: later GETs don't look again
    s3().put_object(Bucket=BUCKET, Key=f"{PREFIX}backdrop", Body=image_bytes(), ContentType="image/jpeg")
    assert len(call(app, "GET /me/backdrops")[1]["items"]) == 1


def test_broken_first_version_image_is_just_deleted(app):
    s3().put_object(Bucket=BUCKET, Key=f"{PREFIX}backdrop", Body=b"junk", ContentType="image/png")
    assert call(app, "GET /me/backdrops")[1] == {"items": [], "selectedId": None, "max": 20}
    assert keys() == []


def test_missing_object_answering_403_reads_as_missing(app, monkeypatch):
    class Denied:
        def head_object(self, **kwargs):
            raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject")
    monkeypatch.setattr(app, "_s3", Denied())
    assert call(app, "GET /me/backdrops") == (200, {"items": [], "selectedId": None, "max": 20})


def test_s3_failure_is_a_sanitised_503(app, monkeypatch):
    class Broken:
        def head_object(self, **kwargs):
            raise ClientError({"Error": {"Code": "InternalError", "Message": "boom"}}, "HeadObject")

        def delete_object(self, **kwargs):
            raise ClientError({"Error": {"Code": "InternalError", "Message": "boom"}}, "DeleteObject")
    monkeypatch.setattr(app, "_s3", Broken())
    assert call(app, "GET /me/backdrops") == (503, {"error": "service_unavailable"})
    assert call(app, "DELETE /me/backdrops/{id}", image_id="0" * 26) == (503, {"error": "service_unavailable"})


def test_items_carry_the_tenant_for_purge(app):
    add(app)
    rows = boto3.resource("dynamodb", region_name=REGION).Table(TABLE).scan()["Items"]
    assert rows and all(r["tenantId"] == TENANT_A and r["PK"] == f"USER#{TENANT_A}#{OWNER_A_SUB}" for r in rows)


def test_no_claims_is_401_and_no_tenant_is_403(app):
    assert app.handler({"routeKey": "GET /me/backdrops"})["statusCode"] == 401
    event = {"routeKey": "GET /me/backdrops",
             "requestContext": {"authorizer": {"jwt": {"claims": {"sub": "x", "cognito:groups": ["owner_user"]}}}}}
    assert app.handler(event)["statusCode"] == 403


def test_inactive_tenant_is_refused(app):
    boto3.resource("dynamodb", region_name=REGION).Table(TENANT_TABLE).put_item(
        Item=tenant_row(TENANT_B, status="suspended"))
    from shared import tenant as shared_tenant
    shared_tenant.reset_caches()
    status, body = call(app, "GET /me/backdrops", tenant_id=TENANT_B)
    assert status == 403 and body["error"] == "tenant_inactive"


def test_a_sub_that_could_leave_the_callers_prefix_is_refused(app):
    status, body = call(app, "GET /me/backdrops", sub="../other")
    assert status == 403 and body["error"] == "no_tenant"


def test_old_single_image_routes_are_gone(app):
    assert app.handler(with_tenant_claims({"routeKey": "GET /me/backdrop"}, TENANT_A))["statusCode"] == 404


# ----------------------------------------------------------- profile picture

AVATAR_MAX = 5 * 1024 * 1024


def stage_avatar(app, data, content_type="image/png", **claims):
    status, body = call(app, "POST /me/avatar/upload", {"contentType": content_type, "size": len(data)}, **claims)
    assert status == 200, body
    s3().put_object(Bucket=BUCKET, Key=body["fields"]["key"], Body=data, ContentType=content_type)
    return body


def test_no_picture_yet(app):
    assert call(app, "GET /me/avatar") == (200, {"url": None})


def test_avatar_upload_is_a_presigned_post_to_the_staging_key(app):
    status, body = call(app, "POST /me/avatar/upload", {"contentType": "image/jpeg", "size": 1000})
    assert status == 200 and set(body) == {"url", "fields"}
    assert body["fields"]["key"] == f"{PREFIX}avatar.upload"
    policy = json.loads(base64.b64decode(body["fields"]["policy"]))
    assert ["content-length-range", 1, AVATAR_MAX] in policy["conditions"]
    assert {"Content-Type": "image/jpeg"} in policy["conditions"]


@pytest.mark.parametrize("body, field", [
    ({"contentType": "image/gif", "size": 10}, "contentType"),
    ({"contentType": "image/png", "size": AVATAR_MAX + 1}, "size"),
    ({"contentType": "image/png"}, "size"),
])
def test_avatar_upload_rejects_bad_input(app, body, field):
    status, error = call(app, "POST /me/avatar/upload", body)
    assert status == 400 and error["error"].startswith(field)


def test_avatar_complete_makes_a_256_square_webp(app):
    stage_avatar(app, image_bytes("PNG", (500, 300)))
    status, body = call(app, "POST /me/avatar/complete")
    assert status == 200 and urlsplit(body["url"]).path == f"/{PREFIX}avatar.webp"
    assert keys() == [f"{PREFIX}avatar.webp"]
    obj = s3().get_object(Bucket=BUCKET, Key=f"{PREFIX}avatar.webp")
    with Image.open(io.BytesIO(obj["Body"].read())) as img:
        assert img.format == "WEBP" and img.size == (256, 256)
    assert urlsplit(call(app, "GET /me/avatar")[1]["url"]).path == f"/{PREFIX}avatar.webp"


def test_avatar_complete_without_upload_or_with_junk(app):
    assert call(app, "POST /me/avatar/complete") == (400, {"error": "upload_missing"})
    stage_avatar(app, b"junk")
    assert call(app, "POST /me/avatar/complete") == (400, {"error": "not_an_image"})
    assert keys() == []


def test_avatar_is_per_user_and_delete_is_own(app):
    stage_avatar(app, image_bytes())
    call(app, "POST /me/avatar/complete")
    stage_avatar(app, image_bytes(), role="staff_user")
    call(app, "POST /me/avatar/complete", role="staff_user")
    assert call(app, "DELETE /me/avatar", role="staff_user") == (204, None)
    assert call(app, "GET /me/avatar", role="staff_user")[1]["url"] is None
    assert call(app, "GET /me/avatar")[1]["url"] is not None

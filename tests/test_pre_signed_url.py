import importlib.util
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError


APP_PATH = (
    Path(__file__).parents[1]
    / "functions"
    / "pre-signed-url"
    / "app.py"
)
BUCKET_NAME = "test-menu-images"
LOCATION_ID = "location-id"
IMAGE_ID = "11111111-2222-3333-4444-555555555555"
UPLOAD_URL = "https://test-menu-images.s3.amazonaws.com/presigned-upload"


def make_event(
    *,
    method="GET",
    groups='["owner_user"]',
    sub="caller-sub",
    query=None,
):
    if query is None:
        query = {
            "locationId": LOCATION_ID,
            "contentType": "image/webp",
        }
    return {
        "requestContext": {
            "http": {"method": method},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": sub,
                        "cognito:groups": groups,
                    }
                }
            },
        },
        "queryStringParameters": query,
    }


def response_body(response):
    return json.loads(response["body"])


def assert_response(response, status_code, body):
    assert response["statusCode"] == status_code
    assert response["headers"]["Content-Type"] == "application/json"
    assert response["headers"]["Cache-Control"] == "no-store"
    assert response_body(response) == body


@pytest.fixture
def app_and_s3(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("MENU_IMAGES_BUCKET_NAME", BUCKET_NAME)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-north-1")

    spec = importlib.util.spec_from_file_location(
        "pre_signed_url_app",
        APP_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    s3 = Mock()
    s3.generate_presigned_url.return_value = UPLOAD_URL
    module._s3 = s3
    monkeypatch.setattr(module, "_new_image_id", lambda: IMAGE_ID)
    return module, s3


def test_missing_claims_returns_401_before_using_s3(app_and_s3):
    app, s3 = app_and_s3
    event = make_event()
    del event["requestContext"]["authorizer"]

    response = app.handler(event, None)

    assert_response(
        response,
        401,
        {"error": "no JWT claims on this request"},
    )
    s3.generate_presigned_url.assert_not_called()


@pytest.mark.parametrize("claims", [None, [], "invalid"])
def test_malformed_claims_return_401_before_using_s3(
    app_and_s3,
    claims,
):
    app, s3 = app_and_s3
    event = make_event()
    event["requestContext"]["authorizer"]["jwt"]["claims"] = claims

    response = app.handler(event, None)

    assert_response(
        response,
        401,
        {"error": "no JWT claims on this request"},
    )
    s3.generate_presigned_url.assert_not_called()


@pytest.mark.parametrize("sub", [None, "", "   ", 123])
def test_missing_subject_returns_401_before_using_s3(app_and_s3, sub):
    app, s3 = app_and_s3

    response = app.handler(make_event(sub=sub), None)

    assert_response(
        response,
        401,
        {"error": "JWT is missing a subject"},
    )
    s3.generate_presigned_url.assert_not_called()


@pytest.mark.parametrize(
    "groups",
    [
        None,
        "",
        "[]",
        '["unknown"]',
        '["staff"]',
        '["staff_user"]',
        123,
    ],
)
def test_wrong_group_returns_403_before_using_s3(app_and_s3, groups):
    app, s3 = app_and_s3

    response = app.handler(make_event(groups=groups), None)

    assert_response(response, 403, {"error": "forbidden"})
    s3.generate_presigned_url.assert_not_called()


def test_authorization_happens_before_method_and_query_validation(app_and_s3):
    app, s3 = app_and_s3

    response = app.handler(
        make_event(method="POST", groups='["unknown"]', query={}),
        None,
    )

    assert_response(response, 403, {"error": "forbidden"})
    s3.generate_presigned_url.assert_not_called()


@pytest.mark.parametrize(
    "groups",
    ['["owner_user"]', '["super_user"]'],
)
def test_all_admin_groups_can_request_an_upload(
    app_and_s3,
    groups,
):
    app, _ = app_and_s3

    response = app.handler(make_event(groups=groups), None)

    assert response["statusCode"] == 200


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", ""])
def test_non_get_method_returns_405_without_using_s3(
    app_and_s3,
    method,
):
    app, s3 = app_and_s3

    response = app.handler(make_event(method=method), None)

    assert_response(response, 405, {"error": "method not allowed"})
    assert response["headers"]["Allow"] == "GET"
    s3.generate_presigned_url.assert_not_called()


@pytest.mark.parametrize("query", [None, [], "invalid"])
def test_missing_or_malformed_query_returns_400_without_using_s3(
    app_and_s3,
    query,
):
    app, s3 = app_and_s3
    event = make_event()
    event["queryStringParameters"] = query

    response = app.handler(event, None)

    assert_response(
        response,
        400,
        {"error": "query parameters are required"},
    )
    s3.generate_presigned_url.assert_not_called()


def test_unknown_query_parameters_are_rejected_without_using_s3(app_and_s3):
    app, s3 = app_and_s3
    query = {
        "locationId": LOCATION_ID,
        "contentType": "image/webp",
        "key": "client-controlled-key",
        "distributionId": "client-controlled-distribution",
    }

    response = app.handler(make_event(query=query), None)

    assert_response(
        response,
        400,
        {
            "error": (
                "unsupported query parameters: distributionId, key"
            )
        },
    )
    s3.generate_presigned_url.assert_not_called()


@pytest.mark.parametrize(
    "location_id",
    [
        None,
        "",
        "   ",
        123,
        ".hidden",
        "../other-location",
        "location/other",
        "location?query",
        "location id",
        "x" * 129,
        "sodermalm-å",
    ],
)
def test_invalid_location_id_returns_400_without_using_s3(
    app_and_s3,
    location_id,
):
    app, s3 = app_and_s3
    query = {
        "locationId": location_id,
        "contentType": "image/webp",
    }

    response = app.handler(make_event(query=query), None)

    expected_error = (
        "locationId is required"
        if not isinstance(location_id, str) or not location_id.strip()
        else "locationId is invalid"
    )
    assert_response(response, 400, {"error": expected_error})
    s3.generate_presigned_url.assert_not_called()


@pytest.mark.parametrize(
    "content_type",
    [None, "", "   ", 123, "image/gif", "image/svg+xml", "text/html"],
)
def test_invalid_content_type_returns_400_without_using_s3(
    app_and_s3,
    content_type,
):
    app, s3 = app_and_s3
    query = {
        "locationId": LOCATION_ID,
        "contentType": content_type,
    }

    response = app.handler(make_event(query=query), None)

    expected_error = (
        "contentType is required"
        if not isinstance(content_type, str) or not content_type.strip()
        else (
            "contentType must be image/avif, image/jpeg, image/png, or image/webp"
        )
    )
    assert_response(response, 400, {"error": expected_error})
    s3.generate_presigned_url.assert_not_called()


@pytest.mark.parametrize(
    ("content_type", "extension"),
    [
        ("image/avif", "avif"),
        ("image/jpeg", "jpg"),
        ("image/png", "png"),
        ("image/webp", "webp"),
        (" IMAGE/PNG ", "png"),
    ],
)
def test_generates_location_scoped_presigned_put_url(
    app_and_s3,
    content_type,
    extension,
):
    app, s3 = app_and_s3
    query = {
        "locationId": f"  {LOCATION_ID}  ",
        "contentType": content_type,
    }

    response = app.handler(make_event(query=query), None)

    normalized_content_type = content_type.strip().lower()
    image_key = f"locations/{LOCATION_ID}/menu/{IMAGE_ID}.{extension}"
    assert_response(
        response,
        200,
        {
            "uploadUrl": UPLOAD_URL,
            "imageKey": image_key,
            "expiresIn": 300,
            "requiredHeaders": {
                "Content-Type": normalized_content_type,
            },
        },
    )
    s3.generate_presigned_url.assert_called_once_with(
        ClientMethod="put_object",
        Params={
            "Bucket": BUCKET_NAME,
            "Key": image_key,
            "ContentType": normalized_content_type,
        },
        ExpiresIn=300,
        HttpMethod="PUT",
    )


def test_each_request_receives_a_new_immutable_image_key(
    app_and_s3,
    monkeypatch,
):
    app, s3 = app_and_s3
    image_ids = iter(["first-image-id", "second-image-id"])
    monkeypatch.setattr(app, "_new_image_id", lambda: next(image_ids))

    first = app.handler(make_event(), None)
    second = app.handler(make_event(), None)

    first_key = response_body(first)["imageKey"]
    second_key = response_body(second)["imageKey"]
    assert first_key == f"locations/{LOCATION_ID}/menu/first-image-id.webp"
    assert second_key == f"locations/{LOCATION_ID}/menu/second-image-id.webp"
    assert first_key != second_key
    assert s3.generate_presigned_url.call_count == 2


def test_real_sigv4_presigner_binds_the_required_content_type(app_and_s3):
    app, _ = app_and_s3
    app._s3 = None
    image_key = f"locations/{LOCATION_ID}/menu/{IMAGE_ID}.webp"

    upload_url = app._presigned_put_url(image_key, "image/webp")
    parsed_url = urlsplit(upload_url)
    signed_query = parse_qs(parsed_url.query)

    assert parsed_url.scheme == "https"
    assert parsed_url.path == f"/{image_key}"
    assert signed_query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert signed_query["X-Amz-Expires"] == ["300"]
    assert signed_query["X-Amz-SignedHeaders"] == ["content-type;host"]


@pytest.mark.parametrize("invalid_url", [None, "", "   ", 123, {}])
def test_invalid_presigner_result_returns_sanitized_503(
    app_and_s3,
    invalid_url,
):
    app, s3 = app_and_s3
    s3.generate_presigned_url.return_value = invalid_url

    response = app.handler(make_event(), None)

    assert_response(
        response,
        503,
        {"error": "image upload service unavailable"},
    )


@pytest.mark.parametrize(
    "aws_error",
    [
        ClientError(
            {
                "Error": {
                    "Code": "AccessDenied",
                    "Message": "sensitive AWS message",
                }
            },
            "GeneratePresignedUrl",
        ),
        EndpointConnectionError(
            endpoint_url="https://s3.eu-north-1.amazonaws.com",
        ),
    ],
)
def test_presigner_failures_return_sanitized_503(
    app_and_s3,
    aws_error,
):
    app, s3 = app_and_s3
    s3.generate_presigned_url.side_effect = aws_error

    response = app.handler(make_event(), None)

    assert_response(
        response,
        503,
        {"error": "image upload service unavailable"},
    )
    assert "sensitive AWS message" not in response["body"]

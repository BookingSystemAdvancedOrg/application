"""pre-signed-url: an optional `size` (bytes) is capped and signed into the
PUT URL as Content-Length, so S3 refuses a body of any other size."""

from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
from botocore.config import Config

from test_pre_signed_url import BUCKET_NAME, LOCATION_ID, app_and_s3, make_event, response_body  # noqa: F401

MAX = 10 * 1024 * 1024


def _event(size):
    return make_event(query={"locationId": LOCATION_ID, "contentType": "image/png", "size": size})


def test_size_is_signed_as_content_length(app_and_s3):  # noqa: F811
    app, s3 = app_and_s3
    response = app.handler(_event("2048"), None)
    assert response["statusCode"] == 200
    params = s3.generate_presigned_url.call_args.kwargs["Params"]
    assert params["ContentLength"] == 2048
    assert params["ContentType"] == "image/png"
    assert response_body(response)["requiredHeaders"] == {"Content-Type": "image/png"}


def test_without_size_the_url_is_signed_as_before(app_and_s3):  # noqa: F811
    app, s3 = app_and_s3
    response = app.handler(make_event(), None)
    assert response["statusCode"] == 200
    assert "ContentLength" not in s3.generate_presigned_url.call_args.kwargs["Params"]


@pytest.mark.parametrize("size", ["0", str(MAX + 1), "-5", "abc", "1.5", "5000000000"])
def test_size_out_of_range_is_refused(app_and_s3, size):  # noqa: F811
    app, s3 = app_and_s3
    response = app.handler(_event(size), None)
    assert response["statusCode"] == 400
    s3.generate_presigned_url.assert_not_called()


def test_largest_allowed_size_is_accepted(app_and_s3):  # noqa: F811
    app, s3 = app_and_s3
    assert app.handler(_event(str(MAX)), None)["statusCode"] == 200


def test_real_signature_covers_content_length(app_and_s3, monkeypatch):  # noqa: F811
    app, _ = app_and_s3
    app._s3 = boto3.client("s3", region_name="eu-north-1", config=Config(signature_version="s3v4"))
    url = response_body(app.handler(_event("2048"), None))["uploadUrl"]
    signed = parse_qs(urlsplit(url).query)["X-Amz-SignedHeaders"][0].split(";")
    assert "content-length" in signed and "content-type" in signed

"""Error reports from Booqy Admin (functions/support-reports): what is
stored (and what never is), per-user rate limit, 10-minute dedupe, the
operator list and mark handled, and the SNS notification."""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key
import pytest
from moto import mock_aws

from tenant_support import OWNER_A_SUB, REGION, STAFF_A_SUB, TENANT_A, seed_tenancy, with_tenant_claims

ROOT = Path(__file__).parents[1]
REPORTS = "test-support-reports"
GUARD = "test-guard"


def load():
    spec = importlib.util.spec_from_file_location("support_reports_app",
                                                  ROOT / "functions" / "support-reports" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def env(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
        monkeypatch.setenv("ENVIRONMENT", "dev")
        monkeypatch.setenv("SUPPORT_REPORTS_TABLE_NAME", REPORTS)
        monkeypatch.setenv("REQUEST_GUARD_TABLE_NAME", GUARD)
        monkeypatch.setenv("CONSOLE_APP_URL", "https://ops.example")
        seed_tenancy(monkeypatch)
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(TableName=REPORTS, BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                                    {"AttributeName": "SK", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                               {"AttributeName": "SK", "AttributeType": "S"}])
        ddb.create_table(TableName=GUARD, BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}],
                         AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}])
        sns = boto3.client("sns", region_name=REGION)
        topic = sns.create_topic(Name="alerts")["TopicArn"]
        sqs = boto3.client("sqs", region_name=REGION)
        queue = sqs.create_queue(QueueName="alerts-inbox")["QueueUrl"]
        queue_arn = sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=queue_arn)
        monkeypatch.setenv("ALERT_TOPIC_ARN", topic)
        app = load()
        yield app, sqs, queue


def report(app, body, role="owner_user", sub=None, headers=None):
    event = with_tenant_claims({"routeKey": "POST /tenant/support/reports", "body": json.dumps(body),
                                "headers": headers or {"user-agent": "Mozilla/5.0 Test"}}, TENANT_A, role, sub)
    res = app.handler(event)
    return res["statusCode"], json.loads(res["body"])


def operator(app, route, body=None, path=None, query=None, groups="[platform_admin]"):
    event = {"routeKey": route, "pathParameters": path or {}, "queryStringParameters": query,
             "requestContext": {"authorizer": {"jwt": {"claims": {"sub": "op-1", "username": "ops@booqy.se",
                                                                  "cognito:groups": groups}}}}}
    if body is not None:
        event["body"] = json.dumps(body)
    res = app.handler(event)
    return res["statusCode"], json.loads(res["body"])


def stored(app):
    return boto3.resource("dynamodb", region_name=REGION).Table(REPORTS).query(
        KeyConditionExpression=Key("PK").eq("REPORTS"))["Items"]


BASE = {"code": "internal_error", "status": 500, "path": "/bokningar", "requestId": "abc123=", "appVersion": "1.4.1"}


def test_stores_the_report_and_notifies(env):
    app, sqs, queue = env
    status, body = report(app, BASE)
    assert status == 201 and body["duplicate"] is False
    [item] = stored(app)
    assert item["reportId"] == body["reportId"]
    assert item["tenantId"] == TENANT_A and item["userSub"] == OWNER_A_SUB and item["role"] == "owner_user"
    assert item["code"] == "internal_error" and int(item["status"]) == 500 and item["path"] == "/bokningar"
    assert item["requestId"] == "abc123=" and item["appVersion"] == "1.4.1"
    assert item["userAgent"] == "Mozilla/5.0 Test" and item["handled"] is False
    created = datetime.fromisoformat(item["createdAt"].replace("Z", "+00:00"))
    assert abs(int(item["ttl"]) - int((created + timedelta(days=180)).timestamp())) <= 1
    [message] = sqs.receive_message(QueueUrl=queue, MaxNumberOfMessages=10)["Messages"]
    text = json.loads(message["Body"])["Message"]
    assert "internal_error" in text and "abc123=" in text and "https://ops.example/reports" in text


def test_never_stores_secrets_or_form_contents(env):
    app, _, _ = env
    # Free text, tokens and form fields are refused outright.
    for extra in ({"message": "Bordet för Anna Svensson"}, {"token": "eyJhbGciOi"}, {"form": {"name": "x"}},
                  {"authorization": "Bearer x"}):
        status, body = report(app, {**BASE, **extra})
        assert status == 400 and body["code"] == "unsupported_fields"
    # A query string or fragment (reset codes, searches) never reaches the table,
    # and a long opaque path segment (a token in a path) is replaced.
    status, _ = report(app, {**BASE, "path": "/reset-password?code=123456#code=999&email=a@b.se"})
    assert status == 201
    status, _ = report(app, {**BASE, "code": "x_failed", "path": "/m/u/" + "a" * 80 + "/edit"})
    assert status == 201
    paths = sorted(i["path"] for i in stored(app))
    assert paths == ["/m/u/:x/edit", "/reset-password"]
    dumped = json.dumps(stored(app), default=str)
    for secret in ("123456", "999", "a@b.se", "Bearer", "eyJ", "a" * 80):
        assert secret not in dumped
    # The request id and version must look like ids, not carry text.
    assert report(app, {**BASE, "requestId": "has spaces and text"})[0] == 400
    assert report(app, {**BASE, "appVersion": "<script>"})[0] == 400
    assert report(app, {**BASE, "code": "Not a code"})[0] == 400


def test_same_report_within_ten_minutes_is_stored_once(env):
    app, sqs, queue = env
    first = report(app, BASE)[1]
    status, again = report(app, BASE)
    assert status == 200 and again == {"reportId": first["reportId"], "duplicate": True}
    assert len(stored(app)) == 1
    # Another page or code is a new report.
    assert report(app, {**BASE, "path": "/meny"})[0] == 201
    # Another user's identical report is their own.
    assert report(app, BASE, role="staff_user", sub=STAFF_A_SUB)[0] == 201
    assert len(stored(app)) == 3
    assert len(sqs.receive_message(QueueUrl=queue, MaxNumberOfMessages=10)["Messages"]) == 3


def test_dedupe_expires_after_ten_minutes(env, monkeypatch):
    app, _, _ = env
    report(app, BASE)
    later = datetime.now(timezone.utc) + timedelta(minutes=11)
    monkeypatch.setattr(app, "_now", lambda: later)
    status, body = report(app, BASE)
    assert status == 201 and body["duplicate"] is False
    assert len(stored(app)) == 2


def test_rate_limit_ten_an_hour_per_user(env):
    app, _, _ = env
    for n in range(10):
        assert report(app, {**BASE, "path": f"/sida{n}"})[0] == 201
    status, body = report(app, {**BASE, "path": "/sida10"})
    assert status == 429 and body["error"] == "too_many_requests"
    # The limited report left no dedupe marker behind: it is still not stored.
    assert report(app, {**BASE, "path": "/sida10"})[0] == 429
    # Another user is not affected.
    assert report(app, BASE, role="staff_user", sub=STAFF_A_SUB)[0] == 201


def test_operator_lists_newest_first_and_marks_handled(env, monkeypatch):
    app, _, _ = env
    start = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)
    for n in range(3):
        monkeypatch.setattr(app, "_now", lambda n=n: start + timedelta(minutes=n))
        report(app, {**BASE, "path": f"/p{n}"})
    status, body = operator(app, "GET /platform/support/reports")
    assert status == 200
    assert [i["path"] for i in body["items"]] == ["/p2", "/p1", "/p0"]
    assert body["cursor"] is None and "ttl" not in body["items"][0] and "PK" not in body["items"][0]
    target = body["items"][1]["reportId"]
    status, item = operator(app, "POST /platform/support/reports/{reportId}/handled", {"handled": True},
                            {"reportId": target})
    assert status == 200 and item["handled"] is True and item["handledBy"] == "ops@booqy.se"
    assert [i["path"] for i in operator(app, "GET /platform/support/reports", query={"status": "open"})[1]["items"]] \
        == ["/p2", "/p0"]
    assert [i["path"] for i in operator(app, "GET /platform/support/reports", query={"status": "handled"})[1]["items"]] \
        == ["/p1"]
    status, item = operator(app, "POST /platform/support/reports/{reportId}/handled", {"handled": False},
                            {"reportId": target})
    assert item["handled"] is False and item["handledBy"] is None
    missing = "20260101T000000000Z-00000000"
    assert operator(app, "POST /platform/support/reports/{reportId}/handled", {}, {"reportId": missing})[0] == 404


def test_operator_list_pages_with_a_cursor(env, monkeypatch):
    app, _, _ = env
    monkeypatch.setattr(app, "PAGE_SIZE", 2)
    for n in range(3):
        report(app, {**BASE, "path": f"/p{n}"})
    first = operator(app, "GET /platform/support/reports")[1]
    assert len(first["items"]) == 2 and first["cursor"]
    second = operator(app, "GET /platform/support/reports", query={"cursor": first["cursor"]})[1]
    assert len(second["items"]) == 1
    assert operator(app, "GET /platform/support/reports", query={"cursor": "!!"})[0] == 400


def test_only_operators_list_and_tenants_cannot(env):
    app, _, _ = env
    assert operator(app, "GET /platform/support/reports", groups="[other]")[0] == 403
    # A tenant token on the tenant route cannot reach the list (no such tenant route).
    event = with_tenant_claims({"routeKey": "GET /tenant/support/reports"}, TENANT_A)
    assert app.handler(event)["statusCode"] == 404


def test_notification_failure_still_stores(env, monkeypatch):
    app, _, _ = env
    monkeypatch.setenv("ALERT_TOPIC_ARN", "arn:aws:sns:eu-north-1:123456789012:missing")
    assert report(app, BASE)[0] == 201
    assert len(stored(app)) == 1

"""dlq-replay: redelivers each DLQ message to its owning Lambda, deletes it
only on success, escalates and parks poison messages."""

import importlib.util
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

APP = Path(__file__).parents[1] / "functions" / "dlq-replay" / "app.py"
REGION = "eu-north-1"
FN = f"arn:aws:lambda:{REGION}:123456789012:function"
LIFECYCLE = f"{FN}:dev-catering-lifecycle"
NOTIFICATION = f"{FN}:dev-notification"
PRINT_JOBS = f"{FN}:dev-print-jobs"
NO_SHOW = f"{FN}:dev-no-show-check"
STREAM = f"arn:aws:dynamodb:{REGION}:123456789012:table/dev-order/stream/2026-10-01T00:00:00.000"
QUEUE_NAMES = {
    "lifecycle_stream": "LIFECYCLE_STREAM_DLQ_URL",
    "notification_stream": "NOTIFICATION_STREAM_DLQ_URL",
    "print_stream": "PRINT_STREAM_DLQ_URL",
    "scheduled_invocation": "SCHEDULED_INVOCATION_DLQ_URL",
}


class FakeLambda:
    def __init__(self):
        self.calls = []
        self.results = {}   # FunctionName -> (payload, function_error)

    def invoke(self, FunctionName, Payload):
        self.calls.append((FunctionName, json.loads(Payload)))
        body, error = self.results.get(FunctionName, ("null", False))
        resp = {"StatusCode": 200, "Payload": io.BytesIO(body.encode())}
        if error:
            resp["FunctionError"] = "Unhandled"
        return resp


class FakeStreams:
    def __init__(self, records):
        self.records = records

    def get_shard_iterator(self, **kw):
        start = int(kw["SequenceNumber"])
        return {"ShardIterator": json.dumps([r for r in self.records if int(r["dynamodb"]["SequenceNumber"]) >= start])}

    def get_records(self, ShardIterator, Limit):
        return {"Records": json.loads(ShardIterator), "NextShardIterator": None}


class FakeSns:
    def __init__(self):
        self.published = []

    def publish(self, **kw):
        self.published.append(kw)


class Context:
    def get_remaining_time_in_millis(self):
        return 300_000


@pytest.fixture
def env(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                 "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        sqs = boto3.client("sqs", region_name=REGION)
        urls = {}
        for name, var in QUEUE_NAMES.items():
            urls[name] = sqs.create_queue(QueueName=f"dev-{name}-dlq")["QueueUrl"]
            monkeypatch.setenv(var, urls[name])
        monkeypatch.setenv("ENVIRONMENT", "dev")
        monkeypatch.setenv("CATERING_LIFECYCLE_FUNCTION_ARN", f"{LIFECYCLE}:live")
        monkeypatch.setenv("NOTIFICATION_FUNCTION_ARN", f"{NOTIFICATION}:live")
        monkeypatch.setenv("PRINT_JOBS_FUNCTION_ARN", f"{PRINT_JOBS}:live")
        monkeypatch.setenv("REPLAYABLE_FUNCTION_ARNS", json.dumps([LIFECYCLE, f"{LIFECYCLE}:live", NO_SHOW, f"{NO_SHOW}:live"]))
        monkeypatch.setenv("ALERT_TOPIC_ARN", f"arn:aws:sns:{REGION}:123456789012:dev-platform-alerts")

        spec = importlib.util.spec_from_file_location("dlq_replay_app", APP)
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        app.lam, app.sns = FakeLambda(), FakeSns()
        app.streams = FakeStreams([_record(n) for n in (100, 101, 102, 103)])
        yield app, sqs, urls


def _record(seq):
    return {"eventName": "INSERT", "dynamodb": {"SequenceNumber": str(seq), "Keys": {"PK": {"S": f"ORDER#{seq}"}}}}


def _pointer(first=None):
    first = first or datetime.now(timezone.utc) - timedelta(minutes=10)
    return {"DDBStreamBatchInfo": {
        "shardId": "shardId-1", "streamArn": STREAM, "batchSize": 2,
        "startSequenceNumber": "101", "endSequenceNumber": "102",
        "approximateArrivalOfFirstRecord": first.isoformat().replace("+00:00", "Z"),
        "approximateArrivalOfLastRecord": first.isoformat().replace("+00:00", "Z"),
    }}


def _left(sqs, url):
    attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]
    return int(attrs["ApproximateNumberOfMessages"]) + int(attrs["ApproximateNumberOfMessagesNotVisible"])


@pytest.mark.parametrize("queue,target", [
    ("lifecycle_stream", f"{LIFECYCLE}:live"),
    ("notification_stream", f"{NOTIFICATION}:live"),
    ("print_stream", f"{PRINT_JOBS}:live"),
])
def test_stream_pointer_rereads_the_failed_batch_and_deletes_on_success(env, queue, target):
    app, sqs, urls = env
    sqs.send_message(QueueUrl=urls[queue], MessageBody=json.dumps(_pointer()))

    stats = app.handler({"queue": queue}, Context())

    assert stats["replayed"] == 1
    assert app.lam.calls == [(target, {"Records": [
        {**_record(101), "eventSourceARN": STREAM}, {**_record(102), "eventSourceARN": STREAM}]})]
    assert _left(sqs, urls[queue]) == 0


def test_scheduler_dlq_message_goes_to_the_target_attribute_live_alias(env):
    app, sqs, urls = env
    sqs.send_message(QueueUrl=urls["scheduled_invocation"], MessageBody=json.dumps({"locationId": "loc-1"}),
                     MessageAttributes={"TARGET_ARN": {"DataType": "String", "StringValue": f"{NO_SHOW}:live"}})

    assert app.handler({"queue": "scheduled_invocation"}, Context())["replayed"] == 1
    assert app.lam.calls == [(f"{NO_SHOW}:live", {"locationId": "loc-1"})]
    assert _left(sqs, urls["scheduled_invocation"]) == 0


def test_lambda_on_failure_record_replays_the_request_payload(env):
    app, sqs, urls = env
    record = {"requestContext": {"functionArn": f"{LIFECYCLE}:live", "condition": "RetriesExhausted"},
              "requestPayload": {"action": "expire", "requestId": "r-1"}}
    sqs.send_message(QueueUrl=urls["scheduled_invocation"], MessageBody=json.dumps(record))

    app.handler({"queue": "scheduled_invocation"}, Context())

    assert app.lam.calls == [(f"{LIFECYCLE}:live", {"action": "expire", "requestId": "r-1"})]


def test_scheduler_message_without_target_defaults_to_catering_lifecycle(env):
    app, sqs, urls = env
    sqs.send_message(QueueUrl=urls["scheduled_invocation"], MessageBody=json.dumps({"action": "remind"}))

    app.handler({"queue": "scheduled_invocation"}, Context())

    assert app.lam.calls == [(f"{LIFECYCLE}:live", {"action": "remind"})]


def test_target_off_the_allow_list_is_escalated_and_dropped(env):
    app, sqs, urls = env
    record = {"requestContext": {"functionArn": f"{FN}:dev-tenant-purge"}, "requestPayload": {}}
    sqs.send_message(QueueUrl=urls["scheduled_invocation"], MessageBody=json.dumps(record))

    stats = app.handler({"queue": "scheduled_invocation"}, Context())

    assert stats["escalated"] == 1
    assert app.lam.calls == []
    assert "allow-list" in json.loads(app.sns.published[0]["Message"])["reason"]
    assert _left(sqs, urls["scheduled_invocation"]) == 0


@pytest.mark.parametrize("body", ["not json", json.dumps({"something": "else"})])
def test_malformed_stream_message_is_escalated_and_dropped(env, body):
    app, sqs, urls = env
    sqs.send_message(QueueUrl=urls["notification_stream"], MessageBody=body)

    assert app.handler({"queue": "notification_stream"}, Context())["escalated"] == 1
    assert app.lam.calls == []
    assert _left(sqs, urls["notification_stream"]) == 0


@pytest.mark.parametrize("result,error", [('{"errorMessage": "boom"}', True),
                                          ('{"batchItemFailures": [{"itemIdentifier": "101"}]}', False)])
def test_failed_or_partial_redelivery_leaves_the_message_for_the_next_run(env, result, error):
    app, sqs, urls = env
    app.lam.results[f"{NOTIFICATION}:live"] = (result, error)
    sqs.send_message(QueueUrl=urls["notification_stream"], MessageBody=json.dumps(_pointer()))

    stats = app.handler({"queue": "notification_stream"}, Context())

    assert stats["left_for_next_run"] == 1
    assert app.sns.published == []
    assert _left(sqs, urls["notification_stream"]) == 1


def test_replay_is_idempotent_a_second_run_finds_nothing(env):
    app, sqs, urls = env
    sqs.send_message(QueueUrl=urls["lifecycle_stream"], MessageBody=json.dumps(_pointer()))

    app.handler({"queue": "all"}, Context())
    second = app.handler({"queue": "all"}, Context())

    assert len(app.lam.calls) == 1
    assert second == {"replayed": 0, "reconciled": 0, "left_for_next_run": 0, "escalated": 0, "parked": 0}


def test_expired_lifecycle_pointer_asks_for_a_reconcile(env):
    app, sqs, urls = env
    old = datetime.now(timezone.utc) - timedelta(hours=30)
    sqs.send_message(QueueUrl=urls["lifecycle_stream"], MessageBody=json.dumps(_pointer(old)))

    assert app.handler({"queue": "lifecycle_stream"}, Context())["reconciled"] == 1
    target, payload = app.lam.calls[0]
    assert target == f"{LIFECYCLE}:live"
    assert payload["action"] == "reconcile" and payload["window"]["streamArn"] == STREAM
    assert _left(sqs, urls["lifecycle_stream"]) == 0


@pytest.mark.parametrize("queue,word", [("notification_stream", "notification"), ("print_stream", "kitchen ticket")])
def test_expired_notification_or_print_pointer_is_escalated_and_dropped(env, queue, word):
    app, sqs, urls = env
    old = datetime.now(timezone.utc) - timedelta(hours=30)
    sqs.send_message(QueueUrl=urls[queue], MessageBody=json.dumps(_pointer(old)))

    assert app.handler({"queue": queue}, Context())["escalated"] == 1
    assert app.lam.calls == []
    assert word in app.sns.published[0]["Subject"]
    assert _left(sqs, urls[queue]) == 0


def _received(sqs, url, count):
    m = sqs.receive_message(QueueUrl=url, AttributeNames=["All"], MessageAttributeNames=["All"])["Messages"][0]
    m["Attributes"]["ApproximateReceiveCount"] = str(count)
    return m


def test_poison_message_is_escalated_once_then_parked(env):
    app, sqs, urls = env
    url = urls["notification_stream"]
    sqs.send_message(QueueUrl=url, MessageBody=json.dumps(_pointer()))

    assert app.process("notification_stream", _received(sqs, url, 4)) == "escalated"
    assert len(app.sns.published) == 1
    assert "gave up after 3" in app.sns.published[0]["Subject"]
    assert app.lam.calls == []
    assert _left(sqs, url) == 1        # parked, not deleted


def test_parked_message_is_not_escalated_again(env):
    app, sqs, urls = env
    url = urls["notification_stream"]
    sqs.send_message(QueueUrl=url, MessageBody=json.dumps(_pointer()))

    assert app.process("notification_stream", _received(sqs, url, 5)) == "parked"
    assert app.sns.published == []
    assert app.lam.calls == []
    assert _left(sqs, url) == 1


def test_all_drains_every_queue_within_the_budget(env):
    app, sqs, urls = env
    for _ in range(3):
        sqs.send_message(QueueUrl=urls["lifecycle_stream"], MessageBody=json.dumps(_pointer()))
    sqs.send_message(QueueUrl=urls["scheduled_invocation"], MessageBody=json.dumps({"action": "remind"}))

    stats = app.handler({"queue": "all", "max": 2}, Context())

    assert stats["replayed"] == 2
    assert _left(sqs, urls["lifecycle_stream"]) + _left(sqs, urls["scheduled_invocation"]) == 2

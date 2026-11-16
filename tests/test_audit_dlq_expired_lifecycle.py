"""Audit #28: an expired catering-lifecycle stream pointer is handed to
catering-lifecycle as a reconcile, which today only logs, so the message
must not be dropped silently: an alert names the window."""

import json
from datetime import datetime, timedelta, timezone

from test_dlq_replay import LIFECYCLE, STREAM, Context, _left, _pointer, env  # noqa: F401 - env is a fixture


def test_expired_lifecycle_pointer_is_reconciled_and_alerted(env):  # noqa: F811
    app, sqs, urls = env
    old = datetime.now(timezone.utc) - timedelta(hours=30)
    sqs.send_message(QueueUrl=urls["lifecycle_stream"], MessageBody=json.dumps(_pointer(old)))

    stats = app.handler({"queue": "lifecycle_stream"}, Context())

    assert stats["reconciled"] == 1
    assert app.lam.calls[0][0] == f"{LIFECYCLE}:live"
    assert len(app.sns.published) == 1
    alert = app.sns.published[0]
    assert "lifecycle_stream" in alert["Subject"]
    assert json.loads(alert["Message"])["streamArn"] == STREAM
    assert _left(sqs, urls["lifecycle_stream"]) == 0


def test_expired_lifecycle_pointer_still_alerts_when_the_reconcile_fails(env):  # noqa: F811
    app, sqs, urls = env
    old = datetime.now(timezone.utc) - timedelta(hours=30)
    sqs.send_message(QueueUrl=urls["lifecycle_stream"], MessageBody=json.dumps(_pointer(old)))

    def failing(function_arn, payload):
        raise app.ReplayFailed("down")

    app.invoke = failing
    stats = app.handler({"queue": "lifecycle_stream"}, Context())

    assert stats["escalated"] == 1
    assert len(app.sns.published) == 1
    assert _left(sqs, urls["lifecycle_stream"]) == 0

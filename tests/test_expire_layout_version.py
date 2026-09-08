import copy
import importlib.util
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, call

import boto3
import pytest
from moto import mock_aws

from shared import dynamo as shared_dynamo


APP_PATH = (
    Path(__file__).parents[1]
    / "functions"
    / "expire-layout-version"
    / "app.py"
)
TABLE_NAME = "test-published-layout-snapshot"
LOCATION_ID = "location-id"
CALLER_SUB = "caller-sub"
CUTOVER_AT = "2026-10-05T01:00:00Z"
NOW = datetime(2026, 10, 5, 1, 0, tzinfo=timezone.utc)


def snapshot_item(version, *, is_current, **overrides):
    version_number = Decimal(str(version))
    item = {
        "PK": f"LOCATION#{LOCATION_ID}",
        "SK": f"LAYOUT#v{version}",
        "version": version_number,
        "label": f"Version {version}",
        "isCurrent": is_current,
        "effectiveFrom": (
            "2026-09-01T01:00:00Z" if is_current else None
        ),
        "effectiveTo": None,
        "expiresAt": None,
        "elements": [{"elementId": f"element-{version}"}],
        "validPositions": [],
        "createdBy": "publisher-sub",
        "createdAt": "2026-09-01T00:00:00Z",
        "updatedBy": "activation-sub",
        "updatedAt": "2026-09-07T10:30:00Z",
    }
    item.update(overrides)
    return item


def outgoing_snapshot(**overrides):
    return snapshot_item(
        1,
        is_current=True,
        effectiveTo=CUTOVER_AT,
        expiresAt=CUTOVER_AT,
        **overrides,
    )


def target_snapshot(**overrides):
    return snapshot_item(
        2,
        is_current=False,
        effectiveFrom=CUTOVER_AT,
        effectiveTo=None,
        expiresAt=None,
        **overrides,
    )


def state_key():
    return {
        "PK": f"LOCATION#{LOCATION_ID}",
        "SK": "LAYOUT#ACTIVATION",
    }


def schedule_arn(name):
    return (
        "arn:aws:scheduler:eu-north-1:123456789012:"
        f"schedule/default/{name}"
    )


def pending_state(
    app,
    *,
    current_version=1,
    pending_version=2,
    operation_revision=2,
    status="scheduled",
    cutover_at=CUTOVER_AT,
):
    token = app._activation_token(
        LOCATION_ID,
        current_version,
        pending_version,
        operation_revision,
        cutover_at,
    )
    name = app._schedule_name(token)
    state = {
        **state_key(),
        "recordType": "layoutActivationState",
        "currentVersion": Decimal(str(current_version)),
        "revision": Decimal(
            str(
                operation_revision + 1
                if status == "scheduled"
                else operation_revision
            )
        ),
        "updatedBy": CALLER_SUB,
        "updatedAt": "2026-09-07T10:30:00Z",
        "pendingVersion": Decimal(str(pending_version)),
        "pendingStatus": status,
        "activationToken": token,
        "cutoverAt": cutover_at,
        "scheduleName": name,
    }
    if status == "scheduled":
        state["scheduleArn"] = schedule_arn(name)
    return state


def make_event(*, token="a" * 64, outgoing_version=1, target_version=2):
    return {
        "PK": f"LOCATION#{LOCATION_ID}",
        "SK": f"LAYOUT#v{outgoing_version}",
        "activationStateSK": "LAYOUT#ACTIVATION",
        "activationToken": token,
        "targetSK": f"LAYOUT#v{target_version}",
    }


def event_for_state(state, *, outgoing_version=1, target_version=2):
    return make_event(
        token=state["activationToken"],
        outgoing_version=outgoing_version,
        target_version=target_version,
    )


def put_ready_cutover(snapshot_table, app):
    outgoing = outgoing_snapshot()
    target = target_snapshot()
    state = pending_state(app)
    for item in (outgoing, target, state):
        snapshot_table.put_item(Item=item)
    return outgoing, target, state


@pytest.fixture
def app_and_table(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv(
        "PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME",
        TABLE_NAME,
    )
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-north-1")

    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        resource = boto3.resource("dynamodb", region_name="eu-north-1")
        snapshot_table = resource.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        spec = importlib.util.spec_from_file_location(
            "expire_layout_version_app",
            APP_PATH,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "_utc_now", lambda: NOW)

        yield module, snapshot_table

        shared_dynamo._resource = None
        shared_dynamo._client = None


@pytest.mark.parametrize(
    "execution_time",
    [
        NOW,
        NOW + timedelta(hours=7),
    ],
    ids=["exact-cutover", "late-delivery"],
)
def test_due_cutover_atomically_activates_target(
    app_and_table,
    monkeypatch,
    execution_time,
):
    app, snapshot_table = app_and_table
    outgoing, target, state = put_ready_cutover(snapshot_table, app)
    original_outgoing = copy.deepcopy(outgoing)
    original_target = copy.deepcopy(target)
    monkeypatch.setattr(app, "_utc_now", lambda: execution_time)

    result = app.handler(event_for_state(state), None)

    assert result is None
    stored_outgoing = snapshot_table.get_item(
        Key={"PK": outgoing["PK"], "SK": outgoing["SK"]}
    )["Item"]
    stored_target = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    stored_state = snapshot_table.get_item(Key=state_key())["Item"]

    assert stored_outgoing["isCurrent"] is False
    assert stored_target["isCurrent"] is True
    assert sum(
        item["isCurrent"] for item in (stored_outgoing, stored_target)
    ) == 1
    for stored, original in (
        (stored_outgoing, original_outgoing),
        (stored_target, original_target),
    ):
        for field in (
            "version",
            "label",
            "effectiveFrom",
            "effectiveTo",
            "expiresAt",
            "elements",
            "validPositions",
            "createdBy",
            "createdAt",
        ):
            assert stored[field] == original[field]
        assert stored["updatedBy"] == CALLER_SUB
        assert stored["updatedAt"] == app._isoformat(execution_time)

    assert stored_state == {
        **state_key(),
        "recordType": "layoutActivationState",
        "currentVersion": Decimal("2"),
        "revision": Decimal("4"),
        "updatedBy": CALLER_SUB,
        "updatedAt": app._isoformat(execution_time),
    }


def test_matching_event_before_cutover_fails_without_writing(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    outgoing, target, state = put_ready_cutover(snapshot_table, app)
    originals = [copy.deepcopy(item) for item in (outgoing, target, state)]
    monkeypatch.setattr(
        app,
        "_utc_now",
        lambda: NOW - timedelta(microseconds=1),
    )

    with pytest.raises(app._CutoverConflict, match="has not arrived"):
        app.handler(event_for_state(state), None)

    for original in originals:
        stored = snapshot_table.get_item(
            Key={"PK": original["PK"], "SK": original["SK"]}
        )["Item"]
        assert stored == original


def _invalid_events():
    valid = make_event()
    return [
        pytest.param(None, id="not-a-map"),
        pytest.param({}, id="empty"),
        pytest.param(
            {key: value for key, value in valid.items() if key != "targetSK"},
            id="missing-field",
        ),
        pytest.param({**valid, "extra": "value"}, id="extra-field"),
        pytest.param({**valid, "PK": None}, id="pk-not-string"),
        pytest.param({**valid, "PK": "LOCATION#"}, id="empty-location"),
        pytest.param({**valid, "PK": "LOCATION# location"}, id="spaced-location"),
        pytest.param(
            {**valid, "PK": f"LOCATION#{'x' * 129}"},
            id="long-location",
        ),
        pytest.param({**valid, "PK": "OTHER#location"}, id="wrong-pk-prefix"),
        pytest.param({**valid, "SK": "LAYOUT#v01"}, id="leading-zero-old"),
        pytest.param({**valid, "SK": "LAYOUT#v0"}, id="zero-old"),
        pytest.param({**valid, "SK": "LAYOUT#v١"}, id="non-ascii-old"),
        pytest.param({**valid, "targetSK": "LAYOUT#v2 "}, id="spaced-target"),
        pytest.param({**valid, "targetSK": "LAYOUT#v1"}, id="same-version"),
        pytest.param(
            {**valid, "activationStateSK": "LAYOUT#STATE"},
            id="wrong-state-key",
        ),
        pytest.param({**valid, "activationToken": None}, id="token-not-string"),
        pytest.param({**valid, "activationToken": "a" * 63}, id="short-token"),
        pytest.param({**valid, "activationToken": "A" * 64}, id="uppercase-token"),
        pytest.param({**valid, "activationToken": "g" * 64}, id="non-hex-token"),
    ]


@pytest.mark.parametrize("event", _invalid_events())
def test_invalid_event_fails_before_dynamodb(
    app_and_table,
    monkeypatch,
    event,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not read DynamoDB"))
    monkeypatch.setattr(app, "table", table_factory)

    with pytest.raises(app._InvalidExpirationEvent):
        app.handler(event, None)

    table_factory.assert_not_called()


def test_ready_cutover_uses_exact_strongly_consistent_reads(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    outgoing, target, state = put_ready_cutover(snapshot_table, app)
    get_item = Mock(wraps=snapshot_table.get_item)
    snapshot_table.get_item = get_item
    monkeypatch.setattr(app, "table", lambda _: snapshot_table)

    app.handler(event_for_state(state), None)

    assert get_item.call_args_list == [
        call(Key=state_key(), ConsistentRead=True),
        call(
            Key={"PK": outgoing["PK"], "SK": outgoing["SK"]},
            ConsistentRead=True,
        ),
        call(
            Key={"PK": target["PK"], "SK": target["SK"]},
            ConsistentRead=True,
        ),
    ]


def test_missing_activation_state_fails(app_and_table):
    app, _ = app_and_table

    with pytest.raises(app._CutoverConflict, match="state is missing"):
        app.handler(make_event(), None)


def test_valid_stale_token_is_a_noop(app_and_table):
    app, snapshot_table = app_and_table
    outgoing, target, state = put_ready_cutover(snapshot_table, app)
    originals = [copy.deepcopy(item) for item in (outgoing, target, state)]
    stale_event = make_event(token="a" * 64)
    assert stale_event["activationToken"] != state["activationToken"]

    result = app.handler(stale_event, None)

    assert result is None
    for original in originals:
        stored = snapshot_table.get_item(
            Key={"PK": original["PK"], "SK": original["SK"]}
        )["Item"]
        assert stored == original


def test_matching_token_must_match_state_versions(app_and_table):
    app, snapshot_table = app_and_table
    _, _, state = put_ready_cutover(snapshot_table, app)

    with pytest.raises(app._CutoverConflict, match="does not match state"):
        app.handler(event_for_state(state, target_version=3), None)

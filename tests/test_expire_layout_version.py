import copy
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, call

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from moto import mock_aws

from shared import dynamo as shared_dynamo


APP_PATH = (
    Path(__file__).parents[1]
    / "functions"
    / "expire-layout-version"
    / "app.py"
)
ACTIVATE_APP_PATH = (
    Path(__file__).parents[1]
    / "functions"
    / "activate-layout-version"
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
    lifecycle = {
        "effectiveTo": CUTOVER_AT,
        "expiresAt": CUTOVER_AT,
    }
    lifecycle.update(overrides)
    return snapshot_item(1, is_current=True, **lifecycle)


def target_snapshot(**overrides):
    lifecycle = {
        "effectiveFrom": CUTOVER_AT,
        "effectiveTo": None,
        "expiresAt": None,
    }
    lifecycle.update(overrides)
    return snapshot_item(2, is_current=False, **lifecycle)


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


def put_scheduling_cutover(snapshot_table, app):
    outgoing = outgoing_snapshot(effectiveTo=None, expiresAt=None)
    target = target_snapshot(
        effectiveFrom="2026-05-01T01:00:00Z",
        effectiveTo="2026-06-01T01:00:00Z",
        expiresAt="2026-06-01T01:00:00Z",
    )
    state = pending_state(app, status="scheduling")
    for item in (outgoing, target, state):
        snapshot_table.put_item(Item=item)
    return outgoing, target, state


def client_error(code, *, cancellation_reasons=None):
    response = {
        "Error": {"Code": code, "Message": "sensitive dependency detail"},
        "ResponseMetadata": {"HTTPStatusCode": 400},
    }
    if cancellation_reasons is not None:
        response["CancellationReasons"] = cancellation_reasons
    return ClientError(response, "TransactWriteItems")


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


def test_missing_activation_state_is_an_orphan_noop(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    transaction_client = Mock(
        side_effect=AssertionError("must not write DynamoDB")
    )
    monkeypatch.setattr(app, "dynamodb_client", transaction_client)

    assert app.handler(make_event(), None) is None
    transaction_client.assert_not_called()


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


def test_due_scheduling_intent_recovers_orphaned_finalization(
    app_and_table,
):
    app, snapshot_table = app_and_table
    outgoing, target, state = put_scheduling_cutover(snapshot_table, app)

    assert app.handler(event_for_state(state), None) is None

    stored_outgoing = snapshot_table.get_item(
        Key={"PK": outgoing["PK"], "SK": outgoing["SK"]}
    )["Item"]
    stored_target = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    stored_state = snapshot_table.get_item(Key=state_key())["Item"]
    assert stored_outgoing["isCurrent"] is False
    assert stored_outgoing["effectiveFrom"] == outgoing["effectiveFrom"]
    assert stored_outgoing["effectiveTo"] == CUTOVER_AT
    assert stored_outgoing["expiresAt"] == CUTOVER_AT
    assert stored_target["isCurrent"] is True
    assert stored_target["effectiveFrom"] == CUTOVER_AT
    assert stored_target["effectiveTo"] is None
    assert stored_target["expiresAt"] is None
    assert stored_state == {
        **state_key(),
        "recordType": "layoutActivationState",
        "currentVersion": Decimal("2"),
        "revision": Decimal("3"),
        "updatedBy": CALLER_SUB,
        "updatedAt": app._isoformat(NOW),
    }


def test_scheduling_intent_before_cutover_fails_without_writing(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    outgoing, target, state = put_scheduling_cutover(snapshot_table, app)
    originals = [copy.deepcopy(item) for item in (outgoing, target, state)]
    monkeypatch.setattr(app, "_utc_now", lambda: NOW - timedelta(seconds=1))

    with pytest.raises(app._CutoverConflict, match="has not arrived"):
        app.handler(event_for_state(state), None)

    for original in originals:
        stored = snapshot_table.get_item(
            Key={"PK": original["PK"], "SK": original["SK"]}
        )["Item"]
        assert stored == original


def test_completed_cutover_retry_is_noop_after_state_read(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    outgoing, target, state = put_ready_cutover(snapshot_table, app)
    event = event_for_state(state)
    app.handler(event, None)
    completed = [
        snapshot_table.get_item(
            Key={"PK": item["PK"], "SK": item["SK"]}
        )["Item"]
        for item in (outgoing, target, state)
    ]
    real_get_item = snapshot_table.get_item
    get_item = Mock(wraps=real_get_item)
    snapshot_table.get_item = get_item
    transaction_client = Mock(
        side_effect=AssertionError("must not write DynamoDB")
    )
    monkeypatch.setattr(app, "table", lambda _: snapshot_table)
    monkeypatch.setattr(app, "dynamodb_client", transaction_client)

    assert app.handler(event, None) is None

    assert get_item.call_args_list == [
        call(Key=state_key(), ConsistentRead=True),
    ]
    transaction_client.assert_not_called()
    for original in completed:
        assert real_get_item(
            Key={"PK": original["PK"], "SK": original["SK"]}
        )["Item"] == original


def test_superseded_pending_token_is_noop_after_state_read(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    old_state = pending_state(app)
    newer_state = pending_state(
        app,
        current_version=2,
        pending_version=3,
        operation_revision=5,
    )
    snapshot_table.put_item(Item=newer_state)
    real_get_item = snapshot_table.get_item
    get_item = Mock(wraps=real_get_item)
    snapshot_table.get_item = get_item
    transaction_client = Mock(
        side_effect=AssertionError("must not write DynamoDB")
    )
    monkeypatch.setattr(app, "table", lambda _: snapshot_table)
    monkeypatch.setattr(app, "dynamodb_client", transaction_client)

    assert app.handler(event_for_state(old_state), None) is None

    assert get_item.call_args_list == [
        call(Key=state_key(), ConsistentRead=True),
    ]
    transaction_client.assert_not_called()


def test_snapshot_timestamps_accept_task11_compatible_utc_forms(
    app_and_table,
):
    app, snapshot_table = app_and_table
    outgoing = outgoing_snapshot(
        effectiveFrom="2026-09-01T01:00:00+00:00",
        createdAt="2026-09-01T00:00:00.1Z",
    )
    target = target_snapshot(updatedAt="2026-09-07T10:30:00+00:00")
    state = pending_state(app)
    for item in (outgoing, target, state):
        snapshot_table.put_item(Item=item)

    assert app.handler(event_for_state(state), None) is None

    stored_outgoing = snapshot_table.get_item(
        Key={"PK": outgoing["PK"], "SK": outgoing["SK"]}
    )["Item"]
    assert stored_outgoing["effectiveFrom"] == outgoing["effectiveFrom"]
    assert stored_outgoing["createdAt"] == outgoing["createdAt"]


@pytest.mark.parametrize(
    "invalid_now",
    [None, "2026-10-05T01:00:00Z", datetime(2026, 10, 5, 1, 0)],
    ids=["none", "string", "naive-datetime"],
)
def test_worker_clock_must_be_an_aware_datetime(
    app_and_table,
    monkeypatch,
    invalid_now,
):
    app, snapshot_table = app_and_table
    _, _, state = put_ready_cutover(snapshot_table, app)
    transaction_client = Mock(
        side_effect=AssertionError("must not write DynamoDB")
    )
    monkeypatch.setattr(app, "_utc_now", lambda: invalid_now)
    monkeypatch.setattr(app, "dynamodb_client", transaction_client)

    with pytest.raises(app._CutoverConflict, match="clock is invalid"):
        app.handler(event_for_state(state), None)

    transaction_client.assert_not_called()


@pytest.mark.parametrize(
    "case",
    [
        "record-type",
        "current-version-bool",
        "revision-zero",
        "revision-fraction",
        "revision-overflow",
        "blank-actor",
        "bad-updated-time",
        "same-pending-version",
        "partial-pending-fields",
        "bad-status",
        "bad-token",
        "wrong-cutover-hour",
        "non-utc-cutover",
        "bad-schedule-name",
        "missing-schedule-arn",
        "bad-schedule-arn",
        "scheduling-with-arn",
    ],
)
def test_invalid_matching_activation_state_fails_without_transaction(
    app_and_table,
    monkeypatch,
    case,
):
    app, snapshot_table = app_and_table
    state = pending_state(app)
    event = event_for_state(state)
    if case == "record-type":
        state["recordType"] = "other"
    elif case == "current-version-bool":
        state["currentVersion"] = True
    elif case == "revision-zero":
        state["revision"] = Decimal("0")
    elif case == "revision-fraction":
        state["revision"] = Decimal("3.5")
    elif case == "revision-overflow":
        state["revision"] = Decimal("9" * 38)
    elif case == "blank-actor":
        state["updatedBy"] = " "
    elif case == "bad-updated-time":
        state["updatedAt"] = "not-a-time"
    elif case == "same-pending-version":
        state["pendingVersion"] = state["currentVersion"]
    elif case == "partial-pending-fields":
        del state["scheduleName"]
    elif case == "bad-status":
        state["pendingStatus"] = "running"
    elif case == "bad-token":
        state["activationToken"] = "g" * 64
    elif case == "wrong-cutover-hour":
        state = pending_state(app, cutover_at="2026-10-05T02:00:00Z")
        event = event_for_state(state)
    elif case == "non-utc-cutover":
        state = pending_state(app, cutover_at="2026-10-05T01:00:00+02:00")
        event = event_for_state(state)
    elif case == "bad-schedule-name":
        state["scheduleName"] = "expire-layout-version-wrong"
    elif case == "missing-schedule-arn":
        del state["scheduleArn"]
    elif case == "bad-schedule-arn":
        state["scheduleArn"] = "arn:aws:scheduler:wrong"
    elif case == "scheduling-with-arn":
        state = pending_state(app, status="scheduling")
        event = event_for_state(state)
        state["scheduleArn"] = schedule_arn(state["scheduleName"])

    snapshot_table.put_item(Item=state)
    transaction_client = Mock(
        side_effect=AssertionError("must not write DynamoDB")
    )
    monkeypatch.setattr(app, "dynamodb_client", transaction_client)

    with pytest.raises(app._CutoverConflict):
        app.handler(event, None)

    transaction_client.assert_not_called()


@pytest.mark.parametrize("missing", ["outgoing", "target"])
def test_matching_transition_with_missing_snapshot_fails_without_transaction(
    app_and_table,
    monkeypatch,
    missing,
):
    app, snapshot_table = app_and_table
    outgoing, target, state = put_ready_cutover(snapshot_table, app)
    missing_item = outgoing if missing == "outgoing" else target
    snapshot_table.delete_item(
        Key={"PK": missing_item["PK"], "SK": missing_item["SK"]}
    )
    transaction_client = Mock(
        side_effect=AssertionError("must not write DynamoDB")
    )
    monkeypatch.setattr(app, "dynamodb_client", transaction_client)

    with pytest.raises(app._CutoverConflict, match="snapshot is missing"):
        app.handler(event_for_state(state), None)

    transaction_client.assert_not_called()


@pytest.mark.parametrize(
    ("record", "field", "value"),
    [
        ("outgoing", "version", Decimal("2")),
        ("outgoing", "isCurrent", "true"),
        ("outgoing", "effectiveFrom", "not-a-time"),
        ("outgoing", "effectiveTo", None),
        ("outgoing", "expiresAt", None),
        ("outgoing", "elements", ["bad"]),
        ("outgoing", "validPositions", [{}]),
        ("outgoing", "updatedBy", " "),
        ("target", "isCurrent", True),
        ("target", "effectiveFrom", "2026-10-06T01:00:00Z"),
        ("target", "effectiveTo", "2026-11-05T01:00:00Z"),
        ("target", "expiresAt", "2026-11-05T01:00:00Z"),
        ("target", "createdAt", "2026-09-01T00:00:00+02:00"),
    ],
)
def test_snapshot_or_lifecycle_corruption_fails_without_transaction(
    app_and_table,
    monkeypatch,
    record,
    field,
    value,
):
    app, snapshot_table = app_and_table
    outgoing = outgoing_snapshot()
    target = target_snapshot()
    state = pending_state(app)
    selected = outgoing if record == "outgoing" else target
    selected[field] = value
    for item in (outgoing, target, state):
        snapshot_table.put_item(Item=item)
    transaction_client = Mock(
        side_effect=AssertionError("must not write DynamoDB")
    )
    monkeypatch.setattr(app, "dynamodb_client", transaction_client)

    with pytest.raises(app._CutoverConflict):
        app.handler(event_for_state(state), None)

    transaction_client.assert_not_called()


@pytest.mark.parametrize(
    "response",
    [None, [], {"Item": []}],
    ids=["none", "list", "non-map-item"],
)
def test_malformed_get_item_response_fails(
    app_and_table,
    monkeypatch,
    response,
):
    app, snapshot_table = app_and_table
    snapshot_table.get_item = Mock(return_value=response)
    monkeypatch.setattr(app, "table", lambda _: snapshot_table)

    with pytest.raises(app._CutoverConflict, match="invalid response"):
        app.handler(make_event(), None)


def test_dynamodb_read_failure_propagates(app_and_table, monkeypatch):
    app, snapshot_table = app_and_table
    error = EndpointConnectionError(endpoint_url="https://dynamodb.invalid")
    snapshot_table.get_item = Mock(side_effect=error)
    monkeypatch.setattr(app, "table", lambda _: snapshot_table)

    with pytest.raises(EndpointConnectionError) as raised:
        app.handler(make_event(), None)

    assert raised.value is error


@pytest.mark.parametrize("phase", ["scheduling", "scheduled"])
def test_transaction_conditions_bind_the_complete_transition(
    app_and_table,
    monkeypatch,
    phase,
):
    app, snapshot_table = app_and_table
    if phase == "scheduled":
        _, _, state = put_ready_cutover(snapshot_table, app)
    else:
        _, _, state = put_scheduling_cutover(snapshot_table, app)
    transaction_client = Mock()
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)

    assert app.handler(event_for_state(state), None) is None

    request = transaction_client.transact_write_items.call_args.kwargs
    assert set(request) == {"TransactItems"}
    assert len(request["TransactItems"]) == 3
    assert "ClientRequestToken" not in request
    outgoing_update, target_update, state_update = [
        operation["Update"] for operation in request["TransactItems"]
    ]
    for update in (outgoing_update, target_update):
        assert update["TableName"] == TABLE_NAME
        assert set(update["ExpressionAttributeNames"].values()) == {
            "version",
            "isCurrent",
            "effectiveFrom",
            "effectiveTo",
            "expiresAt",
            "updatedBy",
            "updatedAt",
        }
        for field in (
            "#version",
            "#isCurrent",
            "#effectiveFrom",
            "#effectiveTo",
            "#expiresAt",
        ):
            assert field in update["ConditionExpression"]
        for field in (
            "#isCurrent",
            "#effectiveFrom",
            "#effectiveTo",
            "#expiresAt",
            "#updatedBy",
            "#updatedAt",
        ):
            assert field in update["UpdateExpression"]

    assert state_update["TableName"] == TABLE_NAME
    assert set(state_update["ExpressionAttributeNames"].values()) == {
        "recordType",
        "currentVersion",
        "revision",
        "pendingVersion",
        "pendingStatus",
        "activationToken",
        "cutoverAt",
        "scheduleName",
        "scheduleArn",
        "updatedBy",
        "updatedAt",
    }
    for field in (
        "#recordType",
        "#currentVersion",
        "#revision",
        "#pendingVersion",
        "#pendingStatus",
        "#activationToken",
        "#cutoverAt",
        "#scheduleName",
        "#scheduleArn",
        "#updatedBy",
        "#updatedAt",
    ):
        assert field in state_update["ConditionExpression"]
    remove_clause = state_update["UpdateExpression"].split("REMOVE ", 1)[1]
    assert {value.strip() for value in remove_clause.split(",")} == {
        "#pendingVersion",
        "#pendingStatus",
        "#activationToken",
        "#cutoverAt",
        "#scheduleName",
        "#scheduleArn",
    }
    if phase == "scheduled":
        assert "#scheduleArn = :scheduleArn" in state_update[
            "ConditionExpression"
        ]
        assert state_update["ExpressionAttributeValues"][":scheduleArn"] == {
            "S": state["scheduleArn"]
        }
    else:
        assert "attribute_not_exists(#scheduleArn)" in state_update[
            "ConditionExpression"
        ]
        assert ":scheduleArn" not in state_update["ExpressionAttributeValues"]


@pytest.mark.parametrize(
    "error_code",
    ["ConditionalCheckFailedException", "TransactionCanceledException"],
)
def test_uncommitted_conditional_failure_with_same_token_is_reraised(
    app_and_table,
    monkeypatch,
    error_code,
):
    app, snapshot_table = app_and_table
    outgoing, target, state = put_ready_cutover(snapshot_table, app)
    originals = [copy.deepcopy(item) for item in (outgoing, target, state)]
    error = client_error(
        error_code,
        cancellation_reasons=(
            [{"Code": "ConditionalCheckFailed"}]
            if error_code == "TransactionCanceledException"
            else None
        ),
    )
    transaction_client = Mock()
    transaction_client.transact_write_items.side_effect = error
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)

    with pytest.raises(ClientError) as raised:
        app.handler(event_for_state(state), None)

    assert raised.value is error
    for original in originals:
        stored = snapshot_table.get_item(
            Key={"PK": original["PK"], "SK": original["SK"]}
        )["Item"]
        assert stored == original


def test_uncommitted_endpoint_timeout_with_same_token_is_reraised(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    _, _, state = put_ready_cutover(snapshot_table, app)
    error = EndpointConnectionError(endpoint_url="https://dynamodb.invalid")
    transaction_client = Mock()
    transaction_client.transact_write_items.side_effect = error
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)

    with pytest.raises(EndpointConnectionError) as raised:
        app.handler(event_for_state(state), None)

    assert raised.value is error


@pytest.mark.parametrize(
    "error",
    [
        client_error(
            "TransactionCanceledException",
            cancellation_reasons=[{"Code": "ConditionalCheckFailed"}],
        ),
        EndpointConnectionError(endpoint_url="https://dynamodb.invalid"),
    ],
    ids=["conditional-race", "response-lost"],
)
def test_committed_transaction_error_reconciles_as_success(
    app_and_table,
    monkeypatch,
    error,
):
    app, snapshot_table = app_and_table
    outgoing, target, state = put_ready_cutover(snapshot_table, app)
    real_client = shared_dynamo.client()
    transaction_client = Mock()

    def commit_then_raise(**request):
        real_client.transact_write_items(**request)
        raise error

    transaction_client.transact_write_items.side_effect = commit_then_raise
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)

    assert app.handler(event_for_state(state), None) is None

    stored_outgoing = snapshot_table.get_item(
        Key={"PK": outgoing["PK"], "SK": outgoing["SK"]}
    )["Item"]
    stored_target = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    stored_state = snapshot_table.get_item(Key=state_key())["Item"]
    assert stored_outgoing["isCurrent"] is False
    assert stored_target["isCurrent"] is True
    assert stored_state["currentVersion"] == Decimal("2")
    assert not set(stored_state).intersection(app._PENDING_FIELDS)


def test_transaction_race_with_newer_token_is_stale_noop(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    outgoing, target, state = put_ready_cutover(snapshot_table, app)
    newer_state = pending_state(
        app,
        current_version=1,
        pending_version=3,
        operation_revision=5,
    )
    error = client_error(
        "TransactionCanceledException",
        cancellation_reasons=[{"Code": "ConditionalCheckFailed"}],
    )
    transaction_client = Mock()

    def supersede_then_raise(**_):
        snapshot_table.put_item(Item=newer_state)
        raise error

    transaction_client.transact_write_items.side_effect = supersede_then_raise
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)

    assert app.handler(event_for_state(state), None) is None

    stored_outgoing = snapshot_table.get_item(
        Key={"PK": outgoing["PK"], "SK": outgoing["SK"]}
    )["Item"]
    stored_target = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    assert stored_outgoing == outgoing
    assert stored_target == target
    assert snapshot_table.get_item(Key=state_key())["Item"] == newer_state


def test_scheduling_to_scheduled_race_reraises_for_retry(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    _, _, state = put_scheduling_cutover(snapshot_table, app)
    event = event_for_state(state)
    staged_state = pending_state(app, status="scheduled")
    error = client_error(
        "TransactionCanceledException",
        cancellation_reasons=[{"Code": "ConditionalCheckFailed"}],
    )
    transaction_client = Mock()

    def finalize_then_raise(**_):
        snapshot_table.put_item(Item=outgoing_snapshot())
        snapshot_table.put_item(Item=target_snapshot())
        snapshot_table.put_item(Item=staged_state)
        raise error

    transaction_client.transact_write_items.side_effect = finalize_then_raise
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)

    with pytest.raises(ClientError) as raised:
        app.handler(event, None)

    assert raised.value is error


def test_snapshot_read_race_completed_by_other_worker_is_noop(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    outgoing, target, state = put_ready_cutover(snapshot_table, app)
    event = event_for_state(state)
    details = app._event_details(event)
    state_details = app._validate_state(state, details)
    lifecycles = app._validate_pending_lifecycle(
        outgoing,
        target,
        state_details,
        details,
    )
    real_read_item = app._read_item
    read_count = 0

    def race_on_outgoing_read(table_value, key):
        nonlocal read_count
        read_count += 1
        if read_count == 2:
            app._complete_cutover(
                outgoing,
                target,
                state_details,
                details,
                NOW,
                *lifecycles,
            )
        return real_read_item(table_value, key)

    monkeypatch.setattr(app, "_read_item", race_on_outgoing_read)

    assert app.handler(event, None) is None
    assert snapshot_table.get_item(Key=state_key())["Item"][
        "currentVersion"
    ] == Decimal("2")


def test_reconciliation_read_failure_never_acknowledges_transaction_error(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    _, _, state = put_ready_cutover(snapshot_table, app)
    original_error = client_error("AccessDeniedException")
    transaction_client = Mock()
    transaction_client.transact_write_items.side_effect = original_error
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)
    real_get_item = snapshot_table.get_item
    call_count = 0

    def fail_reconciliation(**request):
        nonlocal call_count
        call_count += 1
        if call_count == 4:
            raise EndpointConnectionError(
                endpoint_url="https://dynamodb.invalid"
            )
        return real_get_item(**request)

    snapshot_table.get_item = Mock(side_effect=fail_reconciliation)
    monkeypatch.setattr(app, "table", lambda _: snapshot_table)

    with pytest.raises(ClientError) as raised:
        app.handler(event_for_state(state), None)

    assert raised.value is original_error


def test_task11_schedule_input_completes_task12_cutover(
    app_and_table,
    monkeypatch,
):
    worker, snapshot_table = app_and_table
    monkeypatch.setenv(
        "SCHEDULER_INVOKE_ROLE_ARN",
        "arn:aws:iam::123456789012:role/test-scheduler-role",
    )
    monkeypatch.setenv(
        "EXPIRE_LAYOUT_VERSION_FUNCTION_ARN",
        "arn:aws:lambda:eu-north-1:123456789012:function:test-expire",
    )
    spec = importlib.util.spec_from_file_location(
        "activate_layout_version_contract_app",
        ACTIVATE_APP_PATH,
    )
    activate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(activate)
    activation_time = datetime(
        2026,
        9,
        7,
        10,
        30,
        tzinfo=timezone.utc,
    )
    monkeypatch.setattr(activate, "_utc_now", lambda: activation_time)
    scheduler = Mock()
    scheduler.create_schedule.side_effect = lambda **request: {
        "ScheduleArn": schedule_arn(request["Name"])
    }
    monkeypatch.setattr(activate, "_get_scheduler_client", lambda: scheduler)

    current = snapshot_item(
        1,
        is_current=True,
        effectiveFrom="2026-08-01T01:00:00Z",
        effectiveTo=None,
        expiresAt=None,
    )
    replacement = snapshot_item(
        2,
        is_current=False,
        effectiveFrom=None,
        effectiveTo=None,
        expiresAt="2026-10-05T10:30:00Z",
    )
    steady_state = {
        **state_key(),
        "recordType": "layoutActivationState",
        "currentVersion": Decimal("1"),
        "revision": Decimal("1"),
        "updatedBy": CALLER_SUB,
        "updatedAt": "2026-09-07T09:00:00Z",
    }
    for item in (current, replacement, steady_state):
        snapshot_table.put_item(Item=item)

    response = activate.handler(
        {
            "requestContext": {
                "http": {"method": "POST"},
                "authorizer": {
                    "jwt": {
                        "claims": {
                            "sub": CALLER_SUB,
                            "cognito:groups": '["owner_user"]',
                        }
                    }
                },
            },
            "pathParameters": {
                "locationId": LOCATION_ID,
                "versionId": "2",
            },
        },
        None,
    )

    assert response["statusCode"] == 202
    schedule_request = scheduler.create_schedule.call_args.kwargs
    scheduled_event = json.loads(schedule_request["Target"]["Input"])
    assert set(scheduled_event) == worker._EVENT_FIELDS
    assert worker.handler(scheduled_event, None) is None

    stored_current = snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"]
    stored_replacement = snapshot_table.get_item(
        Key={"PK": replacement["PK"], "SK": replacement["SK"]}
    )["Item"]
    stored_state = snapshot_table.get_item(Key=state_key())["Item"]
    assert stored_current["isCurrent"] is False
    assert stored_replacement["isCurrent"] is True
    assert stored_state["currentVersion"] == Decimal("2")
    assert stored_state["revision"] == Decimal("4")
    assert not set(stored_state).intersection(worker._PENDING_FIELDS)

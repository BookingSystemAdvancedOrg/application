import base64
import importlib.util
import json
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
    / "manage-user"
    / "app.py"
)
TABLE_NAME = "test-user"
USER_POOL_ID = "eu-north-1_test-pool"
CALLER_SUB = "caller-sub"
TARGET_SUB = "target-sub"
CREATED_AT = "2026-08-21T10:00:00Z"

GROUP_TO_ROLE = {
    "staff_user": "staff",
    "owner_user": "owner_user",
    "super_user": "super_admin",
}
ROLE_TO_GROUP = {role: group for group, role in GROUP_TO_ROLE.items()}

COGNITO_METHODS = [
    "admin_add_user_to_group",
    "admin_create_user",
    "admin_delete_user",
    "admin_disable_user",
    "admin_enable_user",
    "admin_get_user",
    "admin_list_groups_for_user",
    "admin_remove_user_from_group",
    "admin_update_user_attributes",
]

NO_BODY = object()


def client_error(code, operation, message="sensitive AWS detail"):
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        operation,
    )


def make_event(
    *,
    method="POST",
    proxy="invite",
    body=NO_BODY,
    groups='["owner_user"]',
    sub=CALLER_SUB,
    base64_encoded=False,
):
    event = {
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
        "pathParameters": {"proxy": proxy},
    }

    if body is not NO_BODY:
        raw_body = body if isinstance(body, str) else json.dumps(body)
        if base64_encoded:
            raw_body = base64.b64encode(raw_body.encode("utf-8")).decode()
        event["body"] = raw_body
        event["isBase64Encoded"] = base64_encoded

    return event


def invite_body(group="staff_user", location_id="location-id"):
    body = {
        "name": "Test User",
        "email": "test.user@example.com",
        "phone": "+46701234567",
        "group": group,
    }
    if location_id is not None:
        body["locationId"] = location_id
    return body


def user_item(
    *,
    sub=TARGET_SUB,
    role="staff",
    location_id="location-id",
    status="active",
    name="Test User",
    email="test.user@example.com",
    phone="+46701234567",
):
    return {
        "PK": f"USER#{sub}",
        "SK": "PROFILE",
        "cognitoSub": sub,
        "role": role,
        "locationId": location_id,
        "name": name,
        "email": email,
        "phone": phone,
        "status": status,
        "createdBy": CALLER_SUB,
        "createdAt": CREATED_AT,
    }


def set_create_response(cognito, sub=TARGET_SUB):
    cognito.admin_create_user.return_value = {
        "User": {
            "Username": sub,
            "Attributes": [
                {"Name": "sub", "Value": sub},
                {"Name": "email", "Value": "test.user@example.com"},
                {"Name": "name", "Value": "Test User"},
                {"Name": "phone_number", "Value": "+46701234567"},
            ],
        }
    }


def put_user(user_table, item=None):
    user_table.put_item(Item=item or user_item())


def get_user(user_table, sub=TARGET_SUB):
    return user_table.get_item(
        Key={"PK": f"USER#{sub}", "SK": "PROFILE"},
        ConsistentRead=True,
    ).get("Item")


def response_body(response):
    raw_body = response.get("body", "")
    return None if raw_body == "" else json.loads(raw_body)


def assert_response(response, status_code, body=NO_BODY):
    assert response["statusCode"] == status_code
    assert response["headers"]["Cache-Control"] == "no-store"
    if body is not NO_BODY:
        assert response_body(response) == body


@pytest.fixture
def app_state(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("USER_TABLE_NAME", TABLE_NAME)
    monkeypatch.setenv("COGNITO_USER_POOL_ID", USER_POOL_ID)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-north-1")

    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None

        resource = boto3.resource("dynamodb", region_name="eu-north-1")
        user_table = resource.create_table(
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
            "manage_user_app",
            APP_PATH,
        )
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)

        cognito = Mock(spec_set=COGNITO_METHODS)
        set_create_response(cognito)
        cognito.admin_list_groups_for_user.return_value = {
            "Groups": [{"GroupName": "staff_user"}],
        }
        cognito.admin_get_user.return_value = {
            "Enabled": True,
            "UserAttributes": [
                {"Name": "name", "Value": "Test User"},
                {
                    "Name": "email",
                    "Value": "test.user@example.com",
                },
                {"Name": "email_verified", "Value": "true"},
                {
                    "Name": "phone_number",
                    "Value": "+46701234567",
                },
                {
                    "Name": "phone_number_verified",
                    "Value": "true",
                },
            ],
        }

        monkeypatch.setattr(
            app,
            "_get_cognito_client",
            lambda: cognito,
            raising=False,
        )
        monkeypatch.setattr(
            app,
            "_cognito_client",
            cognito,
            raising=False,
        )
        monkeypatch.setattr(
            app,
            "_utc_now",
            lambda: CREATED_AT,
            raising=False,
        )

        yield app, user_table, cognito

        shared_dynamo._resource = None
        shared_dynamo._client = None


def test_missing_claims_returns_401_before_dispatch_or_aws(app_state):
    app, user_table, cognito = app_state
    event = make_event(proxy="unknown")
    del event["requestContext"]["authorizer"]

    response = app.handler(event, None)

    assert_response(
        response,
        401,
        {"error": "no JWT claims on this request"},
    )
    assert user_table.scan()["Items"] == []
    assert cognito.mock_calls == []


@pytest.mark.parametrize("claims", [None, [], "invalid"])
def test_malformed_claims_return_401_without_aws(app_state, claims):
    app, user_table, cognito = app_state
    event = make_event()
    event["requestContext"]["authorizer"]["jwt"]["claims"] = claims

    response = app.handler(event, None)

    assert response["statusCode"] == 401
    assert user_table.scan()["Items"] == []
    assert cognito.mock_calls == []


def test_missing_subject_returns_401_without_aws(app_state):
    app, user_table, cognito = app_state

    response = app.handler(make_event(sub=""), None)

    assert_response(response, 401, {"error": "JWT is missing a subject"})
    assert user_table.scan()["Items"] == []
    assert cognito.mock_calls == []


@pytest.mark.parametrize(
    "groups",
    ['["staff_user"]', '["unknown"]', "", None],
)
def test_wrong_caller_group_returns_403_before_body_or_aws(
    app_state,
    groups,
):
    app, user_table, cognito = app_state

    response = app.handler(
        make_event(groups=groups, body="not JSON"),
        None,
    )

    assert_response(response, 403, {"error": "forbidden"})
    assert user_table.scan()["Items"] == []
    assert cognito.mock_calls == []


def test_unknown_route_returns_404_without_aws(app_state):
    app, user_table, cognito = app_state

    response = app.handler(make_event(proxy="unknown/path"), None)

    assert_response(response, 404, {"error": "not found"})
    assert user_table.scan()["Items"] == []
    assert cognito.mock_calls == []


@pytest.mark.parametrize(
    ("method", "proxy", "allowed"),
    [
        ("GET", "invite", "POST"),
        ("PATCH", TARGET_SUB, "PUT, DELETE"),
        ("PUT", f"{TARGET_SUB}/deactivate", "POST"),
        ("PUT", f"{TARGET_SUB}/reactivate", "POST"),
        ("POST", f"{TARGET_SUB}/group", "PUT"),
    ],
)
def test_known_route_wrong_method_returns_405(
    app_state,
    method,
    proxy,
    allowed,
):
    app, user_table, cognito = app_state

    response = app.handler(make_event(method=method, proxy=proxy), None)

    assert_response(response, 405, {"error": "method not allowed"})
    assert response["headers"]["Allow"] == allowed
    assert user_table.scan()["Items"] == []
    assert cognito.mock_calls == []


@pytest.mark.parametrize(
    "body",
    [NO_BODY, "", "{", "[]", "null"],
)
def test_body_routes_reject_missing_or_invalid_json(app_state, body):
    app, user_table, cognito = app_state
    event = make_event(body=body)

    response = app.handler(event, None)

    assert response["statusCode"] == 400
    assert response["headers"]["Cache-Control"] == "no-store"
    assert user_table.scan()["Items"] == []
    assert cognito.mock_calls == []


def test_invalid_base64_body_returns_400_without_aws(app_state):
    app, user_table, cognito = app_state
    event = make_event(body="not-base64")
    event["isBase64Encoded"] = True

    response = app.handler(event, None)

    assert response["statusCode"] == 400
    assert user_table.scan()["Items"] == []
    assert cognito.mock_calls == []


@pytest.mark.parametrize("field", ["PK", "SK", "cognitoSub", "role", "status"])
def test_profile_rejects_server_controlled_fields(app_state, field):
    app, user_table, cognito = app_state
    put_user(user_table)

    response = app.handler(
        make_event(
            method="PUT",
            proxy=TARGET_SUB,
            body={field: "caller-controlled"},
        ),
        None,
    )

    assert response["statusCode"] == 400
    assert get_user(user_table) == user_item()
    assert cognito.mock_calls == []


@pytest.mark.parametrize(
    ("group", "location_id", "role", "stored_location"),
    [
        ("staff_user", "location-id", "staff", "location-id"),
        ("owner_user", None, "owner_user", ""),
        ("super_user", None, "super_admin", ""),
    ],
)
def test_super_user_can_invite_each_group_and_persist_mapping(
    app_state,
    group,
    location_id,
    role,
    stored_location,
):
    app, user_table, cognito = app_state
    body = invite_body(group=group, location_id=location_id)

    response = app.handler(
        make_event(
            body=body,
            groups='["super_user"]',
            base64_encoded=True,
        ),
        None,
    )
    result = response_body(response)

    assert_response(response, 201)
    assert response["headers"]["Location"] == f"/users/{TARGET_SUB}"
    assert "PK" not in result and "SK" not in result
    assert result == {
        "cognitoSub": TARGET_SUB,
        "role": role,
        "locationId": stored_location,
        "name": body["name"],
        "email": body["email"],
        "phone": body["phone"],
        "status": "active",
        "createdBy": CALLER_SUB,
        "createdAt": CREATED_AT,
    }
    assert get_user(user_table) == {
        "PK": f"USER#{TARGET_SUB}",
        "SK": "PROFILE",
        **result,
    }

    create_kwargs = cognito.admin_create_user.call_args.kwargs
    assert create_kwargs["UserPoolId"] == USER_POOL_ID
    assert create_kwargs["Username"] == body["email"]
    attributes = {
        attribute["Name"]: attribute["Value"]
        for attribute in create_kwargs["UserAttributes"]
    }
    assert attributes == {
        "email": body["email"],
        "name": body["name"],
        "phone_number": body["phone"],
    }
    cognito.admin_add_user_to_group.assert_called_once_with(
        UserPoolId=USER_POOL_ID,
        Username=TARGET_SUB,
        GroupName=group,
    )
    cognito.admin_delete_user.assert_not_called()


def test_owner_can_invite_staff(app_state):
    app, user_table, cognito = app_state

    response = app.handler(make_event(body=invite_body()), None)

    assert_response(response, 201)
    assert get_user(user_table)["role"] == "staff"
    cognito.admin_add_user_to_group.assert_called_once()


@pytest.mark.parametrize("group", ["owner_user", "super_user"])
def test_owner_cannot_invite_privileged_user(app_state, group):
    app, user_table, cognito = app_state

    response = app.handler(
        make_event(body=invite_body(group=group, location_id=None)),
        None,
    )

    assert_response(response, 403, {"error": "forbidden"})
    assert user_table.scan()["Items"] == []
    assert cognito.mock_calls == []


@pytest.mark.parametrize(
    "body",
    [
        invite_body(location_id=None),
        invite_body(location_id=""),
        invite_body(group="owner_user", location_id="location-id"),
        invite_body(group="unknown", location_id=None),
    ],
)
def test_invite_validates_group_location_invariants(app_state, body):
    app, user_table, cognito = app_state

    response = app.handler(
        make_event(body=body, groups='["super_user"]'),
        None,
    )

    assert response["statusCode"] == 400
    assert user_table.scan()["Items"] == []
    assert cognito.mock_calls == []


def test_invite_uses_conditional_put(app_state, monkeypatch):
    app, user_table, _ = app_state
    table_spy = Mock(wraps=user_table)
    monkeypatch.setattr(app, "table", lambda _: table_spy)

    response = app.handler(make_event(body=invite_body()), None)

    assert response["statusCode"] == 201
    assert table_spy.put_item.call_args.kwargs["ConditionExpression"] == (
        "attribute_not_exists(PK) AND attribute_not_exists(SK)"
    )


def test_duplicate_cognito_username_returns_409_without_write(app_state):
    app, user_table, cognito = app_state
    cognito.admin_create_user.side_effect = client_error(
        "UsernameExistsException",
        "AdminCreateUser",
    )

    response = app.handler(make_event(body=invite_body()), None)

    assert response["statusCode"] == 409
    assert user_table.scan()["Items"] == []
    cognito.admin_add_user_to_group.assert_not_called()
    cognito.admin_delete_user.assert_not_called()
    assert "sensitive AWS detail" not in response["body"]


def test_ambiguous_create_transport_failure_does_not_delete_user(app_state):
    app, user_table, cognito = app_state
    cognito.admin_create_user.side_effect = EndpointConnectionError(
        endpoint_url="https://cognito-idp.eu-north-1.amazonaws.com",
    )

    response = app.handler(make_event(body=invite_body()), None)

    assert_response(response, 503, {"error": "user service unavailable"})
    assert user_table.scan()["Items"] == []
    cognito.admin_add_user_to_group.assert_not_called()
    cognito.admin_delete_user.assert_not_called()


def test_group_add_failure_deletes_new_cognito_user(app_state):
    app, user_table, cognito = app_state
    cognito.admin_add_user_to_group.side_effect = client_error(
        "InternalErrorException",
        "AdminAddUserToGroup",
    )

    response = app.handler(make_event(body=invite_body()), None)

    assert response["statusCode"] == 503
    assert user_table.scan()["Items"] == []
    assert cognito.mock_calls[:3] == [
        call.admin_create_user(**cognito.admin_create_user.call_args.kwargs),
        call.admin_add_user_to_group(
            UserPoolId=USER_POOL_ID,
            Username=TARGET_SUB,
            GroupName="staff_user",
        ),
        call.admin_delete_user(
            UserPoolId=USER_POOL_ID,
            Username=TARGET_SUB,
        ),
    ]
    assert "sensitive AWS detail" not in response["body"]


def test_conditional_put_collision_preserves_record_and_compensates(
    app_state,
):
    app, user_table, cognito = app_state
    existing = user_item(name="Existing")
    put_user(user_table, existing)

    response = app.handler(make_event(body=invite_body()), None)

    assert response["statusCode"] == 409
    assert get_user(user_table) == existing
    cognito.admin_delete_user.assert_called_once_with(
        UserPoolId=USER_POOL_ID,
        Username=TARGET_SUB,
    )


def test_dynamodb_invite_failure_deletes_new_cognito_user(
    app_state,
    monkeypatch,
):
    app, user_table, cognito = app_state
    failing_table = Mock(wraps=user_table)
    failing_table.put_item.side_effect = client_error(
        "ValidationException",
        "PutItem",
    )
    monkeypatch.setattr(app, "table", lambda _: failing_table)

    response = app.handler(make_event(body=invite_body()), None)

    assert_response(response, 503, {"error": "user service unavailable"})
    assert user_table.scan()["Items"] == []
    cognito.admin_delete_user.assert_called_once_with(
        UserPoolId=USER_POOL_ID,
        Username=TARGET_SUB,
    )


def test_profile_put_updates_only_supplied_fields(app_state):
    app, user_table, cognito = app_state
    put_user(user_table)

    response = app.handler(
        make_event(
            method="PUT",
            proxy=TARGET_SUB,
            body={"name": "Changed Name"},
        ),
        None,
    )
    result = response_body(response)

    assert_response(response, 200)
    assert result["name"] == "Changed Name"
    assert result["email"] == "test.user@example.com"
    assert result["phone"] == "+46701234567"
    assert "PK" not in result and "SK" not in result
    assert get_user(user_table)["name"] == "Changed Name"
    cognito.admin_update_user_attributes.assert_called_once_with(
        UserPoolId=USER_POOL_ID,
        Username=TARGET_SUB,
        UserAttributes=[{"Name": "name", "Value": "Changed Name"}],
    )


def test_location_only_profile_update_does_not_call_cognito(app_state):
    app, user_table, cognito = app_state
    put_user(user_table)

    response = app.handler(
        make_event(
            method="PUT",
            proxy=TARGET_SUB,
            body={"locationId": "new-location"},
        ),
        None,
    )

    assert_response(response, 200)
    assert get_user(user_table)["locationId"] == "new-location"
    cognito.admin_update_user_attributes.assert_not_called()


def test_self_profile_update_is_allowed(app_state):
    app, user_table, cognito = app_state
    put_user(
        user_table,
        user_item(
            sub=CALLER_SUB,
            role="super_admin",
            location_id="",
        ),
    )

    response = app.handler(
        make_event(
            method="PUT",
            proxy=CALLER_SUB,
            groups='["super_user"]',
            body={"name": "Self Updated"},
        ),
        None,
    )

    assert_response(response, 200)
    assert get_user(user_table, CALLER_SUB)["name"] == "Self Updated"
    cognito.admin_update_user_attributes.assert_called_once()


def test_owner_cannot_update_privileged_profile(app_state):
    app, user_table, cognito = app_state
    put_user(
        user_table,
        user_item(role="owner_user", location_id=""),
    )

    response = app.handler(
        make_event(
            method="PUT",
            proxy=TARGET_SUB,
            body={"name": "Forbidden Change"},
        ),
        None,
    )

    assert_response(response, 403, {"error": "forbidden"})
    assert get_user(user_table)["name"] == "Test User"
    assert cognito.mock_calls == []


def test_profile_target_not_found_returns_404(app_state):
    app, _, cognito = app_state

    response = app.handler(
        make_event(
            method="PUT",
            proxy=TARGET_SUB,
            body={"name": "Missing"},
        ),
        None,
    )

    assert_response(response, 404, {"error": "user not found"})
    assert cognito.mock_calls == []


def test_profile_cognito_failure_leaves_dynamodb_unchanged(app_state):
    app, user_table, cognito = app_state
    original = user_item()
    put_user(user_table, original)
    cognito.admin_update_user_attributes.side_effect = client_error(
        "InternalErrorException",
        "AdminUpdateUserAttributes",
    )

    response = app.handler(
        make_event(
            method="PUT",
            proxy=TARGET_SUB,
            body={"name": "Must Not Persist"},
        ),
        None,
    )

    assert response["statusCode"] == 503
    assert get_user(user_table) == original
    assert "sensitive AWS detail" not in response["body"]


@pytest.mark.parametrize("actual_group", ["owner_user", "super_user"])
@pytest.mark.parametrize(
    ("method", "proxy", "body", "initial_status"),
    [
        ("PUT", TARGET_SUB, {"name": "Must Not Change"}, "active"),
        ("POST", f"{TARGET_SUB}/deactivate", NO_BODY, "active"),
        ("POST", f"{TARGET_SUB}/reactivate", NO_BODY, "disabled"),
        ("DELETE", TARGET_SUB, NO_BODY, "active"),
    ],
)
def test_owner_is_denied_when_cognito_group_diverges_from_staff_record(
    app_state,
    method,
    proxy,
    body,
    initial_status,
    actual_group,
):
    app, user_table, cognito = app_state
    original = user_item(status=initial_status)
    put_user(user_table, original)
    cognito.admin_list_groups_for_user.return_value = {
        "Groups": [{"GroupName": actual_group}],
    }

    response = app.handler(
        make_event(method=method, proxy=proxy, body=body),
        None,
    )

    assert_response(response, 403, {"error": "forbidden"})
    assert get_user(user_table) == original
    assert cognito.mock_calls == [
        call.admin_list_groups_for_user(
            UserPoolId=USER_POOL_ID,
            Username=TARGET_SUB,
        ),
    ]
    cognito.admin_update_user_attributes.assert_not_called()
    cognito.admin_disable_user.assert_not_called()
    cognito.admin_enable_user.assert_not_called()
    cognito.admin_delete_user.assert_not_called()


def test_profile_dynamodb_failure_restores_cognito_attributes(
    app_state,
    monkeypatch,
):
    app, user_table, cognito = app_state
    original = user_item()
    put_user(user_table, original)
    failing_table = Mock(wraps=user_table)
    failing_table.put_item.side_effect = client_error(
        "ValidationException",
        "PutItem",
    )
    monkeypatch.setattr(app, "table", lambda _: failing_table)

    response = app.handler(
        make_event(
            method="PUT",
            proxy=TARGET_SUB,
            body={"name": "Must Roll Back"},
        ),
        None,
    )

    assert_response(response, 503, {"error": "user service unavailable"})
    assert get_user(user_table) == original
    assert cognito.admin_update_user_attributes.call_args_list == [
        call(
            UserPoolId=USER_POOL_ID,
            Username=TARGET_SUB,
            UserAttributes=[
                {"Name": "name", "Value": "Must Roll Back"},
            ],
        ),
        call(
            UserPoolId=USER_POOL_ID,
            Username=TARGET_SUB,
            UserAttributes=[
                {"Name": "name", "Value": "Test User"},
            ],
        ),
    ]


@pytest.mark.parametrize(
    ("action", "initial_status", "expected_status", "cognito_method"),
    [
        ("deactivate", "active", "disabled", "admin_disable_user"),
        ("reactivate", "disabled", "active", "admin_enable_user"),
    ],
)
def test_status_actions_update_cognito_and_dynamodb(
    app_state,
    action,
    initial_status,
    expected_status,
    cognito_method,
):
    app, user_table, cognito = app_state
    put_user(user_table, user_item(status=initial_status))
    cognito.admin_get_user.return_value["Enabled"] = not (
        expected_status == "active"
    )

    response = app.handler(
        make_event(proxy=f"{TARGET_SUB}/{action}"),
        None,
    )

    assert_response(response, 200)
    assert response_body(response)["status"] == expected_status
    assert get_user(user_table)["status"] == expected_status
    getattr(cognito, cognito_method).assert_called_once_with(
        UserPoolId=USER_POOL_ID,
        Username=TARGET_SUB,
    )


@pytest.mark.parametrize("action", ["deactivate", "reactivate"])
def test_owner_cannot_change_privileged_status(app_state, action):
    app, user_table, cognito = app_state
    put_user(
        user_table,
        user_item(role="owner_user", location_id=""),
    )

    response = app.handler(
        make_event(proxy=f"{TARGET_SUB}/{action}"),
        None,
    )

    assert_response(response, 403, {"error": "forbidden"})
    assert cognito.mock_calls == []


def test_self_deactivation_is_forbidden(app_state):
    app, user_table, cognito = app_state
    put_user(
        user_table,
        user_item(
            sub=CALLER_SUB,
            role="super_admin",
            location_id="",
        ),
    )

    response = app.handler(
        make_event(
            proxy=f"{CALLER_SUB}/deactivate",
            groups='["super_user"]',
        ),
        None,
    )

    assert_response(response, 403, {"error": "forbidden"})
    assert cognito.mock_calls == []


def test_self_reactivation_is_forbidden(app_state):
    app, user_table, cognito = app_state
    put_user(
        user_table,
        user_item(
            sub=CALLER_SUB,
            role="super_admin",
            location_id="",
            status="disabled",
        ),
    )

    response = app.handler(
        make_event(
            proxy=f"{CALLER_SUB}/reactivate",
            groups='["super_user"]',
        ),
        None,
    )

    assert_response(response, 403, {"error": "forbidden"})
    assert get_user(user_table, CALLER_SUB)["status"] == "disabled"
    cognito.admin_enable_user.assert_not_called()


def test_status_target_not_found_returns_404(app_state):
    app, _, cognito = app_state

    response = app.handler(
        make_event(proxy=f"{TARGET_SUB}/deactivate"),
        None,
    )

    assert_response(response, 404, {"error": "user not found"})
    assert cognito.mock_calls == []


def test_status_cognito_transport_error_is_sanitized(app_state):
    app, user_table, cognito = app_state
    original = user_item()
    put_user(user_table, original)
    cognito.admin_disable_user.side_effect = EndpointConnectionError(
        endpoint_url="https://cognito-idp.eu-north-1.amazonaws.com",
    )

    response = app.handler(
        make_event(proxy=f"{TARGET_SUB}/deactivate"),
        None,
    )

    assert_response(response, 503, {"error": "user service unavailable"})
    assert get_user(user_table) == original


def test_status_dynamodb_failure_reverses_cognito_change(
    app_state,
    monkeypatch,
):
    app, user_table, cognito = app_state
    original = user_item()
    put_user(user_table, original)
    failing_table = Mock(wraps=user_table)
    failing_table.put_item.side_effect = client_error(
        "ValidationException",
        "PutItem",
    )
    monkeypatch.setattr(app, "table", lambda _: failing_table)

    response = app.handler(
        make_event(proxy=f"{TARGET_SUB}/deactivate"),
        None,
    )

    assert_response(response, 503, {"error": "user service unavailable"})
    assert get_user(user_table) == original
    assert cognito.mock_calls == [
        call.admin_list_groups_for_user(
            UserPoolId=USER_POOL_ID,
            Username=TARGET_SUB,
        ),
        call.admin_get_user(
            UserPoolId=USER_POOL_ID,
            Username=TARGET_SUB,
        ),
        call.admin_disable_user(
            UserPoolId=USER_POOL_ID,
            Username=TARGET_SUB,
        ),
        call.admin_enable_user(
            UserPoolId=USER_POOL_ID,
            Username=TARGET_SUB,
        ),
    ]


def test_group_change_updates_membership_role_and_location(app_state):
    app, user_table, cognito = app_state
    put_user(user_table)
    cognito.admin_list_groups_for_user.side_effect = [
        {
            "Groups": [
                {"GroupName": "staff_user"},
                {"GroupName": "unrelated"},
            ],
            "NextToken": "next-page",
        },
        {"Groups": [{"GroupName": "another-unrelated"}]},
    ]

    response = app.handler(
        make_event(
            method="PUT",
            proxy=f"{TARGET_SUB}/group",
            body={"group": "owner_user"},
            groups='["super_user"]',
        ),
        None,
    )

    assert_response(response, 200)
    result = response_body(response)
    assert result["role"] == "owner_user"
    assert result["locationId"] == ""
    assert get_user(user_table)["role"] == "owner_user"
    assert cognito.admin_list_groups_for_user.call_args_list == [
        call(UserPoolId=USER_POOL_ID, Username=TARGET_SUB),
        call(
            UserPoolId=USER_POOL_ID,
            Username=TARGET_SUB,
            NextToken="next-page",
        ),
    ]
    cognito.admin_add_user_to_group.assert_called_once_with(
        UserPoolId=USER_POOL_ID,
        Username=TARGET_SUB,
        GroupName="owner_user",
    )
    cognito.admin_remove_user_from_group.assert_called_once_with(
        UserPoolId=USER_POOL_ID,
        Username=TARGET_SUB,
        GroupName="staff_user",
    )


def test_same_staff_group_can_change_location_without_membership_calls(
    app_state,
):
    app, user_table, cognito = app_state
    put_user(user_table)

    response = app.handler(
        make_event(
            method="PUT",
            proxy=f"{TARGET_SUB}/group",
            body={"group": "staff_user", "locationId": "new-location"},
        ),
        None,
    )

    assert_response(response, 200)
    assert get_user(user_table)["locationId"] == "new-location"
    cognito.admin_add_user_to_group.assert_not_called()
    cognito.admin_remove_user_from_group.assert_not_called()


def test_owner_cannot_promote_staff(app_state):
    app, user_table, cognito = app_state
    put_user(user_table)

    response = app.handler(
        make_event(
            method="PUT",
            proxy=f"{TARGET_SUB}/group",
            body={"group": "owner_user"},
        ),
        None,
    )

    assert_response(response, 403, {"error": "forbidden"})
    assert get_user(user_table) == user_item()
    assert cognito.mock_calls == []


def test_self_group_change_is_forbidden(app_state):
    app, user_table, cognito = app_state
    put_user(
        user_table,
        user_item(
            sub=CALLER_SUB,
            role="super_admin",
            location_id="",
        ),
    )

    response = app.handler(
        make_event(
            method="PUT",
            proxy=f"{CALLER_SUB}/group",
            body={"group": "owner_user"},
            groups='["super_user"]',
        ),
        None,
    )

    assert_response(response, 403, {"error": "forbidden"})
    assert cognito.mock_calls == []


def test_group_add_failure_does_not_remove_old_group_or_update_record(
    app_state,
):
    app, user_table, cognito = app_state
    original = user_item()
    put_user(user_table, original)
    cognito.admin_add_user_to_group.side_effect = client_error(
        "InternalErrorException",
        "AdminAddUserToGroup",
    )

    response = app.handler(
        make_event(
            method="PUT",
            proxy=f"{TARGET_SUB}/group",
            body={"group": "owner_user"},
            groups='["super_user"]',
        ),
        None,
    )

    assert response["statusCode"] == 503
    assert get_user(user_table) == original
    cognito.admin_remove_user_from_group.assert_not_called()
    assert "sensitive AWS detail" not in response["body"]


def test_group_with_no_managed_membership_is_repaired_by_super_user(
    app_state,
):
    app, user_table, cognito = app_state
    put_user(user_table)
    cognito.admin_list_groups_for_user.return_value = {
        "Groups": [{"GroupName": "unrelated"}],
    }

    response = app.handler(
        make_event(
            method="PUT",
            proxy=f"{TARGET_SUB}/group",
            body={"group": "owner_user"},
            groups='["super_user"]',
        ),
        None,
    )

    assert_response(response, 200)
    assert get_user(user_table)["role"] == "owner_user"
    assert get_user(user_table)["locationId"] == ""
    cognito.admin_add_user_to_group.assert_called_once_with(
        UserPoolId=USER_POOL_ID,
        Username=TARGET_SUB,
        GroupName="owner_user",
    )
    cognito.admin_remove_user_from_group.assert_not_called()


def test_group_dynamodb_failure_restores_previous_membership(
    app_state,
    monkeypatch,
):
    app, user_table, cognito = app_state
    original = user_item()
    put_user(user_table, original)
    failing_table = Mock(wraps=user_table)
    failing_table.put_item.side_effect = client_error(
        "ValidationException",
        "PutItem",
    )
    monkeypatch.setattr(app, "table", lambda _: failing_table)

    response = app.handler(
        make_event(
            method="PUT",
            proxy=f"{TARGET_SUB}/group",
            body={"group": "owner_user"},
            groups='["super_user"]',
        ),
        None,
    )

    assert_response(response, 503, {"error": "user service unavailable"})
    assert get_user(user_table) == original
    assert cognito.mock_calls == [
        call.admin_list_groups_for_user(
            UserPoolId=USER_POOL_ID,
            Username=TARGET_SUB,
        ),
        call.admin_add_user_to_group(
            UserPoolId=USER_POOL_ID,
            Username=TARGET_SUB,
            GroupName="owner_user",
        ),
        call.admin_remove_user_from_group(
            UserPoolId=USER_POOL_ID,
            Username=TARGET_SUB,
            GroupName="staff_user",
        ),
        call.admin_add_user_to_group(
            UserPoolId=USER_POOL_ID,
            Username=TARGET_SUB,
            GroupName="staff_user",
        ),
        call.admin_remove_user_from_group(
            UserPoolId=USER_POOL_ID,
            Username=TARGET_SUB,
            GroupName="owner_user",
        ),
    ]


def test_delete_removes_cognito_and_dynamodb_user(app_state):
    app, user_table, cognito = app_state
    put_user(user_table)

    response = app.handler(
        make_event(method="DELETE", proxy=TARGET_SUB),
        None,
    )

    assert_response(response, 204, None)
    assert get_user(user_table) is None
    cognito.admin_delete_user.assert_called_once_with(
        UserPoolId=USER_POOL_ID,
        Username=TARGET_SUB,
    )


def test_delete_treats_missing_cognito_mirror_as_idempotent(app_state):
    app, user_table, cognito = app_state
    put_user(user_table)
    cognito.admin_delete_user.side_effect = client_error(
        "UserNotFoundException",
        "AdminDeleteUser",
    )

    response = app.handler(
        make_event(method="DELETE", proxy=TARGET_SUB),
        None,
    )

    assert_response(response, 204, None)
    assert get_user(user_table) is None


def test_delete_missing_dynamodb_target_returns_404_without_cognito(
    app_state,
):
    app, _, cognito = app_state

    response = app.handler(
        make_event(method="DELETE", proxy=TARGET_SUB),
        None,
    )

    assert_response(response, 404, {"error": "user not found"})
    cognito.admin_delete_user.assert_not_called()


def test_owner_cannot_delete_privileged_user(app_state):
    app, user_table, cognito = app_state
    put_user(
        user_table,
        user_item(role="owner_user", location_id=""),
    )

    response = app.handler(
        make_event(method="DELETE", proxy=TARGET_SUB),
        None,
    )

    assert_response(response, 403, {"error": "forbidden"})
    assert get_user(user_table) is not None
    assert cognito.mock_calls == []


def test_self_delete_is_forbidden(app_state):
    app, user_table, cognito = app_state
    put_user(
        user_table,
        user_item(
            sub=CALLER_SUB,
            role="super_admin",
            location_id="",
        ),
    )

    response = app.handler(
        make_event(
            method="DELETE",
            proxy=CALLER_SUB,
            groups='["super_user"]',
        ),
        None,
    )

    assert_response(response, 403, {"error": "forbidden"})
    assert get_user(user_table, CALLER_SUB) is not None
    assert cognito.mock_calls == []


@pytest.mark.parametrize(
    "error",
    [
        client_error("InternalErrorException", "AdminDeleteUser"),
        EndpointConnectionError(
            endpoint_url="https://cognito-idp.eu-north-1.amazonaws.com"
        ),
    ],
)
def test_delete_dependency_failures_are_sanitized_and_keep_record(
    app_state,
    error,
):
    app, user_table, cognito = app_state
    original = user_item()
    put_user(user_table, original)
    cognito.admin_delete_user.side_effect = error

    response = app.handler(
        make_event(method="DELETE", proxy=TARGET_SUB),
        None,
    )

    assert_response(response, 503, {"error": "user service unavailable"})
    assert get_user(user_table) == original
    assert "sensitive AWS detail" not in response["body"]


def test_profile_rollback_uses_live_cognito_values_and_verification_flags(
    app_state,
    monkeypatch,
):
    app, user_table, cognito = app_state
    original = user_item(
        name="Stale Dynamo Name",
        email="stale.dynamo@example.com",
        phone="+46702222222",
    )
    put_user(user_table, original)
    cognito.admin_get_user.return_value = {
        "Enabled": True,
        "UserAttributes": [
            {"Name": "name", "Value": "Live Cognito Name"},
            {"Name": "email", "Value": "live.cognito@example.com"},
            {"Name": "email_verified", "Value": "false"},
            {"Name": "phone_number", "Value": "+46701111111"},
            {"Name": "phone_number_verified", "Value": "true"},
        ],
    }
    failing_table = Mock(wraps=user_table)
    failing_table.put_item.side_effect = client_error(
        "ValidationException",
        "PutItem",
    )
    monkeypatch.setattr(app, "table", lambda _: failing_table)

    response = app.handler(
        make_event(
            method="PUT",
            proxy=TARGET_SUB,
            body={
                "name": "New Name",
                "email": "new.user@example.com",
                "phone": "+46709999999",
            },
        ),
        None,
    )

    assert_response(response, 503, {"error": "user service unavailable"})
    assert get_user(user_table) == original
    cognito.admin_get_user.assert_called_once_with(
        UserPoolId=USER_POOL_ID,
        Username=TARGET_SUB,
    )
    assert cognito.admin_update_user_attributes.call_args_list == [
        call(
            UserPoolId=USER_POOL_ID,
            Username=TARGET_SUB,
            UserAttributes=[
                {"Name": "name", "Value": "New Name"},
                {"Name": "email", "Value": "new.user@example.com"},
                {"Name": "phone_number", "Value": "+46709999999"},
            ],
        ),
        call(
            UserPoolId=USER_POOL_ID,
            Username=TARGET_SUB,
            UserAttributes=[
                {"Name": "name", "Value": "Live Cognito Name"},
                {"Name": "email", "Value": "live.cognito@example.com"},
                {"Name": "email_verified", "Value": "false"},
                {"Name": "phone_number", "Value": "+46701111111"},
                {"Name": "phone_number_verified", "Value": "true"},
            ],
        ),
    ]


@pytest.mark.parametrize(
    ("snapshot", "expected_status"),
    [
        (None, 503),
        ({"Enabled": "true", "UserAttributes": []}, 503),
        ({"Enabled": True}, 503),
        ({"Enabled": True, "UserAttributes": "invalid"}, 503),
        (
            {
                "Enabled": True,
                "UserAttributes": [
                    {
                        "Name": "email",
                        "Value": "live@example.com",
                    }
                ],
            },
            409,
        ),
    ],
)
def test_invalid_profile_snapshot_prevents_cognito_mutation(
    app_state,
    snapshot,
    expected_status,
):
    app, user_table, cognito = app_state
    original = user_item()
    put_user(user_table, original)
    cognito.admin_get_user.return_value = snapshot

    response = app.handler(
        make_event(
            method="PUT",
            proxy=TARGET_SUB,
            body={"name": "Must Not Be Applied"},
        ),
        None,
    )

    assert response["statusCode"] == expected_status
    assert get_user(user_table) == original
    cognito.admin_get_user.assert_called_once()
    cognito.admin_update_user_attributes.assert_not_called()


@pytest.mark.parametrize(
    (
        "action",
        "initial_status",
        "cognito_enabled",
        "expected_status",
        "expects_cognito_change",
    ),
    [
        ("deactivate", "active", True, "disabled", True),
        ("deactivate", "active", False, "disabled", False),
        ("deactivate", "disabled", True, "disabled", True),
        ("deactivate", "disabled", False, "disabled", False),
        ("reactivate", "disabled", False, "active", True),
        ("reactivate", "disabled", True, "active", False),
        ("reactivate", "active", False, "active", True),
        ("reactivate", "active", True, "active", False),
    ],
)
def test_status_actions_reconcile_cognito_and_dynamodb_drift(
    app_state,
    action,
    initial_status,
    cognito_enabled,
    expected_status,
    expects_cognito_change,
):
    app, user_table, cognito = app_state
    put_user(user_table, user_item(status=initial_status))
    cognito.admin_get_user.return_value["Enabled"] = cognito_enabled

    response = app.handler(
        make_event(proxy=f"{TARGET_SUB}/{action}"),
        None,
    )

    assert_response(response, 200)
    assert response_body(response)["status"] == expected_status
    assert get_user(user_table)["status"] == expected_status
    cognito.admin_get_user.assert_called_once_with(
        UserPoolId=USER_POOL_ID,
        Username=TARGET_SUB,
    )
    mutation = (
        cognito.admin_disable_user
        if action == "deactivate"
        else cognito.admin_enable_user
    )
    assert mutation.call_count == int(expects_cognito_change)
    inverse = (
        cognito.admin_enable_user
        if action == "deactivate"
        else cognito.admin_disable_user
    )
    inverse.assert_not_called()


def test_status_write_failure_does_not_inverse_unchanged_cognito(
    app_state,
    monkeypatch,
):
    app, user_table, cognito = app_state
    original = user_item(status="active")
    put_user(user_table, original)
    cognito.admin_get_user.return_value["Enabled"] = False
    failing_table = Mock(wraps=user_table)
    failing_table.put_item.side_effect = client_error(
        "ValidationException",
        "PutItem",
    )
    monkeypatch.setattr(app, "table", lambda _: failing_table)

    response = app.handler(
        make_event(proxy=f"{TARGET_SUB}/deactivate"),
        None,
    )

    assert_response(response, 503, {"error": "user service unavailable"})
    assert get_user(user_table) == original
    cognito.admin_disable_user.assert_not_called()
    cognito.admin_enable_user.assert_not_called()


def test_existing_write_condition_includes_expected_role_and_status(
    app_state,
    monkeypatch,
):
    app, user_table, _ = app_state
    original = user_item()
    put_user(user_table, original)
    table_spy = Mock(wraps=user_table)
    monkeypatch.setattr(app, "table", lambda _: table_spy)

    response = app.handler(
        make_event(
            method="PUT",
            proxy=TARGET_SUB,
            body={"name": "Condition Checked"},
        ),
        None,
    )

    assert_response(response, 200)
    write = table_spy.put_item.call_args.kwargs
    names = write["ExpressionAttributeNames"]
    values = write["ExpressionAttributeValues"]
    condition = write["ConditionExpression"]
    for field in ("role", "status"):
        name_key = next(key for key, value in names.items() if value == field)
        value_key = name_key.replace("#expected", ":expected")
        assert f"{name_key} = {value_key}" in condition
        assert values[value_key] == original[field]


def test_conditional_profile_conflict_returns_409_and_rolls_back_cognito(
    app_state,
    monkeypatch,
):
    app, user_table, cognito = app_state
    original = user_item()
    put_user(user_table, original)
    failing_table = Mock(wraps=user_table)
    failing_table.put_item.side_effect = client_error(
        "ConditionalCheckFailedException",
        "PutItem",
    )
    monkeypatch.setattr(app, "table", lambda _: failing_table)

    response = app.handler(
        make_event(
            method="PUT",
            proxy=TARGET_SUB,
            body={"name": "Conflicting Change"},
        ),
        None,
    )

    assert response["statusCode"] == 409
    assert get_user(user_table) == original
    assert cognito.admin_update_user_attributes.call_args_list == [
        call(
            UserPoolId=USER_POOL_ID,
            Username=TARGET_SUB,
            UserAttributes=[
                {"Name": "name", "Value": "Conflicting Change"},
            ],
        ),
        call(
            UserPoolId=USER_POOL_ID,
            Username=TARGET_SUB,
            UserAttributes=[
                {"Name": "name", "Value": "Test User"},
            ],
        ),
    ]


def test_ambiguous_invite_write_retries_without_cognito_compensation(
    app_state,
    monkeypatch,
):
    app, user_table, cognito = app_state
    failing_table = Mock(wraps=user_table)
    failing_table.put_item.side_effect = [
        client_error("InternalServerError", "PutItem"),
        client_error("InternalServerError", "PutItem"),
    ]
    monkeypatch.setattr(app, "table", lambda _: failing_table)

    response = app.handler(make_event(body=invite_body()), None)

    assert_response(response, 503, {"error": "user service unavailable"})
    assert get_user(user_table) is None
    assert failing_table.put_item.call_count == 2
    cognito.admin_delete_user.assert_not_called()


def test_ambiguous_profile_write_retries_without_cognito_compensation(
    app_state,
    monkeypatch,
):
    app, user_table, cognito = app_state
    original = user_item()
    put_user(user_table, original)
    failing_table = Mock(wraps=user_table)
    failing_table.put_item.side_effect = [
        client_error("InternalServerError", "PutItem"),
        client_error("InternalServerError", "PutItem"),
    ]
    monkeypatch.setattr(app, "table", lambda _: failing_table)

    response = app.handler(
        make_event(
            method="PUT",
            proxy=TARGET_SUB,
            body={"name": "Ambiguous Name"},
        ),
        None,
    )

    assert_response(response, 503, {"error": "user service unavailable"})
    assert get_user(user_table) == original
    assert failing_table.put_item.call_count == 2
    assert cognito.admin_update_user_attributes.call_args_list == [
        call(
            UserPoolId=USER_POOL_ID,
            Username=TARGET_SUB,
            UserAttributes=[
                {"Name": "name", "Value": "Ambiguous Name"},
            ],
        ),
    ]


def test_ambiguous_status_write_retries_without_cognito_inverse(
    app_state,
    monkeypatch,
):
    app, user_table, cognito = app_state
    original = user_item(status="active")
    put_user(user_table, original)
    cognito.admin_get_user.return_value["Enabled"] = True
    failing_table = Mock(wraps=user_table)
    failing_table.put_item.side_effect = [
        client_error("InternalServerError", "PutItem"),
        client_error("InternalServerError", "PutItem"),
    ]
    monkeypatch.setattr(app, "table", lambda _: failing_table)

    response = app.handler(
        make_event(proxy=f"{TARGET_SUB}/deactivate"),
        None,
    )

    assert_response(response, 503, {"error": "user service unavailable"})
    assert get_user(user_table) == original
    assert failing_table.put_item.call_count == 2
    cognito.admin_disable_user.assert_called_once()
    cognito.admin_enable_user.assert_not_called()


def test_ambiguous_delete_retries_and_preserves_prior_dynamodb_state(
    app_state,
    monkeypatch,
):
    app, user_table, cognito = app_state
    original = user_item()
    put_user(user_table, original)
    failing_table = Mock(wraps=user_table)
    failing_table.delete_item.side_effect = [
        client_error("InternalServerError", "DeleteItem"),
        client_error("InternalServerError", "DeleteItem"),
    ]
    monkeypatch.setattr(app, "table", lambda _: failing_table)

    response = app.handler(
        make_event(method="DELETE", proxy=TARGET_SUB),
        None,
    )

    assert_response(response, 503, {"error": "user service unavailable"})
    assert get_user(user_table) == original
    assert failing_table.delete_item.call_count == 2
    cognito.admin_delete_user.assert_called_once()


def test_invite_put_that_commits_then_errors_is_reconciled_without_delete(
    app_state,
    monkeypatch,
):
    app, user_table, cognito = app_state
    table_spy = Mock(wraps=user_table)

    def commit_then_error(**kwargs):
        user_table.put_item(**kwargs)
        raise client_error("InternalServerError", "PutItem")

    table_spy.put_item.side_effect = commit_then_error
    monkeypatch.setattr(app, "table", lambda _: table_spy)

    response = app.handler(make_event(body=invite_body()), None)

    assert_response(response, 201)
    assert get_user(user_table) is not None
    cognito.admin_delete_user.assert_not_called()


def test_profile_put_that_commits_then_errors_does_not_roll_back_cognito(
    app_state,
    monkeypatch,
):
    app, user_table, cognito = app_state
    put_user(user_table)
    table_spy = Mock(wraps=user_table)

    def commit_then_error(**kwargs):
        user_table.put_item(**kwargs)
        raise client_error("InternalServerError", "PutItem")

    table_spy.put_item.side_effect = commit_then_error
    monkeypatch.setattr(app, "table", lambda _: table_spy)

    response = app.handler(
        make_event(
            method="PUT",
            proxy=TARGET_SUB,
            body={"name": "Committed Name"},
        ),
        None,
    )

    assert_response(response, 200)
    assert get_user(user_table)["name"] == "Committed Name"
    assert cognito.admin_update_user_attributes.call_count == 1
    assert cognito.admin_update_user_attributes.call_args.kwargs[
        "UserAttributes"
    ] == [{"Name": "name", "Value": "Committed Name"}]


def test_delete_that_commits_then_errors_is_reconciled_as_success(
    app_state,
    monkeypatch,
):
    app, user_table, cognito = app_state
    put_user(user_table)
    table_spy = Mock(wraps=user_table)

    def commit_then_error(**kwargs):
        user_table.delete_item(**kwargs)
        raise client_error("InternalServerError", "DeleteItem")

    table_spy.delete_item.side_effect = commit_then_error
    monkeypatch.setattr(app, "table", lambda _: table_spy)

    response = app.handler(
        make_event(method="DELETE", proxy=TARGET_SUB),
        None,
    )

    assert_response(response, 204, None)
    assert get_user(user_table) is None
    cognito.admin_delete_user.assert_called_once()


def test_internal_dynamodb_fields_never_appear_in_user_response(app_state):
    app, user_table, _ = app_state
    stored = {**user_item(), "internalSecret": "must-not-leak"}
    put_user(user_table, stored)

    response = app.handler(
        make_event(
            method="PUT",
            proxy=TARGET_SUB,
            body={"name": "Test User"},
        ),
        None,
    )
    result = response_body(response)

    assert_response(response, 200)
    assert "internalSecret" not in result
    assert "PK" not in result and "SK" not in result
    assert set(result) == {
        "cognitoSub",
        "role",
        "locationId",
        "name",
        "email",
        "phone",
        "status",
        "createdBy",
        "createdAt",
    }


def test_missing_public_user_field_returns_409_without_mutation(app_state):
    app, user_table, cognito = app_state
    incomplete = user_item()
    del incomplete["phone"]
    put_user(user_table, incomplete)

    response = app.handler(
        make_event(
            method="PUT",
            proxy=TARGET_SUB,
            body={"name": "Test User"},
        ),
        None,
    )

    assert response["statusCode"] == 409
    assert get_user(user_table) == incomplete
    cognito.admin_get_user.assert_not_called()
    cognito.admin_update_user_attributes.assert_not_called()

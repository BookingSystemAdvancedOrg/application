"""manage-user

TRIGGER:
    API Gateway -- GET /list-users and ANY /users/{proxy+} -- Auth: JWT

PURPOSE:
    Lists and reads internal users and manages their lifecycle: invite,
    profile update, deactivate/reactivate, delete, and Cognito group change.
    Callers must be owner_user or super_user. Owners may read staff_user
    accounts and themselves and manage staff_user accounts; only super-users
    may read or manage other privileged accounts.

ROUTES:
    GET    /list-users
    POST   /users/invite
    GET    /users/{cognitoSub}
    PUT    /users/{cognitoSub}
    POST   /users/{cognitoSub}/deactivate
    POST   /users/{cognitoSub}/reactivate
    PUT    /users/{cognitoSub}/group
    DELETE /users/{cognitoSub}

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    USER_TABLE_NAME -- App-side user record (role, assigned location, etc.)
    COGNITO_USER_POOL_ID -- Target user pool for the Cognito admin calls

AWS RESOURCE ACCESS:
    Full dynamodb:* on the User table. Cognito AdminCreateUser,
    AdminDeleteUser, AdminDisableUser, AdminEnableUser,
    AdminUpdateUserAttributes, AdminAddUserToGroup,
    AdminRemoveUserFromGroup, AdminGetUser, and AdminListGroupsForUser on the
    configured user pool.

NOTES:
    User listing uses the DynamoDB directory mirror so it does not make one
    Cognito request per row. Owner results are limited to mirrored staff rows
    plus the caller's own row. A single non-self owner read verifies the
    target's live Cognito group before returning it.

    Cognito and DynamoDB cannot be updated atomically. The handler compensates
    completed Cognito steps when a later step fails where the available API
    actions permit a safe rollback.

Full details: docs/LAMBDA_REFERENCE.md
"""

import base64
import binascii
import json
import logging
import os
import re
from datetime import datetime, timezone
from http import HTTPStatus

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from shared.auth import (
    Unauthorized,
    get_claims,
    get_groups,
    get_sub,
    require_group,
)
from shared.dynamo import table
from shared.responses import json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
USER_TABLE_NAME = os.environ["USER_TABLE_NAME"]
COGNITO_USER_POOL_ID = os.environ["COGNITO_USER_POOL_ID"]

_GROUP_TO_ROLE = {
    "staff_user": "staff",
    "owner_user": "owner_user",
    "super_user": "super_admin",
}
_ROLE_TO_GROUP = {role: group for group, role in _GROUP_TO_ROLE.items()}
_MANAGED_GROUPS = frozenset(_GROUP_TO_ROLE)

_INVITE_FIELDS = {"name", "email", "phone", "group", "locationId"}
_PROFILE_FIELDS = {"name", "email", "phone", "locationId"}
_GROUP_FIELDS = {"group", "locationId"}

_PUBLIC_USER_FIELDS = (
    "cognitoSub",
    "role",
    "locationId",
    "name",
    "email",
    "phone",
    "status",
    "createdBy",
    "createdAt",
)
_PROFILE_TO_COGNITO_ATTRIBUTE = {
    "name": "name",
    "email": "email",
    "phone": "phone_number",
}
_VERIFICATION_ATTRIBUTES = {
    "email": "email_verified",
    "phone": "phone_number_verified",
}

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_PATTERN = re.compile(r"^\+[1-9]\d{7,14}$")

_COGNITO_ERROR_MAPPING = {
    "UsernameExistsException": (
        HTTPStatus.CONFLICT.value,
        "user already exists",
    ),
    "AliasExistsException": (
        HTTPStatus.CONFLICT.value,
        "user already exists",
    ),
    "UserNotFoundException": (
        HTTPStatus.NOT_FOUND.value,
        "user not found",
    ),
    "InvalidParameterException": (
        HTTPStatus.BAD_REQUEST.value,
        "invalid user request",
    ),
    "InvalidPasswordException": (
        HTTPStatus.BAD_REQUEST.value,
        "invalid user request",
    ),
    "TooManyRequestsException": (
        HTTPStatus.TOO_MANY_REQUESTS.value,
        "too many user-management requests",
    ),
    "LimitExceededException": (
        HTTPStatus.TOO_MANY_REQUESTS.value,
        "too many user-management requests",
    ),
}

_cognito_client = None
_logger = logging.getLogger(__name__)


class _ForbiddenAction(Exception):
    """The authenticated caller may not perform the target action."""


class _UserConflict(Exception):
    """The mirrored user state is inconsistent or changed concurrently."""


class _ExpectedStateConflict(_UserConflict):
    """A conditional write proved that this request did not persist."""


class _UserServiceFailure(Exception):
    """An AWS response was structurally invalid for this workflow."""


def _get_cognito_client():
    global _cognito_client

    if _cognito_client is None:
        _cognito_client = boto3.client("cognito-idp")

    return _cognito_client


def _user_response(status_code, body, *, headers=None):
    return json_response(
        status_code,
        body,
        headers={
            "Cache-Control": "no-store",
            **(headers or {}),
        },
    )


def _user_error(status_code, message):
    return _user_response(status_code, {"error": message})


def _empty_response(status_code):
    return {
        "statusCode": status_code,
        "headers": {"Cache-Control": "no-store"},
        "body": "",
    }


def _parse_json_body(event):
    raw_body = event.get("body")

    if not isinstance(raw_body, str) or not raw_body.strip():
        raise ValueError("request body is required")

    if event.get("isBase64Encoded") is True:
        try:
            raw_body = base64.b64decode(
                raw_body,
                validate=True,
            ).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            raise ValueError("request body must be valid base64") from None

    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        raise ValueError("request body must be valid JSON") from None

    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")

    return body


def _validate_fields(body, allowed_fields, *, require_one=False):
    unsupported_fields = sorted(set(body) - allowed_fields)
    if unsupported_fields:
        raise ValueError(
            f"unsupported fields: {', '.join(unsupported_fields)}"
        )

    if require_one and not body:
        raise ValueError("at least one editable field is required")


def _required_string(body, field):
    value = body.get(field)

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")

    return value.strip()


def _email(body, *, required):
    if not required and "email" not in body:
        return None

    value = _required_string(body, "email")
    if len(value) > 320 or not _EMAIL_PATTERN.fullmatch(value):
        raise ValueError("email must be valid")

    return value


def _phone(body, *, required):
    if not required and "phone" not in body:
        return None

    value = _required_string(body, "phone")
    if not _PHONE_PATTERN.fullmatch(value):
        raise ValueError("phone must use E.164 format")

    return value


def _group_and_location(body):
    group = _required_string(body, "group")
    if group not in _GROUP_TO_ROLE:
        raise ValueError("group must be staff_user, owner_user, or super_user")

    raw_location_id = body.get("locationId", "")
    if not isinstance(raw_location_id, str):
        raise ValueError("locationId must be a string")

    location_id = raw_location_id.strip()
    if group == "staff_user" and not location_id:
        raise ValueError("locationId is required for staff_user")
    if group != "staff_user" and location_id:
        raise ValueError("locationId must be empty for privileged users")

    return group, location_id


def _invite_body(event):
    body = _parse_json_body(event)
    _validate_fields(body, _INVITE_FIELDS)

    name = _required_string(body, "name")
    email = _email(body, required=True)
    phone = _phone(body, required=True)
    group, location_id = _group_and_location(body)

    return {
        "name": name,
        "email": email,
        "phone": phone,
        "group": group,
        "locationId": location_id,
    }


def _profile_body(event):
    body = _parse_json_body(event)
    _validate_fields(body, _PROFILE_FIELDS, require_one=True)

    updates = {}
    if "name" in body:
        updates["name"] = _required_string(body, "name")
    if "email" in body:
        updates["email"] = _email(body, required=False)
    if "phone" in body:
        updates["phone"] = _phone(body, required=False)
    if "locationId" in body:
        location_id = body["locationId"]
        if not isinstance(location_id, str):
            raise ValueError("locationId must be a string")
        updates["locationId"] = location_id.strip()

    return updates


def _group_body(event):
    body = _parse_json_body(event)
    _validate_fields(body, _GROUP_FIELDS)
    return _group_and_location(body)


def _match_route(proxy_path):
    if not isinstance(proxy_path, str):
        return None

    normalized = proxy_path.strip("/")
    if not normalized:
        return "list", None, ("GET",)

    segments = normalized.split("/")
    if segments == ["invite"]:
        return "invite", None, ("POST",)

    if len(segments) == 1 and segments[0]:
        return "user", segments[0], ("GET", "PUT", "DELETE")

    if len(segments) == 2 and all(segments):
        target_sub, suffix = segments
        if suffix == "deactivate":
            return "deactivate", target_sub, ("POST",)
        if suffix == "reactivate":
            return "reactivate", target_sub, ("POST",)
        if suffix == "group":
            return "group", target_sub, ("PUT",)

    return None


def _target_sub(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("cognitoSub is required")

    value = value.strip()
    if len(value) > 128:
        raise ValueError("cognitoSub is invalid")

    return value


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _public_user(item):
    if not isinstance(item, dict):
        raise _UserConflict
    if any(field not in item for field in _PUBLIC_USER_FIELDS):
        raise _UserConflict

    return {field: item[field] for field in _PUBLIC_USER_FIELDS}


def _validated_stored_user(item, expected_sub=None):
    public_user = _public_user(item)
    cognito_sub = public_user["cognitoSub"]

    if (
        not isinstance(cognito_sub, str)
        or not cognito_sub
        or cognito_sub != cognito_sub.strip()
        or len(cognito_sub) > 128
        or (expected_sub is not None and cognito_sub != expected_sub)
        or item.get("PK") != f"USER#{cognito_sub}"
        or item.get("SK") != "PROFILE"
    ):
        raise _UserConflict

    if any(not isinstance(value, str) for value in public_user.values()):
        raise _UserConflict
    if any(
        not public_user[field].strip()
        for field in ("name", "email", "phone", "createdBy", "createdAt")
    ):
        raise _UserConflict
    if public_user["role"] not in _ROLE_TO_GROUP:
        raise _UserConflict
    if public_user["status"] not in {"active", "disabled"}:
        raise _UserConflict

    location_id = public_user["locationId"]
    if public_user["role"] == "staff":
        if not location_id:
            raise _UserConflict
    elif location_id:
        raise _UserConflict

    return public_user


def _read_user(target_sub):
    response = table(USER_TABLE_NAME).get_item(
        Key={
            "PK": f"USER#{target_sub}",
            "SK": "PROFILE",
        },
        ConsistentRead=True,
    )
    if not isinstance(response, dict):
        raise _UserServiceFailure

    item = response.get("Item")
    if item is not None and not isinstance(item, dict):
        raise _UserServiceFailure
    return item


def _load_user(target_sub):
    item = _read_user(target_sub)
    if item is None:
        return None
    _validated_stored_user(item, target_sub)
    return item


def _list_users(caller_sub, caller_groups):
    user_table = table(USER_TABLE_NAME)
    request = {"ConsistentRead": True}
    users = []
    seen_last_keys = []
    is_super_user = _is_super_user(caller_groups)

    while True:
        response = user_table.scan(**request)
        if not isinstance(response, dict):
            raise _UserServiceFailure

        page = response.get("Items")
        if not isinstance(page, list):
            raise _UserServiceFailure

        for item in page:
            if not isinstance(item, dict):
                raise _UserServiceFailure

            public_user = _validated_stored_user(item)
            if (
                is_super_user
                or public_user["role"] == "staff"
                or public_user["cognitoSub"] == caller_sub
            ):
                users.append(public_user)

        last_key = response.get("LastEvaluatedKey")
        if last_key is None:
            users.sort(
                key=lambda user: (
                    user["name"].casefold(),
                    user["cognitoSub"],
                )
            )
            return _user_response(
                HTTPStatus.OK.value,
                {"items": users},
            )

        if (
            not isinstance(last_key, dict)
            or set(last_key) != {"PK", "SK"}
            or not all(
                isinstance(last_key[key], str) and last_key[key]
                for key in ("PK", "SK")
            )
            or any(last_key == seen_key for seen_key in seen_last_keys)
        ):
            raise _UserServiceFailure

        seen_last_keys.append(last_key)
        request["ExclusiveStartKey"] = last_key


def _expected_item_condition(expected):
    clauses = ["attribute_exists(PK)", "attribute_exists(SK)"]
    names = {}
    values = {}

    for index, field in enumerate(sorted(set(expected) - {"PK", "SK"})):
        name_key = f"#expected{index}"
        value_key = f":expected{index}"
        clauses.append(f"{name_key} = {value_key}")
        names[name_key] = field
        values[value_key] = expected[field]

    return {
        "ConditionExpression": " AND ".join(clauses),
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": values,
    }


def _reconcile_user_state(target_sub, desired, previous):
    try:
        current = _read_user(target_sub)
    except (BotoCoreError, ClientError, _UserServiceFailure):
        raise _UserServiceFailure from None

    if current == desired:
        return "applied"
    if current == previous:
        return "not_applied"
    return "conflict"


def _is_ambiguous_dynamo_error(exc):
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        error_code = error.get("Code")
        response_metadata = exc.response.get("ResponseMetadata", {})
        status_code = response_metadata.get("HTTPStatusCode")
        return (
            error_code
            in {
                "InternalFailure",
                "InternalServerError",
                "RequestTimeout",
                "RequestTimeoutException",
                "ServiceUnavailable",
            }
            or isinstance(status_code, int)
            and status_code >= 500
        )

    return isinstance(exc, BotoCoreError)


def _recover_ambiguous_write(write, target_sub, desired, previous):
    outcome = _reconcile_user_state(target_sub, desired, previous)
    if outcome == "applied":
        return
    if outcome == "conflict":
        raise _UserConflict

    try:
        write()
        return
    except (BotoCoreError, ClientError):
        outcome = _reconcile_user_state(target_sub, desired, previous)
        if outcome == "applied":
            return
        if outcome == "conflict":
            raise _UserConflict from None
        raise _UserServiceFailure from None


def _put_new_user(item):
    target_sub = item["cognitoSub"]

    def write():
        table(USER_TABLE_NAME).put_item(
            Item=item,
            ConditionExpression=(
                "attribute_not_exists(PK) AND attribute_not_exists(SK)"
            ),
        )

    try:
        write()
    except (BotoCoreError, ClientError) as exc:
        if not _is_ambiguous_dynamo_error(exc):
            raise
        _recover_ambiguous_write(write, target_sub, item, None)


def _put_existing_user(item, expected):
    target_sub = item["cognitoSub"]

    def write():
        table(USER_TABLE_NAME).put_item(
            Item=item,
            **_expected_item_condition(expected),
        )

    try:
        write()
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code == "ConditionalCheckFailedException":
            raise _ExpectedStateConflict from None
        if not _is_ambiguous_dynamo_error(exc):
            raise
        _recover_ambiguous_write(write, target_sub, item, expected)
    except BotoCoreError as exc:
        _recover_ambiguous_write(write, target_sub, item, expected)


def _delete_existing_user(target_sub, expected):
    def write():
        table(USER_TABLE_NAME).delete_item(
            Key={
                "PK": f"USER#{target_sub}",
                "SK": "PROFILE",
            },
            **_expected_item_condition(expected),
        )

    try:
        write()
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code == "ConditionalCheckFailedException":
            raise _ExpectedStateConflict from None
        if not _is_ambiguous_dynamo_error(exc):
            raise
        _recover_ambiguous_write(write, target_sub, None, expected)
    except BotoCoreError as exc:
        _recover_ambiguous_write(write, target_sub, None, expected)


def _try_cognito(method_name, **kwargs):
    try:
        getattr(_get_cognito_client(), method_name)(**kwargs)
    except (BotoCoreError, ClientError):
        _logger.error("Cognito compensation failed for %s", method_name)
        return False
    return True


def _rollback_created_user(username):
    if not username:
        return False

    return _try_cognito(
        "admin_delete_user",
        UserPoolId=COGNITO_USER_POOL_ID,
        Username=username,
    )


def _cognito_sub(user):
    attributes = user.get("Attributes", [])
    if not isinstance(attributes, list):
        return None

    for attribute in attributes:
        if not isinstance(attribute, dict):
            continue
        if attribute.get("Name") == "sub":
            value = attribute.get("Value")
            if isinstance(value, str) and value:
                return value
    return None


def _admin_user_snapshot(target_sub):
    response = _get_cognito_client().admin_get_user(
        UserPoolId=COGNITO_USER_POOL_ID,
        Username=target_sub,
    )
    if not isinstance(response, dict):
        raise _UserServiceFailure

    enabled = response.get("Enabled")
    if not isinstance(enabled, bool):
        raise _UserServiceFailure

    raw_attributes = response.get("UserAttributes")
    if not isinstance(raw_attributes, list):
        raise _UserServiceFailure

    attributes = {}
    for attribute in raw_attributes:
        if not isinstance(attribute, dict):
            raise _UserServiceFailure
        name = attribute.get("Name")
        value = attribute.get("Value")
        if not isinstance(name, str) or not isinstance(value, str):
            raise _UserServiceFailure
        if name in attributes:
            raise _UserServiceFailure
        attributes[name] = value

    return enabled, attributes


def _profile_rollback_attributes(attributes, fields):
    names = []
    for field in ("name", "email", "phone"):
        if field not in fields:
            continue

        attribute_name = _PROFILE_TO_COGNITO_ATTRIBUTE[field]
        if attribute_name not in attributes:
            raise _UserConflict
        names.append(attribute_name)

        verification_name = _VERIFICATION_ATTRIBUTES.get(field)
        if verification_name in attributes:
            names.append(verification_name)

    return [
        {"Name": name, "Value": attributes[name]}
        for name in names
    ]


def _is_super_user(caller_groups):
    return "super_user" in caller_groups


def _authorize_invite(caller_groups, group):
    if group != "staff_user" and not _is_super_user(caller_groups):
        raise _ForbiddenAction


def _authorize_target(
    caller_sub,
    caller_groups,
    target,
    *,
    action,
):
    target_sub = target.get("cognitoSub")
    if action in {"deactivate", "reactivate", "delete", "group"}:
        if target_sub == caller_sub:
            raise _ForbiddenAction

    if _is_super_user(caller_groups):
        return

    if action in {"profile", "read"} and target_sub == caller_sub:
        return

    if target.get("role") != "staff":
        raise _ForbiddenAction

    # Cognito is the privilege source of truth. The User-table role is only a
    # mirror and can be stale after an interrupted group change or a manual
    # administrative edit. Group changes perform this check themselves so
    # that they can reuse the result for their convergence workflow.
    if action != "group":
        try:
            managed_groups = _list_managed_groups(target_sub)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            if action == "delete" and error_code == "UserNotFoundException":
                return
            raise

        if managed_groups != {"staff_user"}:
            raise _ForbiddenAction


def _validate_profile_location(target, updates):
    role = target.get("role")
    group = _ROLE_TO_GROUP.get(role)
    if group is None:
        raise _UserConflict

    location_id = updates.get("locationId", target.get("locationId", ""))
    if not isinstance(location_id, str):
        raise _UserConflict

    location_id = location_id.strip()
    if group == "staff_user" and not location_id:
        raise ValueError("locationId is required for staff_user")
    if group != "staff_user" and location_id:
        raise ValueError("locationId must be empty for privileged users")

    updates["locationId"] = location_id


def _cognito_attributes(profile, fields):
    return [
        {
            "Name": _PROFILE_TO_COGNITO_ATTRIBUTE[field],
            "Value": profile[field],
        }
        for field in ("name", "email", "phone")
        if field in fields
    ]


def _invite_user(event, caller_sub, caller_groups):
    requested = _invite_body(event)
    _authorize_invite(caller_groups, requested["group"])

    cognito = _get_cognito_client()
    try:
        response = cognito.admin_create_user(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=requested["email"],
            UserAttributes=[
                {"Name": "email", "Value": requested["email"]},
                {"Name": "name", "Value": requested["name"]},
                {"Name": "phone_number", "Value": requested["phone"]},
            ],
            DesiredDeliveryMediums=["EMAIL"],
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code == "CodeDeliveryFailureException":
            _rollback_created_user(requested["email"])
        raise
    except BotoCoreError:
        # A transport failure is ambiguous: Cognito might not have received
        # the create call at all. Deleting by email here could destroy a
        # pre-existing account, so only compensate after creation is known to
        # have succeeded and Cognito returned the created user.
        raise

    cognito_user = response.get("User")
    if not isinstance(cognito_user, dict):
        _rollback_created_user(requested["email"])
        raise _UserServiceFailure

    username = cognito_user.get("Username") or requested["email"]
    if not isinstance(username, str) or not username:
        _rollback_created_user(requested["email"])
        raise _UserServiceFailure

    cognito_sub = _cognito_sub(cognito_user)
    if not cognito_sub:
        _rollback_created_user(username)
        raise _UserServiceFailure

    try:
        cognito.admin_add_user_to_group(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=username,
            GroupName=requested["group"],
        )
    except (BotoCoreError, ClientError):
        _rollback_created_user(username)
        raise

    item = {
        "PK": f"USER#{cognito_sub}",
        "SK": "PROFILE",
        "cognitoSub": cognito_sub,
        "role": _GROUP_TO_ROLE[requested["group"]],
        "locationId": requested["locationId"],
        "name": requested["name"],
        "email": requested["email"],
        "phone": requested["phone"],
        "status": "active",
        "createdBy": caller_sub,
        "createdAt": _utc_now(),
    }

    try:
        _put_new_user(item)
    except (BotoCoreError, ClientError):
        _rollback_created_user(username)
        raise

    return _user_response(
        HTTPStatus.CREATED.value,
        _public_user(item),
        headers={"Location": f"/users/{cognito_sub}"},
    )


def _get_user(caller_sub, caller_groups, target_sub):
    target = _load_user(target_sub)
    if target is None:
        return _user_error(HTTPStatus.NOT_FOUND.value, "user not found")

    _authorize_target(
        caller_sub,
        caller_groups,
        target,
        action="read",
    )
    return _user_response(HTTPStatus.OK.value, _public_user(target))


def _update_profile(event, caller_sub, caller_groups, target_sub):
    updates = _profile_body(event)
    target = _load_user(target_sub)
    if target is None:
        return _user_error(HTTPStatus.NOT_FOUND.value, "user not found")

    _authorize_target(
        caller_sub,
        caller_groups,
        target,
        action="profile",
    )
    _validate_profile_location(target, updates)

    changed_fields = {
        field
        for field, value in updates.items()
        if target.get(field) != value
    }
    if not changed_fields:
        return _user_response(HTTPStatus.OK.value, _public_user(target))

    cognito_fields = changed_fields & {"name", "email", "phone"}
    rollback_attributes = None
    if cognito_fields:
        _, current_attributes = _admin_user_snapshot(target_sub)
        rollback_attributes = _profile_rollback_attributes(
            current_attributes,
            cognito_fields,
        )
        _get_cognito_client().admin_update_user_attributes(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=target_sub,
            UserAttributes=_cognito_attributes(updates, cognito_fields),
        )

    updated = {**target, **updates}
    try:
        _put_existing_user(updated, target)
    except (BotoCoreError, ClientError, _ExpectedStateConflict):
        if rollback_attributes is not None:
            _try_cognito(
                "admin_update_user_attributes",
                UserPoolId=COGNITO_USER_POOL_ID,
                Username=target_sub,
                UserAttributes=rollback_attributes,
            )
        raise

    return _user_response(HTTPStatus.OK.value, _public_user(updated))


def _set_user_status(
    caller_sub,
    caller_groups,
    target_sub,
    *,
    enabled,
):
    target = _load_user(target_sub)
    if target is None:
        return _user_error(HTTPStatus.NOT_FOUND.value, "user not found")

    action = "reactivate" if enabled else "deactivate"
    _authorize_target(
        caller_sub,
        caller_groups,
        target,
        action=action,
    )

    was_enabled, _ = _admin_user_snapshot(target_sub)
    cognito_changed = was_enabled != enabled
    if cognito_changed:
        cognito = _get_cognito_client()
        method_name = (
            "admin_enable_user" if enabled else "admin_disable_user"
        )
        getattr(cognito, method_name)(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=target_sub,
        )

    desired_status = "active" if enabled else "disabled"
    if target.get("status") == desired_status:
        return _user_response(HTTPStatus.OK.value, _public_user(target))

    updated = {**target, "status": desired_status}
    try:
        _put_existing_user(updated, target)
    except (BotoCoreError, ClientError, _ExpectedStateConflict):
        if cognito_changed:
            rollback_method = (
                "admin_enable_user" if was_enabled else "admin_disable_user"
            )
            _try_cognito(
                rollback_method,
                UserPoolId=COGNITO_USER_POOL_ID,
                Username=target_sub,
            )
        raise

    return _user_response(HTTPStatus.OK.value, _public_user(updated))


def _delete_user(caller_sub, caller_groups, target_sub):
    target = _load_user(target_sub)
    if target is None:
        return _user_error(HTTPStatus.NOT_FOUND.value, "user not found")

    _authorize_target(
        caller_sub,
        caller_groups,
        target,
        action="delete",
    )

    try:
        _get_cognito_client().admin_delete_user(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=target_sub,
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code != "UserNotFoundException":
            raise

    _delete_existing_user(target_sub, target)

    return _empty_response(HTTPStatus.NO_CONTENT.value)


def _list_managed_groups(target_sub):
    groups = set()
    request = {
        "UserPoolId": COGNITO_USER_POOL_ID,
        "Username": target_sub,
    }

    while True:
        response = _get_cognito_client().admin_list_groups_for_user(**request)
        if not isinstance(response, dict):
            raise _UserServiceFailure

        raw_groups = response.get("Groups", [])
        if not isinstance(raw_groups, list):
            raise _UserServiceFailure

        for group in raw_groups:
            if not isinstance(group, dict):
                raise _UserServiceFailure
            group_name = group.get("GroupName")
            if group_name in _MANAGED_GROUPS:
                groups.add(group_name)

        next_token = response.get("NextToken")
        if next_token is None:
            return groups
        if not isinstance(next_token, str) or not next_token:
            raise _UserServiceFailure
        request["NextToken"] = next_token


def _restore_groups(target_sub, old_groups, added_group, removed_groups):
    for group in removed_groups:
        _try_cognito(
            "admin_add_user_to_group",
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=target_sub,
            GroupName=group,
        )

    if added_group and added_group not in old_groups:
        _try_cognito(
            "admin_remove_user_from_group",
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=target_sub,
            GroupName=added_group,
        )


def _change_group(event, caller_sub, caller_groups, target_sub):
    group, location_id = _group_body(event)
    target = _load_user(target_sub)
    if target is None:
        return _user_error(HTTPStatus.NOT_FOUND.value, "user not found")

    _authorize_target(
        caller_sub,
        caller_groups,
        target,
        action="group",
    )
    if group != "staff_user" and not _is_super_user(caller_groups):
        raise _ForbiddenAction

    old_groups = _list_managed_groups(target_sub)
    if not _is_super_user(caller_groups) and old_groups != {"staff_user"}:
        raise _ForbiddenAction

    cognito = _get_cognito_client()
    added_group = None
    removed_groups = []
    try:
        if group not in old_groups:
            cognito.admin_add_user_to_group(
                UserPoolId=COGNITO_USER_POOL_ID,
                Username=target_sub,
                GroupName=group,
            )
            added_group = group

        for old_group in sorted(old_groups - {group}):
            cognito.admin_remove_user_from_group(
                UserPoolId=COGNITO_USER_POOL_ID,
                Username=target_sub,
                GroupName=old_group,
            )
            removed_groups.append(old_group)
    except (BotoCoreError, ClientError):
        _restore_groups(
            target_sub,
            old_groups,
            added_group,
            removed_groups,
        )
        raise

    updated = {
        **target,
        "role": _GROUP_TO_ROLE[group],
        "locationId": location_id,
    }
    if updated == target:
        return _user_response(HTTPStatus.OK.value, _public_user(target))

    try:
        _put_existing_user(updated, target)
    except (BotoCoreError, ClientError, _ExpectedStateConflict):
        _restore_groups(
            target_sub,
            old_groups,
            added_group,
            removed_groups,
        )
        raise

    return _user_response(HTTPStatus.OK.value, _public_user(updated))


def _client_error_response(exc):
    error_code = exc.response.get("Error", {}).get("Code")

    if error_code == "ConditionalCheckFailedException":
        return _user_error(
            HTTPStatus.CONFLICT.value,
            "user changed; retry request",
        )

    status_code, message = _COGNITO_ERROR_MAPPING.get(
        error_code,
        (
            HTTPStatus.SERVICE_UNAVAILABLE.value,
            "user service unavailable",
        ),
    )
    return _user_error(status_code, message)


def handler(event, context):
    try:
        get_claims(event)
        caller_sub = get_sub(event)
    except Unauthorized as exc:
        return _user_error(HTTPStatus.UNAUTHORIZED.value, str(exc))

    try:
        require_group(event, "owner_user", "super_user")
    except Unauthorized:
        return _user_error(HTTPStatus.FORBIDDEN.value, "forbidden")

    caller_groups = set(get_groups(event))
    request_context = event.get("requestContext") or {}
    if not isinstance(request_context, dict):
        request_context = {}
    http = request_context.get("http") or {}
    if not isinstance(http, dict):
        http = {}
    raw_method = http.get("method")
    method = raw_method.upper() if isinstance(raw_method, str) else ""
    path_parameters = event.get("pathParameters") or {}
    if not isinstance(path_parameters, dict):
        path_parameters = {}
    proxy_path = path_parameters.get("proxy", "")

    route = _match_route(proxy_path)
    if route is None:
        return _user_error(HTTPStatus.NOT_FOUND.value, "not found")

    route_name, raw_target_sub, allowed_methods = route
    if method not in allowed_methods:
        return _user_response(
            HTTPStatus.METHOD_NOT_ALLOWED.value,
            {"error": "method not allowed"},
            headers={"Allow": ", ".join(allowed_methods)},
        )

    try:
        target_sub = (
            _target_sub(raw_target_sub)
            if raw_target_sub is not None
            else None
        )

        if route_name == "invite":
            return _invite_user(event, caller_sub, caller_groups)
        if route_name == "list":
            return _list_users(caller_sub, caller_groups)
        if route_name == "user" and method == "GET":
            return _get_user(
                caller_sub,
                caller_groups,
                target_sub,
            )
        if route_name == "user" and method == "PUT":
            return _update_profile(
                event,
                caller_sub,
                caller_groups,
                target_sub,
            )
        if route_name == "user" and method == "DELETE":
            return _delete_user(caller_sub, caller_groups, target_sub)
        if route_name == "deactivate":
            return _set_user_status(
                caller_sub,
                caller_groups,
                target_sub,
                enabled=False,
            )
        if route_name == "reactivate":
            return _set_user_status(
                caller_sub,
                caller_groups,
                target_sub,
                enabled=True,
            )
        return _change_group(
            event,
            caller_sub,
            caller_groups,
            target_sub,
        )
    except ValueError as exc:
        return _user_error(HTTPStatus.BAD_REQUEST.value, str(exc))
    except _ForbiddenAction:
        return _user_error(HTTPStatus.FORBIDDEN.value, "forbidden")
    except _UserConflict:
        return _user_error(
            HTTPStatus.CONFLICT.value,
            "user record is inconsistent",
        )
    except ClientError as exc:
        return _client_error_response(exc)
    except (BotoCoreError, _UserServiceFailure):
        return _user_error(
            HTTPStatus.SERVICE_UNAVAILABLE.value,
            "user service unavailable",
        )

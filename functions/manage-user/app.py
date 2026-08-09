"""manage-user

TRIGGER:
    API Gateway -- ANY /users/{proxy+} -- Auth: JWT

PURPOSE:
    Full staff lifecycle management - invite/create, update,
    deactivate/reactivate, remove, and assign/change group
    (staff/owner_user/super_user). Restrict to owner_user/super_user.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    USER_TABLE_NAME -- App-side user record (role, assigned location, etc)
    COGNITO_USER_POOL_ID -- Target user pool for the Cognito admin calls below

AWS RESOURCE ACCESS:
    Full dynamodb:* on the User table. Cognito Admin* actions scoped to the
    user pool: AdminCreateUser, AdminDeleteUser, AdminDisableUser,
    AdminEnableUser, AdminUpdateUserAttributes, AdminAddUserToGroup,
    AdminRemoveUserFromGroup, AdminGetUser, AdminListGroupsForUser.

NOTES:
    Creating a staff member is two Cognito calls: AdminCreateUser, then
    AdminAddUserToGroup.

Full details: docs/LAMBDA_REFERENCE.md
"""

import os
from shared.auth import Unauthorized, get_claims
from shared.responses import error_response, json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
USER_TABLE_NAME = os.environ["USER_TABLE_NAME"]
COGNITO_USER_POOL_ID = os.environ["COGNITO_USER_POOL_ID"]


def handler(event, context):
    try:
        claims = get_claims(event)
    except Unauthorized as exc:
        return error_response(401, str(exc))

    method = event["requestContext"]["http"]["method"]
    # {proxy+} match - whatever came after the fixed part of the route.
    proxy_path = (event.get("pathParameters") or {}).get("proxy", "")

    # TODO: implement manage-user - dispatch (method, proxy_path) to the right
    # internal handler, e.g.:
    #   if method == "POST" and proxy_path == "items":
    #       return _create_item(event, claims)

    return error_response(501, "not implemented")

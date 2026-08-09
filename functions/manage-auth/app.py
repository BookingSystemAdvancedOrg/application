"""manage-auth

TRIGGER:
    API Gateway -- ANY /auth/{proxy+} -- Auth: NONE

PURPOSE:
    Handles the staff login flow itself (sign-in, challenge responses, token
    refresh). Necessarily NONE-auth - you can't require a valid JWT to obtain
    one. Only staff/owner/super_user accounts exist in Cognito; customers
    never authenticate.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    COGNITO_USER_POOL_ID -- Target user pool for auth calls
    COGNITO_CLIENT_ID -- App client ID for InitiateAuth/RespondToAuthChallenge

AWS RESOURCE ACCESS:
    Cognito InitiateAuth and RespondToAuthChallenge only, scoped to the user
    pool. No DynamoDB access.

Full details: docs/LAMBDA_REFERENCE.md
"""

import os
from shared.responses import error_response, json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
COGNITO_USER_POOL_ID = os.environ["COGNITO_USER_POOL_ID"]
COGNITO_CLIENT_ID = os.environ["COGNITO_CLIENT_ID"]


def handler(event, context):
    method = event["requestContext"]["http"]["method"]
    proxy_path = (event.get("pathParameters") or {}).get("proxy", "")

    # TODO: implement manage-auth - dispatch (method, proxy_path) internally.

    return error_response(501, "not implemented")

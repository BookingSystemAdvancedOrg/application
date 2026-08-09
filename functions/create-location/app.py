"""create-location

TRIGGER:
    API Gateway -- POST /locations -- Auth: JWT

PURPOSE:
    Creates a new restaurant location record (name, address, business hours,
    etc). Restrict to owner_user/super_user - regular staff shouldn't be able
    to create locations.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    LOCATION_TABLE_NAME -- DynamoDB table to write the new location item to

AWS RESOURCE ACCESS:
    Full dynamodb:* on the Location table only.

Full details: docs/LAMBDA_REFERENCE.md
"""

import os
from shared.auth import Unauthorized, get_claims
from shared.responses import error_response, json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
LOCATION_TABLE_NAME = os.environ["LOCATION_TABLE_NAME"]


def handler(event, context):
    try:
        claims = get_claims(event)
    except Unauthorized as exc:
        return error_response(401, str(exc))

    path_params = event.get("pathParameters") or {}

    # TODO: implement create-location.
    # See the module docstring above (and docs/LAMBDA_REFERENCE.md) for
    # what this needs to do and which group(s) should be allowed to call
    # it, e.g.:
    #   require_group(event, "owner_user", "super_user")

    return error_response(501, "not implemented")

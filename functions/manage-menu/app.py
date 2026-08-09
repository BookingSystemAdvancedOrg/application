"""manage-menu

TRIGGER:
    API Gateway -- ANY /locations/{locationId}/menu/{proxy+} -- Auth: JWT

PURPOSE:
    Staff-facing CRUD for menu items. The {proxy+} catch-all means this one
    function handles every sub-path under /menu/... and every HTTP method -
    dispatch internally on method + remaining path/payload.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    MENU_TABLE_NAME -- DynamoDB table to read/write

AWS RESOURCE ACCESS:
    Full dynamodb:* on the Menu table.

Full details: docs/LAMBDA_REFERENCE.md
"""

import os
from shared.auth import Unauthorized, get_claims
from shared.responses import error_response, json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
MENU_TABLE_NAME = os.environ["MENU_TABLE_NAME"]


def handler(event, context):
    try:
        claims = get_claims(event)
    except Unauthorized as exc:
        return error_response(401, str(exc))

    method = event["requestContext"]["http"]["method"]
    # {proxy+} match - whatever came after the fixed part of the route.
    proxy_path = (event.get("pathParameters") or {}).get("proxy", "")

    # TODO: implement manage-menu - dispatch (method, proxy_path) to the right
    # internal handler, e.g.:
    #   if method == "POST" and proxy_path == "items":
    #       return _create_item(event, claims)

    return error_response(501, "not implemented")

"""manage-layout-element

TRIGGER:
    API Gateway -- ANY /locations/{locationId}/layout-elements/{proxy+} --
    Auth: JWT

PURPOSE:
    CRUD on individual floor-plan elements (tables, walls, decor) in the
    live/draft layout. Same {proxy+}/ANY dispatch pattern as manage-menu.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    LIVE_LAYOUT_ELEMENT_TABLE_NAME -- DynamoDB table to read/write

AWS RESOURCE ACCESS:
    Full dynamodb:* on the Live Layout Element table.

Full details: docs/LAMBDA_REFERENCE.md
"""

import os
from shared.auth import Unauthorized, get_claims
from shared.responses import error_response, json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
LIVE_LAYOUT_ELEMENT_TABLE_NAME = os.environ["LIVE_LAYOUT_ELEMENT_TABLE_NAME"]


def handler(event, context):
    try:
        claims = get_claims(event)
    except Unauthorized as exc:
        return error_response(401, str(exc))

    method = event["requestContext"]["http"]["method"]
    # {proxy+} match - whatever came after the fixed part of the route.
    proxy_path = (event.get("pathParameters") or {}).get("proxy", "")

    # TODO: implement manage-layout-element - dispatch (method, proxy_path) to the right
    # internal handler, e.g.:
    #   if method == "POST" and proxy_path == "items":
    #       return _create_item(event, claims)

    return error_response(501, "not implemented")

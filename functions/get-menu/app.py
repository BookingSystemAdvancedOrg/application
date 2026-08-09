"""get-menu

TRIGGER:
    API Gateway -- GET /locations/{locationId}/menu -- Auth: NONE

PURPOSE:
    Public, unauthenticated menu read for the customer-facing site - returns
    the menu items for a given location.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    MENU_TABLE_NAME -- DynamoDB table to read from

AWS RESOURCE ACCESS:
    Read-only (Scan, GetItem, Query) on the Menu table.

Full details: docs/LAMBDA_REFERENCE.md
"""

import os
from shared.responses import error_response, json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
MENU_TABLE_NAME = os.environ["MENU_TABLE_NAME"]


def handler(event, context):
    path_params = event.get("pathParameters") or {}
    query_params = event.get("queryStringParameters") or {}

    # TODO: implement get-menu.
    # See the module docstring above (and docs/LAMBDA_REFERENCE.md).

    return error_response(501, "not implemented")

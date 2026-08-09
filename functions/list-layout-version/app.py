"""list-layout-version

API GW GET /locations/{locationId}/layout/versions (JWT). Lists published
layout versions for a location. Full spec: docs/LAMBDA_REFERENCE.md #13.
"""

import os

from shared.auth import Unauthorized, get_claims
from shared.responses import error_response, json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME = os.environ["PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME"]


def handler(event, context):
    try:
        claims = get_claims(event)
    except Unauthorized as exc:
        return error_response(401, str(exc))

    path_params = event.get("pathParameters") or {}

    # TODO: implement list-layout-version. See docs/LAMBDA_REFERENCE.md #13, e.g.:
    #   require_group(event, "owner_user", "super_user")

    return error_response(501, "not implemented")

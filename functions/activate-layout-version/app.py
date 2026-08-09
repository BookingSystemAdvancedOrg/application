"""activate-layout-version

API GW POST /locations/{locationId}/layout/versions/{versionId}/activate (JWT).
Delayed-cutover activation of a published layout version. Full spec incl.
the transact-write/scheduler sequence: docs/LAMBDA_REFERENCE.md #14.
"""

import os

from shared.auth import Unauthorized, get_claims
from shared.responses import error_response, json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME = os.environ["PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME"]
SCHEDULER_INVOKE_ROLE_ARN = os.environ["SCHEDULER_INVOKE_ROLE_ARN"]
EXPIRE_LAYOUT_VERSION_FUNCTION_ARN = os.environ["EXPIRE_LAYOUT_VERSION_FUNCTION_ARN"]


def handler(event, context):
    try:
        claims = get_claims(event)
    except Unauthorized as exc:
        return error_response(401, str(exc))

    path_params = event.get("pathParameters") or {}

    # TODO: implement activate-layout-version. See docs/LAMBDA_REFERENCE.md
    # #14 for the full sequence (version validation, cutover calc,
    # TransactWriteItems, schedule cancel/create), e.g.:
    #   require_group(event, "owner_user", "super_user")

    return error_response(501, "not implemented")

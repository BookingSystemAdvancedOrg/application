"""publish-layout

API GW POST /locations/{locationId}/layout/publish (JWT). Snapshots the
live layout as a new immutable Published Layout Snapshot version. Does not
activate it. Full spec: docs/LAMBDA_REFERENCE.md #12.
"""

import os

from shared.auth import Unauthorized, get_claims
from shared.responses import error_response, json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
LIVE_LAYOUT_ELEMENT_TABLE_NAME = os.environ["LIVE_LAYOUT_ELEMENT_TABLE_NAME"]
PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME = os.environ["PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME"]


def handler(event, context):
    try:
        claims = get_claims(event)
    except Unauthorized as exc:
        return error_response(401, str(exc))

    path_params = event.get("pathParameters") or {}

    # TODO: implement publish-layout. See docs/LAMBDA_REFERENCE.md #12, e.g.:
    #   require_group(event, "owner_user", "super_user")

    return error_response(501, "not implemented")

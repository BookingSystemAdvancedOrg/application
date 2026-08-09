"""pre-signed-url

TRIGGER:
    API Gateway -- GET /menu-images/presigned-url -- Auth: JWT

PURPOSE:
    Generates a presigned S3 PUT URL so the admin front-end can upload a menu
    item image directly to S3. After a successful replace, should also
    invalidate the relevant CloudFront distribution's cache for that object
    path.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    MENU_IMAGES_BUCKET_NAME -- S3 bucket to generate the presigned URL against

AWS RESOURCE ACCESS:
    Full s3:* on the Menu Images bucket (bucket + objects).
    cloudfront:CreateInvalidation, currently scoped to Resource:'*' (both
    distributions) - a known-loose grant flagged for tightening later, not
    something to work around here.

Full details: docs/LAMBDA_REFERENCE.md
"""

import os
from shared.auth import Unauthorized, get_claims
from shared.responses import error_response, json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
MENU_IMAGES_BUCKET_NAME = os.environ["MENU_IMAGES_BUCKET_NAME"]


def handler(event, context):
    try:
        claims = get_claims(event)
    except Unauthorized as exc:
        return error_response(401, str(exc))

    path_params = event.get("pathParameters") or {}

    # TODO: implement pre-signed-url.
    # See the module docstring above (and docs/LAMBDA_REFERENCE.md) for
    # what this needs to do and which group(s) should be allowed to call
    # it, e.g.:
    #   require_group(event, "owner_user", "super_user")

    return error_response(501, "not implemented")

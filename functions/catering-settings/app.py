"""catering-settings

TRIGGER:
    API Gateway
    GET /locations/{locationId}/catering/settings   -- Auth: NONE
        What the guest's catering calculator needs: on/off, portion limits,
        lead time, pickup/delivery and delivery prices, terms.
    PUT /locations/{locationId}/catering/settings   -- Auth: JWT (owner)
        Saves the location's `cateringSettings` (fields merged over the
        current ones; shared/catering.py validates). Answers the full
        settings, including the owner's notification contacts.

Multi-tenant: the location decides the tenant on the public route; the
token's tenant must own it on the owner route. Tenants without the
"catering" feature get 404 / 403.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME
"""

import os

from botocore.exceptions import BotoCoreError, ClientError

from shared import catering as c
from shared import dynamo, http, tenant
from shared.reservations import normalize_phone

_PATH = "/locations/{locationId}/catering/settings"


def _get(event):
    location_id = http.path_id(event, "locationId")
    ctx = tenant.for_public(location_id, feature="catering")
    return http.respond(200, c.public_settings(c.settings_of(ctx.location)))


def _put(event):
    location_id = http.path_id(event, "locationId")
    ctx = tenant.for_jwt(event, location_id=location_id, feature="catering", owner_only=True)
    data = http.body(event)
    current = ctx.location.get("cateringSettings")
    merged = {**(current if isinstance(current, dict) else {}), **data}
    settings = c.validate_settings(merged, phone_normalizer=normalize_phone)
    dynamo.table(os.environ["LOCATION_TABLE_NAME"]).update_item(
        Key=ctx.location_key,
        UpdateExpression="SET cateringSettings = :s",
        ConditionExpression="attribute_exists(PK)",
        ExpressionAttributeValues={":s": settings},
    )
    return http.respond(200, settings)


def handler(event, context):
    method, path = http.route(event)
    if path != _PATH:
        return http.error(404, "not found")
    try:
        if method == "GET":
            return _get(event)
        if method == "PUT":
            return _put(event)
        return http.respond(405, {"error": "method not allowed"}, {"Allow": "GET, PUT"})
    except tenant.TenantError as exc:
        return exc.response()
    except ValueError as exc:
        return http.error(400, str(exc))
    except (BotoCoreError, ClientError):
        return http.error(503, "catering service unavailable")

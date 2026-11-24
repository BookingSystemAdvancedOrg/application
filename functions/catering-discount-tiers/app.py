"""catering-discount-tiers

TRIGGER:
    API Gateway
    GET    /locations/{locationId}/catering/discount-tiers           -- Auth: NONE
    POST   /locations/{locationId}/catering/discount-tiers           -- Auth: JWT (owner)
    PUT    /locations/{locationId}/catering/discount-tiers/{tierId}  -- Auth: JWT (owner)
    DELETE /locations/{locationId}/catering/discount-tiers/{tierId}  -- Auth: JWT (owner)

    Booqy Console dev twin seeder (dev only, prod answers 400): {"booqyConsole": ...}
    becomes the owner's API Gateway event, seed routes only (shared/console_invoke.py dev_twin_entry).

PURPOSE:
    The restaurant's volume discounts for catering: a tier gives
    `discountPercent` off the dishes when the TOTAL number of portions is
    between fromPortions and toPortions (null = and up). The best matching
    active tier applies (shared/catering.py). The public GET lists active
    and inactive tiers (only active ones apply to a price).

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    CATERING_DISCOUNT_TIERS_TABLE_NAME
"""

from botocore.exceptions import BotoCoreError, ClientError

from shared import catering as c
from shared import console_invoke, dynamo, http, tenant

_COLLECTION = "/locations/{locationId}/catering/discount-tiers"
_ITEM = "/locations/{locationId}/catering/discount-tiers/{tierId}"
MAX_TIERS = 20


def _key(location_id, tier_id):
    return {"PK": c.location_pk(location_id), "SK": f"TIER#{tier_id}"}


def _owner(event):
    location_id = http.path_id(event, "locationId")
    tenant.for_jwt(event, location_id=location_id, feature="catering", owner_only=True)
    return location_id


# Booqy Console's dev twin seeder may call this as the twin's owner (dev only,
# shared/console_invoke.py), and only for the routes it seeds with; any other
# console call (refunds, resends, messages to guests) is refused. Everything
# else is API Gateway, hooks and schedules as before.
TWIN_ROUTES = frozenset({
    "GET /locations/{locationId}/catering/discount-tiers",
    "POST /locations/{locationId}/catering/discount-tiers",
})


@console_invoke.dev_twin_entry(routes=TWIN_ROUTES)
def handler(event, context):
    method, path = http.route(event)
    try:
        if path == _COLLECTION and method == "GET":
            location_id = http.path_id(event, "locationId")
            tenant.for_public(location_id, feature="catering")
            # All tiers (the owner's page edits inactive ones too); only
            # active ones ever apply to a price.
            return http.respond(200, {"tiers": c.list_tiers(location_id)})
        if path == _COLLECTION and method == "POST":
            location_id = _owner(event)
            tier = c.validate_tier(http.body(event))
            if len(c.list_tiers(location_id)) >= MAX_TIERS:
                raise ValueError(f"at most {MAX_TIERS} tiers")
            tier_id = c.new_id()
            dynamo.table(c.tiers_table()).put_item(
                Item={**_key(location_id, tier_id), **tier},
                ConditionExpression="attribute_not_exists(PK)")
            return http.respond(201, {"tierId": tier_id, **tier})
        if path == _ITEM and method in ("PUT", "DELETE"):
            location_id = _owner(event)
            tier_id = http.path_id(event, "tierId")
            if not c.valid_id(tier_id):
                return http.error(404, "not found")
            table = dynamo.table(c.tiers_table())
            if method == "DELETE":
                table.delete_item(Key=_key(location_id, tier_id))
                return http.respond(204, {})
            tier = c.validate_tier(http.body(event))
            try:
                table.put_item(Item={**_key(location_id, tier_id), **tier},
                               ConditionExpression="attribute_exists(PK)")
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    return http.error(404, "not found")
                raise
            return http.respond(200, {"tierId": tier_id, **tier})
        if path in (_COLLECTION, _ITEM):
            return http.respond(405, {"error": "method not allowed"})
        return http.error(404, "not found")
    except tenant.TenantError as exc:
        return exc.response()
    except ValueError as exc:
        return http.error(400, str(exc))
    except (BotoCoreError, ClientError):
        return http.error(503, "catering service unavailable")

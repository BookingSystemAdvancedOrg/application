"""catering-settings

TRIGGER:
    API Gateway
    GET /locations/{locationId}/catering/settings   -- Auth: NONE
        What the guest's catering calculator needs: on/off, portion limits,
        lead time, pickup/delivery and delivery prices, terms.
    PUT /locations/{locationId}/catering/settings   -- Auth: JWT (owner)
        Saves the location's `cateringSettings` (fields merged over the
        current ones; shared/catering.py validates). Answers the full
        settings, including the owner's notification contacts, and
        `originFound`: whether the address deliveries start from (originAddress,
        else the location's address) can be located for road distances
        (true / false; null when delivery is off or the map service is down).
    GET    /locations/{locationId}/catering/menu                         -- Auth: NONE
        The guest's catering menu (guest fields only): active dishes and
        active packages whose dishes are all active. 404 while catering is off.
    GET    /locations/{locationId}/catering/menu/dishes                  -- Auth: JWT (owner)
    POST   /locations/{locationId}/catering/menu/dishes                  -- Auth: JWT (owner)
    PUT    /locations/{locationId}/catering/menu/dishes/{dishId}         -- Auth: JWT (owner)
    DELETE /locations/{locationId}/catering/menu/dishes/{dishId}         -- Auth: JWT (owner)
    GET    /locations/{locationId}/catering/menu/packages                -- Auth: JWT (owner)
    POST   /locations/{locationId}/catering/menu/packages                -- Auth: JWT (owner)
    PUT    /locations/{locationId}/catering/menu/packages/{packageId}    -- Auth: JWT (owner)
    DELETE /locations/{locationId}/catering/menu/packages/{packageId}    -- Auth: JWT (owner)
        The owner's catering menu (catering-menu table), separate from the
        regular menu. PUT needs the `version` it read (409 changed_retry when
        someone saved first). A dish in a package can't be deleted (409
        dish_in_package).

Multi-tenant: the location decides the tenant on the public route; the
token's tenant must own it on the owner route. Tenants without the
"catering" feature get 404 / 403.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    CATERING_MENU_TABLE_NAME

AWS RESOURCE ACCESS:
    location table: GetItem, Query, UpdateItem (cateringSettings only)
    catering-menu table: GetItem, Query, PutItem, DeleteItem
    Amazon Location: geo-places:Geocode (the delivery origin)
"""

import os

from botocore.exceptions import BotoCoreError, ClientError

from shared import catering as c
from shared import dynamo, geo, http, tenant
from shared.reservations import normalize_phone

_PATH = "/locations/{locationId}/catering/settings"
_MENU = "/locations/{locationId}/catering/menu"
# collection path -> (item path, SK prefix, id field, list key, limit)
_KINDS = {
    f"{_MENU}/dishes": (f"{_MENU}/dishes/{{dishId}}", c.DISH, "dishId", "dishes", c.MAX_DISHES),
    f"{_MENU}/packages": (f"{_MENU}/packages/{{packageId}}", c.PACKAGE, "packageId", "packages", c.MAX_PACKAGES),
}


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
    return http.respond(200, {**settings, "originFound": _origin_found(settings, ctx.location)})


def _origin_found(settings, location):
    if not settings["delivery"]:
        return None
    origin = settings["originAddress"] or (location or {}).get("address")
    if not origin:
        return False
    try:
        return geo.position(origin) is not None
    except (BotoCoreError, ClientError):
        return None


def _validate(prefix, body, menu):
    if prefix == c.DISH:
        return c.validate_dish(body)
    return c.validate_package(body, {d["dishId"] for d in menu["dishes"]})


def _menu(event, method, path):
    location_id = http.path_id(event, "locationId")
    if path == _MENU:
        if method != "GET":
            return http.respond(405, {"error": "method not allowed"}, {"Allow": "GET"})
        ctx = tenant.for_public(location_id, feature="catering")
        if not c.settings_of(ctx.location)["enabled"]:
            return http.error(404, "not found")  # catering switched off: nothing to show the guest
        return http.respond(200, c.list_catering_menu(location_id, active_only=True))
    collection = next(k for k, v in _KINDS.items() if path in (k, v[0]))
    item_path, prefix, id_field, list_key, limit = _KINDS[collection]
    tenant.for_jwt(event, location_id=location_id, feature="catering", owner_only=True)
    table = dynamo.table(c.menu_table())
    menu = c.list_catering_menu(location_id)
    if path == collection:
        if method == "GET":
            return http.respond(200, {list_key: menu[list_key]})
        if method != "POST":
            return http.respond(405, {"error": "method not allowed"}, {"Allow": "GET, POST"})
        item = _validate(prefix, http.body(event), menu)
        # shortcut: count read before the write, so two parallel POSTs can pass the limit by one;
        # fine for one owner per location, use a counter item if writers multiply.
        if len(menu[list_key]) >= limit:
            raise ValueError(f"at most {limit} {list_key}")
        item_id = c.new_id()
        item["version"] = 1
        table.put_item(Item={**c.menu_key(location_id, prefix, item_id), **item},
                       ConditionExpression="attribute_not_exists(PK)")
        return http.respond(201, {id_field: item_id, **item})
    item_id = http.path_id(event, id_field)
    if not c.valid_id(item_id):
        return http.error(404, "not found")
    if method == "DELETE":
        # shortcut: a package saved with this dish between the check and the delete keeps a
        # dangling id (guests then don't see that package; the owner re-saves it); fine for one
        # owner per location, move to a transaction with a package version item if that changes.
        if prefix == c.DISH and any(item_id in p.get("dishIds", []) for p in menu["packages"]):
            return http.error(409, "dish_in_package")
        table.delete_item(Key=c.menu_key(location_id, prefix, item_id))
        return http.respond(204, {})
    if method != "PUT":
        return http.respond(405, {"error": "method not allowed"}, {"Allow": "PUT, DELETE"})
    body = http.body(event)
    version = c.whole(body.get("version"), 1, 10**9, "version")
    item = _validate(prefix, body, menu)
    item["version"] = version + 1
    try:
        table.put_item(Item={**c.menu_key(location_id, prefix, item_id), **item},
                       ConditionExpression="attribute_exists(PK) AND #v = :v",
                       ExpressionAttributeNames={"#v": "version"}, ExpressionAttributeValues={":v": version})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        exists = any(x[id_field] == item_id for x in menu[list_key])
        return http.error(409, "changed_retry") if exists else http.error(404, "not found")
    return http.respond(200, {id_field: item_id, **item})


def handler(event, context):
    method, path = http.route(event)
    if path != _PATH and path != _MENU and not any(path in (k, v[0]) for k, v in _KINDS.items()):
        return http.error(404, "not found")
    try:
        if path != _PATH:
            return _menu(event, method, path)
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

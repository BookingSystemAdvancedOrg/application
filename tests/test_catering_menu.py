"""Catering menu (DISH# / PACKAGE# in catering-menu), the new catering
settings (guests, lead time, delivery bands, reminders) and the one-off
migration from the regular menu. Fixtures from test_catering."""

import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import pytest

from shared import catering as c
from tenant_support import LOC_A, LOC_A2, TENANT_A, TENANT_B, with_tenant_claims
from test_catering import CATERING_MENU_TABLE, LOCATION_TABLE, MENU_TABLE, SETTINGS, body_of, env  # noqa: F401

MENU = "/locations/{locationId}/catering/menu"


def call(env, method, path, body=None, *, role="owner_user", tenant_id=None, params=None, public=False):
    event = {"routeKey": f"{method} {MENU}{path}", "pathParameters": {"locationId": LOC_A, **(params or {})}}
    if body is not None:
        event["body"] = json.dumps(body)
    if not public:
        event = with_tenant_claims(event, tenant_id, role) if tenant_id else with_tenant_claims(event, role=role)
    return env.m["settings"].handler(event, None)


def dish(env, **extra):
    data = {"name": "Laxtallrik", "pricePerPortion": 145, "allergens": ["fish", "milk"], "dietary": [], **extra}
    response = call(env, "POST", "/dishes", data)
    assert response["statusCode"] == 201, response
    return body_of(response)


# --- dishes and packages -----------------------------------------------------------------------

def test_dish_crud_with_version(env):
    created = dish(env, description="Med dillsås", allergens=["milk", "fish"], dietary=["lactose_free"])
    assert created["version"] == 1 and created["allergens"] == ["fish", "milk"] and created["active"] is True
    dish_id = created["dishId"]
    listed = body_of(call(env, "GET", "/dishes"))["dishes"]
    assert [d["dishId"] for d in listed] == [dish_id] and listed[0]["pricePerPortion"] == 145

    edit = {"name": "Lax", "pricePerPortion": 149.5, "version": 1, "active": False}
    put = call(env, "PUT", "/dishes/{dishId}", edit, params={"dishId": dish_id})
    assert put["statusCode"] == 200 and body_of(put)["version"] == 2
    # a stale version loses
    stale = call(env, "PUT", "/dishes/{dishId}", edit, params={"dishId": dish_id})
    assert stale["statusCode"] == 409 and body_of(stale)["error"] == "changed_retry"
    missing = call(env, "PUT", "/dishes/{dishId}", edit, params={"dishId": "f" * 32})
    assert missing["statusCode"] == 404
    assert call(env, "DELETE", "/dishes/{dishId}", params={"dishId": dish_id})["statusCode"] == 204
    assert body_of(call(env, "GET", "/dishes"))["dishes"] == []


@pytest.mark.parametrize("bad", [
    {"name": "", "pricePerPortion": 100},
    {"name": "X", "pricePerPortion": -1},
    {"name": "X", "pricePerPortion": "12.345"},
    {"name": "X", "pricePerPortion": 10001},
    {"name": "X", "pricePerPortion": True},
    {"name": "X", "pricePerPortion": 10, "allergens": ["shellfish"]},
    {"name": "X", "pricePerPortion": 10, "dietary": "vegan"},
    {"name": "X", "pricePerPortion": 10, "price": 10},
])
def test_dish_validation(env, bad):
    assert call(env, "POST", "/dishes", bad)["statusCode"] == 400


def test_packages_reference_dishes(env):
    a, b = dish(env), dish(env, name="Veg", pricePerPortion=120, dietary=["vegetarian"])
    bad = call(env, "POST", "/packages", {"name": "Fest", "pricePerGuest": 250, "dishIds": ["f" * 32]})
    assert bad["statusCode"] == 400
    created = call(env, "POST", "/packages", {"name": "Fest", "pricePerGuest": 250, "dishIds": [a["dishId"], b["dishId"]],
                                              "minGuests": 20})
    assert created["statusCode"] == 201
    package = body_of(created)
    assert package["minGuests"] == 20 and package["version"] == 1
    # a dish in a package can't go
    blocked = call(env, "DELETE", "/dishes/{dishId}", params={"dishId": a["dishId"]})
    assert blocked["statusCode"] == 409 and body_of(blocked)["error"] == "dish_in_package"
    put = call(env, "PUT", "/packages/{packageId}", {"name": "Fest", "pricePerGuest": 260, "dishIds": [b["dishId"]],
                                                    "version": 1}, params={"packageId": package["packageId"]})
    assert body_of(put)["dishIds"] == [b["dishId"]] and body_of(put)["version"] == 2
    assert call(env, "DELETE", "/dishes/{dishId}", params={"dishId": a["dishId"]})["statusCode"] == 204
    assert call(env, "DELETE", "/packages/{packageId}", params={"packageId": package["packageId"]})["statusCode"] == 204


def test_public_menu_shows_only_what_a_guest_may_order(env):
    on, off = dish(env, sort=2), dish(env, name="Gammal", active=False)
    late = dish(env, name="Ägg", sort=1)
    call(env, "POST", "/packages", {"name": "Bra", "pricePerGuest": 200, "dishIds": [on["dishId"]]})
    call(env, "POST", "/packages", {"name": "Har inaktiv rätt", "pricePerGuest": 200, "dishIds": [off["dishId"]]})
    call(env, "POST", "/packages", {"name": "Av", "pricePerGuest": 200, "dishIds": [on["dishId"]], "active": False})
    public = body_of(call(env, "GET", "", public=True))
    assert [d["dishId"] for d in public["dishes"]] == [late["dishId"], on["dishId"]]
    assert [p["name"] for p in public["packages"]] == ["Bra"]
    assert set(public["dishes"][0]) == {"dishId", "name", "description", "pricePerPortion", "allergens", "dietary"}
    assert set(public["packages"][0]) == {"packageId", "name", "description", "pricePerGuest", "dishIds"}
    assert len(body_of(call(env, "GET", "/packages"))["packages"]) == 3


def test_public_menu_is_hidden_while_catering_is_off(env):
    dish(env)
    env.res.Table(LOCATION_TABLE).update_item(Key={"PK": f"TENANT#{TENANT_A}", "SK": f"LOCATION#{LOC_A}"},
                                              UpdateExpression="SET cateringSettings.enabled = :f",
                                              ExpressionAttributeValues={":f": False})
    assert call(env, "GET", "", public=True)["statusCode"] == 404
    assert call(env, "GET", "/dishes")["statusCode"] == 200  # the owner still edits it


def test_owner_only_and_tenant_isolation(env):
    created = dish(env)
    assert call(env, "GET", "/dishes", role="staff_user")["statusCode"] == 403
    assert call(env, "POST", "/dishes", {"name": "X", "pricePerPortion": 1}, role="staff_user")["statusCode"] == 403
    other = call(env, "DELETE", "/dishes/{dishId}", tenant_id=TENANT_B, params={"dishId": created["dishId"]})
    assert other["statusCode"] == 404
    assert call(env, "GET", "/dishes", public=True)["statusCode"] in (401, 403)
    assert call(env, "POST", "", {"x": 1})["statusCode"] == 405


def test_regular_menu_is_untouched(env):
    dish(env)
    rows = env.res.Table(MENU_TABLE).scan()["Items"]
    assert all(set(r) <= {"PK", "SK", "menuItemId", "name", "description", "price", "category", "imageKey", "active",
                          "alcohol"} for r in rows)


# --- settings ---------------------------------------------------------------------------------

def test_new_settings_defaults_and_legacy_delivery_mapping():
    s = c.validate_settings({"maxDistanceKm": 25, "priceWithinLimit": 395, "priceBeyondLimit": 795,
                             "allowOutsideArea": True})
    assert s["minGuests"] == 15 and s["leadTimeRules"] == [] and s["allowSimpleAcceptance"] is False
    assert s["defaultServiceIncluded"] is True
    assert s["deliveryBands"] == [{"upToKm": 25, "price": 395}, {"upToKm": 1000, "price": 795}]
    assert s["maxDeliveryKm"] == 1000
    only = c.validate_settings({"maxDistanceKm": 30, "priceWithinLimit": 300, "allowOutsideArea": False})
    assert only["deliveryBands"] == [{"upToKm": 30, "price": 300}] and only["maxDeliveryKm"] == 30


def test_bands_drive_the_flat_prices():
    s = c.validate_settings({"deliveryBands": [{"upToKm": 10, "price": 200}, {"upToKm": 20, "price": 300},
                                               {"upToKm": 40, "price": 500}], "maxDeliveryKm": 40})
    assert (s["maxDistanceKm"], s["priceWithinLimit"], s["priceBeyondLimit"], s["allowOutsideArea"]) == (10, 200, 500, True)


@pytest.mark.parametrize("change", [
    {"deliveryBands": [{"upToKm": 20, "price": 1}, {"upToKm": 10, "price": 2}], "maxDeliveryKm": 10},
    {"deliveryBands": [{"upToKm": 10, "price": 1}, {"upToKm": 10, "price": 2}], "maxDeliveryKm": 10},
    {"deliveryBands": [{"upToKm": 10, "price": 1}], "maxDeliveryKm": 15},
    {"deliveryBands": [{"upToKm": 0, "price": 1}], "maxDeliveryKm": 0},
    {"deliveryBands": [{"upToKm": 10}], "maxDeliveryKm": 10},
    {"leadTimeRules": [{"fromGuests": 15, "toGuests": 50, "days": 3}, {"fromGuests": 40, "toGuests": None, "days": 7}]},
    {"leadTimeRules": [{"fromGuests": 15, "toGuests": 50, "days": 3}, {"fromGuests": 60, "toGuests": None, "days": 7}]},
    {"leadTimeRules": [{"fromGuests": 15, "toGuests": None, "days": 3}, {"fromGuests": 60, "toGuests": 80, "days": 7}]},
    {"leadTimeRules": [{"fromGuests": 15, "toGuests": 10, "days": 3}]},
    {"leadTimeRules": [{"fromGuests": 15, "toGuests": 20, "days": 91}]},
    {"minGuests": 0},
    {"ownerReminderHours": 72, "requestExpiryHours": 72},
    {"signReminderDaysBefore": 7, "offerValidDays": 7},
    {"allowSimpleAcceptance": "yes"},
])
def test_settings_validation_rejects(change):
    with pytest.raises(ValueError):
        c.validate_settings(change)


def test_partial_save_with_a_short_expiry_keeps_catering_on(env):
    loc = env.res.Table(LOCATION_TABLE)
    key = {"PK": f"TENANT#{TENANT_A}", "SK": f"LOCATION#{LOC_A}"}
    loc.update_item(Key=key, UpdateExpression="SET cateringSettings = :s",
                    ExpressionAttributeValues={":s": {**SETTINGS, "requestExpiryHours": 24, "offerValidDays": 2}})
    put = {"routeKey": "PUT /locations/{locationId}/catering/settings", "pathParameters": {"locationId": LOC_A},
           "body": json.dumps({"terms": "x"})}
    saved = body_of(env.m["settings"].handler(with_tenant_claims(put), None))
    assert saved["ownerReminderHours"] == 23 and saved["signReminderDaysBefore"] == 1
    stored = loc.get_item(Key=key)["Item"]
    assert c.settings_of(stored)["enabled"] is True and c.settings_of(stored)["terms"] == "x"
    # an explicit reminder past the expiry is still refused
    bad = dict(put, body=json.dumps({"ownerReminderHours": 24}))
    assert env.m["settings"].handler(with_tenant_claims(bad), None)["statusCode"] == 400


def test_older_settings_with_a_short_expiry_still_load():
    stored = {**SETTINGS, "requestExpiryHours": 12, "offerValidDays": 1}
    assert c.settings_of({"cateringSettings": stored})["enabled"] is True


def test_lead_time_rules_are_sorted_and_contiguous():
    rules = [{"fromGuests": 51, "toGuests": None, "days": 10}, {"fromGuests": 15, "toGuests": 50, "days": 5}]
    assert [r["fromGuests"] for r in c.validate_settings({"leadTimeRules": rules})["leadTimeRules"]] == [15, 51]


def test_settings_put_and_public_view(env):
    put = {"routeKey": "PUT /locations/{locationId}/catering/settings", "pathParameters": {"locationId": LOC_A},
           "body": json.dumps({"minGuests": 20, "deliveryBands": [{"upToKm": 15, "price": 250}], "maxDeliveryKm": 15,
                               "leadTimeRules": [{"fromGuests": 15, "toGuests": None, "days": 4}],
                               "ownerReminderHours": 12, "dayBeforeReminder": False})}
    saved = body_of(env.m["settings"].handler(with_tenant_claims(put), None))
    assert saved["minGuests"] == 20 and saved["priceWithinLimit"] == 250 and saved["ownerReminderHours"] == 12
    public = body_of(env.m["settings"].handler({"routeKey": "GET /locations/{locationId}/catering/settings",
                                                "pathParameters": {"locationId": LOC_A}}, None))
    assert public["deliveryBands"] == [{"upToKm": 15, "price": 250}] and public["minGuests"] == 20
    assert "ownerReminderHours" not in public and "dayBeforeReminder" not in public


# --- migration -----------------------------------------------------------------------------

def _migration():
    path = Path(__file__).parents[1] / "scripts" / "migrate_catering_menu.py"
    spec = importlib.util.spec_from_file_location("migrate_catering_menu", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_is_a_dry_run_by_default_and_idempotent(env):
    m = _migration()
    env.res.Table(LOCATION_TABLE).update_item(Key={"PK": f"TENANT#{TENANT_A}", "SK": f"LOCATION#{LOC_A2}"},
                                              UpdateExpression="SET cateringSettings.enabled = :f",
                                              ExpressionAttributeValues={":f": False})
    tables = (LOCATION_TABLE, MENU_TABLE, CATERING_MENU_TABLE)
    dry = m.run(env.res, *tables)
    # fixture menu: lax, veg (copied), vin (alcohol), old (inactive); LOC_A2 has catering off
    assert dry["locations"] == 1 and dry["written"] == 2 and dry["skipped"] == 2
    assert env.res.Table(CATERING_MENU_TABLE).scan()["Items"] == []
    assert m.run(env.res, *tables, apply=True)["written"] == 2
    again = m.run(env.res, *tables, apply=True)
    assert again["written"] == 0 and again["alreadyCopied"] == 2
    rows = {r["name"]: r for r in env.res.Table(CATERING_MENU_TABLE).scan()["Items"]}
    assert set(rows) == {"Laxtallrik", "Vegetarisk buffé"}
    lax = rows["Laxtallrik"]
    assert lax["pricePerPortion"] == Decimal("145") and lax["sourceMenuItemId"] == "lax" and lax["version"] == 1
    listed = body_of(call(env, "GET", "/dishes"))["dishes"]
    assert {d["name"] for d in listed} == {"Laxtallrik", "Vegetarisk buffé"}


def test_migration_skips_dishes_copied_in_the_admin(env):
    dish(env, name="Lax (egen)", sourceMenuItemId="lax")
    counts = _migration().run(env.res, LOCATION_TABLE, MENU_TABLE, CATERING_MENU_TABLE, apply=True)
    assert counts["written"] == 1 and counts["alreadyCopied"] == 1
    names = {r["name"] for r in env.res.Table(CATERING_MENU_TABLE).scan()["Items"]}
    assert names == {"Lax (egen)", "Vegetarisk buffé"}

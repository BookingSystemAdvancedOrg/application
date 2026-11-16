"""Menu: allergens, option groups, the location's own categories, dishes
without a photo, price validation and the stale-save check (manage-menu,
get-menu, shared/menu.py)."""

import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import pytest

from shared import menu
from test_manage_menu import (  # noqa: F401  (fixture)
    ITEM_ID, LOCATION_ID, app_and_table, get_item, make_event, menu_item, put_item, response_body, valid_body,
)

GET_MENU = Path(__file__).parents[1] / "functions" / "get-menu" / "app.py"


def _get_menu_app():
    spec = importlib.util.spec_from_file_location("get_menu_app_options", GET_MENU)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- prices ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ['"abc"', '"89,50"', '"89.50"', "-10", "1.234", "null", "true", '""', "10000.01"])
def test_bad_prices_get_400_and_nothing_is_saved(app_and_table, raw):
    app, table = app_and_table
    body = json.dumps(valid_body(price=0)).replace('"price": 0', f'"price": {raw}')
    response = app.handler(make_event(method="POST", body=body), None)
    assert response["statusCode"] == 400
    assert response_body(response)["field"] == "price"
    assert table.scan()["Items"] == []


@pytest.mark.parametrize("raw, stored", [("89.5", Decimal("89.5")), ("0", Decimal("0")), ("10000", Decimal("10000"))])
def test_good_prices_are_saved(app_and_table, raw, stored):
    app, _ = app_and_table
    body = json.dumps(valid_body(price=0)).replace('"price": 0', f'"price": {raw}')
    assert app.handler(make_event(method="POST", body=body), None)["statusCode"] == 201
    assert get_item(app_and_table[1])["price"] == stored


def test_price_kr_unit():
    assert menu.price_kr(Decimal("89.50")) == Decimal("89.5")
    assert menu.price_kr(Decimal("100.00")) == Decimal("100")
    for bad in ("12", True, Decimal("-1"), Decimal("1.001"), Decimal("NaN"), Decimal("10000.01")):
        with pytest.raises(ValueError):
            menu.price_kr(bad)


# --- photo optional -------------------------------------------------------------------

def test_dish_without_photo_is_created_and_listed(app_and_table):
    app, table = app_and_table
    body = valid_body()
    del body["imageKey"]
    response = app.handler(make_event(method="POST", body=body), None)
    assert response["statusCode"] == 201
    assert response_body(response)["imageKey"] is None
    assert "imageKey" not in get_item(table)
    public = _get_menu_app().handler(
        {"requestContext": {"http": {"method": "GET"}}, "pathParameters": {"locationId": LOCATION_ID}}, None)
    assert json.loads(public["body"])["items"][0]["imageKey"] is None


def test_photo_can_be_removed(app_and_table):
    app, table = app_and_table
    put_item(table)
    response = app.handler(make_event(method="PUT", proxy=f"items/{ITEM_ID}", body={"imageKey": None}), None)
    assert response["statusCode"] == 200
    assert "imageKey" not in get_item(table)


# --- allergens and options -------------------------------------------------------------

OPTIONS = [{"name": "Storlek", "required": True, "maxChoices": 1,
            "choices": [{"name": "Normal", "price": 0}, {"name": "Stor", "price": 30}]},
           {"name": "Extra", "choices": [{"name": "Ost", "price": 10.5}, {"name": "Bacon", "price": 15}],
            "maxChoices": 2}]


def test_allergens_and_options_are_stored_and_shown_to_guests(app_and_table):
    app, table = app_and_table
    body = valid_body(allergens=["milk", "gluten"], options=OPTIONS)
    response = app.handler(make_event(method="POST", body=body), None)
    assert response["statusCode"] == 201, response["body"]
    saved = response_body(response)
    assert saved["allergens"] == ["gluten", "milk"]  # EU order
    assert saved["options"][1] == {"name": "Extra", "required": False, "maxChoices": 2,
                                   "choices": [{"name": "Ost", "price": 10.5}, {"name": "Bacon", "price": 15}]}
    public = json.loads(_get_menu_app().handler(
        {"requestContext": {"http": {"method": "GET"}}, "pathParameters": {"locationId": LOCATION_ID}}, None)["body"])
    assert public["items"][0]["allergens"] == ["gluten", "milk"]
    assert public["items"][0]["options"][0]["choices"][1] == {"name": "Stor", "price": 30}
    cleared = app.handler(make_event(method="PUT", proxy=f"items/{ITEM_ID}",
                                     body={"allergens": [], "options": None}), None)
    assert response_body(cleared)["allergens"] == [] and response_body(cleared)["options"] == []
    assert "options" not in get_item(table) and "allergens" not in get_item(table)


@pytest.mark.parametrize("field, value", [
    ("allergens", ["peanut"]),
    ("allergens", ["milk", "milk"]),
    ("allergens", "milk"),
    ("options", [{"name": "Storlek", "choices": []}]),
    ("options", [{"name": "", "choices": [{"name": "A", "price": 0}]}]),
    ("options", [{"name": "S", "choices": [{"name": "A", "price": -5}]}]),
    ("options", [{"name": "S", "choices": [{"name": "A", "price": "5"}]}]),
    ("options", [{"name": "S", "choices": [{"name": "A", "price": 1.005}]}]),
    ("options", [{"name": "S", "maxChoices": 2, "choices": [{"name": "A", "price": 0}]}]),
    ("options", [{"name": "S", "choices": [{"name": "A", "price": 0}, {"name": "a", "price": 0}]}]),
    ("options", [{"name": "S", "choices": [{"name": "A", "price": 0}]},
                 {"name": "s", "choices": [{"name": "A", "price": 0}]}]),
    ("options", [{"name": "S", "required": "yes", "choices": [{"name": "A", "price": 0}]}]),
])
def test_bad_allergens_and_options_are_refused(app_and_table, field, value):
    app, table = app_and_table
    response = app.handler(make_event(method="POST", body=valid_body(**{field: value})), None)
    assert response["statusCode"] == 400
    assert table.scan()["Items"] == []


def test_choose_options_unit():
    item = {"options": menu.validate_options({"options": json.loads(json.dumps(OPTIONS), parse_float=Decimal,
                                                                   parse_int=Decimal)})["options"]}
    extra, chosen = menu.choose_options(item, [{"group": "Extra", "choice": "Bacon"},
                                               {"group": "Storlek", "choice": "Stor"},
                                               {"group": "Extra", "choice": "Ost"}])
    assert extra == 3000 + 1050 + 1500
    assert [c["choice"] for c in chosen] == ["Stor", "Ost", "Bacon"]
    with pytest.raises(ValueError, match="option_required"):
        menu.choose_options(item, [])
    assert menu.choose_options({}, []) == (0, [])
    with pytest.raises(ValueError, match="option_invalid"):
        menu.choose_options({}, [{"group": "Storlek", "choice": "Stor"}])


# --- stale save (two tabs) ---------------------------------------------------------------

def test_second_stale_save_gets_409_and_changes_nothing(app_and_table, monkeypatch):
    app, table = app_and_table
    monkeypatch.setattr(app, "_utc_now", lambda: "2026-11-14T12:00:00.123456Z")
    put_item(table)
    loaded = get_item(table)["updatedAt"]
    first = app.handler(make_event(method="PUT", proxy=f"items/{ITEM_ID}",
                                   body={"price": 99, "expectedUpdatedAt": loaded}), None)
    assert first["statusCode"] == 200
    second = app.handler(make_event(method="PUT", proxy=f"items/{ITEM_ID}",
                                    body={"price": 120, "expectedUpdatedAt": loaded}), None)
    assert second["statusCode"] == 409
    body = response_body(second)
    assert body["code"] == "stale_item" and body["item"]["price"] == 99
    assert get_item(table)["price"] == 99


def test_expected_updated_at_is_only_for_updates(app_and_table):
    app, _ = app_and_table
    response = app.handler(make_event(method="POST", body=valid_body(expectedUpdatedAt="x")), None)
    assert response["statusCode"] == 400


# --- categories ----------------------------------------------------------------------------

def _put_categories(app, body):
    return app.handler(make_event(method="PUT", proxy="categories", body=body), None)


def _protected_categories():
    return json.loads(_get_menu_app().handler(
        make_event(method="GET", proxy="categories"), None)["body"])


def test_defaults_until_the_owner_changes_them(app_and_table):
    assert _protected_categories() == {"categories": menu.default_categories(), "version": 0}


def test_create_rename_reorder_and_delete_categories(app_and_table):
    app, table = app_and_table
    cats = [{"categoryId": "drinks", "name": "Drycker"}, {"name": "Pizzor"}, {"categoryId": "mains", "name": "Pasta"},
            {"categoryId": "starters", "name": "Förrätter"}]
    response = _put_categories(app, {"categories": cats, "version": 0})
    assert response["statusCode"] == 200, response["body"]
    saved = response_body(response)
    pizza_id = saved["categories"][1]["categoryId"]
    assert menu.valid_category_id(pizza_id) and saved["version"] == 1
    assert [c["name"] for c in _protected_categories()["categories"]] == ["Drycker", "Pizzor", "Pasta", "Förrätter"]

    # a dish in the new category; the public menu carries the categories
    assert app.handler(make_event(method="POST", body=valid_body(category=pizza_id)), None)["statusCode"] == 201
    public = json.loads(_get_menu_app().handler(
        {"requestContext": {"http": {"method": "GET"}}, "pathParameters": {"locationId": LOCATION_ID}}, None)["body"])
    assert public["items"][0]["category"] == pizza_id
    assert public["categories"][1] == {"categoryId": pizza_id, "name": "Pizzor"}

    # deleting a category with dishes is refused
    without_pizza = [c for c in saved["categories"] if c["categoryId"] != pizza_id]
    refused = _put_categories(app, {"categories": without_pizza, "version": 1})
    assert refused["statusCode"] == 409 and response_body(refused)["code"] == "category_in_use"
    # an empty one may go (desserts was dropped above already)
    assert table.get_item(Key=menu.categories_key(LOCATION_ID))["Item"]["version"] == 1

    # a stale version is refused
    stale = _put_categories(app, {"categories": saved["categories"], "version": 0})
    assert stale["statusCode"] == 409 and response_body(stale)["code"] == "stale_categories"


def test_dish_category_must_be_the_locations(app_and_table):
    app, _ = app_and_table
    response = app.handler(make_event(method="POST", body=valid_body(category="pizzor")), None)
    assert response["statusCode"] == 400


@pytest.mark.parametrize("body", [
    {"categories": [], "version": 0},
    {"categories": [{"name": "A"}, {"name": "a"}], "version": 0},
    {"categories": [{"name": " "}], "version": 0},
    {"categories": [{"categoryId": "Bad Id", "name": "A"}], "version": 0},
    {"categories": [{"name": "A", "extra": 1}], "version": 0},
    {"categories": [{"name": "x" * 41}], "version": 0},
    {"version": 0},
])
def test_bad_category_lists_are_refused(app_and_table, body):
    app, _ = app_and_table
    assert _put_categories(app, body)["statusCode"] == 400

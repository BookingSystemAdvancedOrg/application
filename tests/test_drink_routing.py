"""Server-side drink default: a category with no routing choice of its own
goes to Dryck when its name sounds like drinks, it is the drinks category
or the dish is alcohol. Same rule and same word list as Booqy Admin
(admin-front-end src/features/printers/drinkNames.json)."""

import json
from pathlib import Path

import pytest

from shared import routing

WORDS = json.loads((Path(__file__).parent / "fixtures" / "drink_names.json").read_text())


@pytest.mark.parametrize("name", WORDS["drinks"])
def test_drink_names(name):
    assert routing.sounds_like_drinks(name) is True


@pytest.mark.parametrize("name", WORDS["food"])
def test_food_names(name):
    assert routing.sounds_like_drinks(name) is False


def test_not_a_name():
    assert routing.sounds_like_drinks(None) is False and routing.sounds_like_drinks("") is False


def _station(line, config=None):
    return list(routing.split([{"name": "x", "quantity": 1, **line}], config or routing.default_config()))


def test_unchosen_drink_category_goes_to_dryck():
    assert _station({"category": "c0a1b2c3d4", "categoryName": "Öl"}) == ["bar"]
    assert _station({"category": "c0a1b2c3d4", "categoryName": "Salladsbar"}) == ["kok"]
    assert _station({"category": "c0a1b2c3d4"}) == ["kok"]  # an order from before the name was kept
    assert _station({"category": "drinks"}) == ["bar"]
    assert _station({"category": "c0a1b2c3d4", "categoryName": "Pizza", "alcohol": True}) == ["bar"]


def test_the_owners_choice_wins():
    config = routing.default_config()
    config["categories"]["c0a1b2c3d4"] = "kok"
    assert _station({"category": "c0a1b2c3d4", "categoryName": "Vin"}, config) == ["kok"]
    config["categories"]["c0a1b2c3d4"] = "none"
    assert _station({"category": "c0a1b2c3d4", "categoryName": "Vin"}, config) == []


def test_the_dishs_own_station_wins():
    assert _station({"category": "c0a1b2c3d4", "categoryName": "Vin", "station": "kok"}) == ["kok"]


def test_without_a_dryck_station_drinks_go_to_the_first():
    config = {**routing.default_config(), "stations": [{"stationId": "kok", "name": "Kök", "printerIds": []}]}
    assert _station({"category": "c0a1b2c3d4", "categoryName": "Öl"}, config) == ["kok"]

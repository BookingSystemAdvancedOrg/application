"""Menu categories carry an optional course (shared/menu.py): when a
category is served at the table in staff table orders. A custom "Pizza"
can be a main course; the four defaults are their own course."""

import pytest

from shared import menu


def test_course_is_validated_stored_and_returned():
    cats, version = menu.validate_categories({"categories": [
        {"categoryId": "starters", "name": "Förrätter"},
        {"name": "Pizza", "course": "mains"},
        {"name": "Öl", "course": "drinks"},
        {"name": "Tillbehör", "course": None},
        {"name": "Snacks", "course": "other"}], "version": 3})
    assert version == 3
    assert "course" not in cats[0] and cats[1]["course"] == "mains" and cats[2]["course"] == "drinks"
    assert "course" not in cats[3] and cats[4]["course"] == "other"
    view = menu.categories_view({"categories": cats, "version": 4})["categories"]
    assert [c.get("course") for c in view] == [None, "mains", "drinks", None, "other"]


@pytest.mark.parametrize("course", ["Varmrätt", "main", "", 3, True, ["mains"]])
def test_unknown_course_is_refused(course):
    with pytest.raises(ValueError):
        menu.validate_categories({"categories": [{"name": "Pizza", "course": course}], "version": 0})


def test_defaults_are_their_own_course_and_custom_has_none_until_chosen():
    assert [menu.category_course(c) for c in menu.default_categories()] == ["starters", "mains", "desserts",
                                                                             "drinks"]
    assert menu.category_course({"categoryId": "c0123456789", "name": "Pizza"}) is None
    assert menu.category_course({"categoryId": "c0123456789", "name": "Pizza", "course": "mains"}) == "mains"
    # the owner may serve a default category as another course
    assert menu.category_course({"categoryId": "starters", "name": "Smårätter", "course": "mains"}) == "mains"
    assert menu.category_course(None) is None


def test_views_without_course_are_unchanged():
    assert menu.categories_view(None)["categories"] == menu.default_categories()
    stored = {"categories": [{"categoryId": "drinks", "name": "Drycker"}], "version": 1}
    assert menu.categories_view(stored)["categories"] == [{"categoryId": "drinks", "name": "Drycker"}]

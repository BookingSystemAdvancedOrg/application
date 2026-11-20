"""The table numbering the table-order API uses is admin's tablesFromLayout
(admin-front-end src/shared/tables.ts) ported: same fixture and expected
labels as admin src/shared/tables.test.ts, so staff, guests and tickets say
the same "Bord 3". The API reads a published snapshot, where floors are
elements of type floor (the public /layout/active turns them into the
``floors`` list admin numbers from)."""

from shared import table_orders as t


def el(element_id, **extra):
    return {"elementId": element_id, "type": "table", "seats": 4, **extra}


def test_numbers_tables_like_the_booking_site_floors_by_level_drawing_order():
    elements = [
        {"elementId": "upper", "type": "floor", "level": 1, "name": "Övervåning"},
        {"elementId": "ground", "type": "floor", "level": 0, "name": "Matsal"},
        el("u1", floorId="upper", seats=2),
        {"elementId": "wall", "type": "wall", "floorId": "ground"},
        el("g1", floorId="ground", seats=6),
        el("g2", floorId="ground", label="Bar"),
    ]
    assert [{k: x[k] for k in ("tableId", "label", "seats")} for x in t.tables_from_layout(elements)] == [
        {"tableId": "g1", "label": "1", "seats": 6},
        {"tableId": "g2", "label": "Bar", "seats": 4},
        {"tableId": "u1", "label": "3", "seats": 2},
    ]
    assert [x["floorName"] for x in t.tables_from_layout(elements)] == ["Matsal", "Matsal", "Övervåning"]


def test_a_flat_layout_is_one_floor():
    assert [x["label"] for x in t.tables_from_layout([el("a"), el("b")])] == ["1", "2"]


def test_blank_label_and_missing_seats_and_unknown_floor():
    elements = [{"elementId": "f", "type": "floor", "level": 0, "name": "Matsal"},
                el("a", floorId="f", label="  "), {"elementId": "b", "type": "table", "floorId": "f"},
                el("lost", floorId="gone")]
    assert [(x["tableId"], x["label"], x["seats"]) for x in t.tables_from_layout(elements)] == [
        ("a", "1", 4), ("b", "2", 0)]
    assert t.tables_from_layout(None) == []

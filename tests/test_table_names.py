"""shared/table_names.py: one naming rule for every Lambda. A table is named
by its label (own label, else its number over all floors); a raw table id is
never a name (docs/RESERVATIONS.md, "Table names")."""

from shared import table_names as n
from shared import table_orders


def el(element_id, **extra):
    return {"elementId": element_id, "type": "table", "seats": 4, **extra}


def test_table_title_per_language():
    assert n.table_title("3") == "Bord 3"
    assert n.table_title("3", "en") == "Table 3"
    assert n.table_title("Bar") == "Bar"
    assert n.table_title("Bar", "en") == "Bar"
    assert n.table_title("") == "Bord"
    assert n.table_title(None, "en") == "Table"
    assert n.table_title("3", "de") == "Bord 3"
    assert n.table_title(" 12 ") == "Bord 12"


def test_labels_by_id_numbers_across_floors_by_level_and_own_label_wins():
    elements = [
        {"elementId": "upper", "type": "floor", "level": 1, "name": "Övervåning"},
        el("u1", floorId="upper"),
        el("u2", floorId="upper"),
        {"elementId": "ground", "type": "floor", "level": 0, "name": "Matsal"},
        el("g1", floorId="ground"),
        el("g2", floorId="ground", label="Bar"),
        el("g3", floorId="ground"),
    ]
    assert n.labels_by_id(elements) == {"g1": "1", "g2": "Bar", "g3": "3", "u1": "4", "u2": "5"}


def test_labels_by_id_of_nothing():
    assert n.labels_by_id(None) == {}
    assert n.labels_by_id([]) == {}


def test_stamp_follows_table_ids_order():
    slot_tables = [{"tableId": "a", "seats": 2, "label": "1"}, {"tableId": "b", "seats": 4, "label": "Bar"}]
    assert n.stamp(["b", "a"], slot_tables) == ["Bar", "1"]
    assert n.stamp(["a", "x"], slot_tables) is None
    assert n.stamp(["a"], [{"tableId": "a", "seats": 2}]) is None


def test_stored_labels_only_when_aligned_and_complete():
    assert n.stored_labels({"tableIds": ["a", "b"], "tableLabels": [" 3", "Bar"]}) == ["3", "Bar"]
    assert n.stored_labels({"tableIds": ["a", "b"], "tableLabels": ["3"]}) is None
    assert n.stored_labels({"tableIds": ["a", "b"], "tableLabels": ["3", " "]}) is None
    assert n.stored_labels({"tableIds": ["a"], "tableLabels": [3]}) is None
    assert n.stored_labels({"tableIds": ["a"]}) is None
    assert n.stored_labels({"tableIds": ["a"], "tableLabels": "3"}) is None


def test_table_orders_reexports_the_same_functions():
    assert table_orders.tables_from_layout is n.tables_from_layout
    assert table_orders.table_title is n.table_title

"""How a table is named to a person, in every Lambda (docs/RESERVATIONS.md,
"Table names"). Pure: no AWS imports.

A table is named by its label: its own label, else its number 1, 2, 3 over
all floors of the layout version (floors by level, tables in drawing order),
the same count as admin src/shared/tables.ts tablesFromLayout and the site
kit's labelTables. A label that starts with a digit gets the word for table
("Bord 3", "Table 3"); an own name ("Bar") is shown as it is. A raw table id
is never shown to a person: where no label is known, callers use a neutral
text instead ("Ett bord", "Bord: se bokningen i admin").
"""

import re

WORD = {"sv": "Bord", "en": "Table"}


def tables_from_layout(elements):
    """The published layout's tables numbered like the booking site and
    admin: floors by level (drawing order on ties), tables in drawing order,
    numbered 1, 2, 3 ... over all floors; a table's own label wins. With no
    floors every element is one floor; with floors, a table without a known
    floor is left out (as in admin)."""
    elements = [e for e in elements or [] if isinstance(e, dict)]
    floors = sorted((e for e in elements if e.get("type") == "floor"), key=lambda f: _num(f.get("level")))
    if not floors:
        groups = [(None, None, [e for e in elements if e.get("type") != "floor"])]
    else:
        groups = [(f.get("elementId"), f.get("name"), [e for e in elements if e.get("type") != "floor"
                                                         and e.get("floorId") == f.get("elementId")])
                  for f in floors]
    out = []
    for floor_id, floor_name, members in groups:
        for el in members:
            if el.get("type") != "table":
                continue
            label = str(el.get("label") or "").strip() or str(len(out) + 1)
            out.append({"tableId": el["elementId"], "label": label, "seats": int(_num(el.get("seats"))),
                        "floorId": floor_id, "floorName": floor_name})
    return out


def _num(value):
    try:
        return float(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def labels_by_id(elements):
    """{tableId: label} for a layout's elements; {} for None."""
    if elements is None:
        return {}
    return {t["tableId"]: t["label"] for t in tables_from_layout(elements)}


def table_title(label, lang="sv"):
    """"Bord 3" ("Table 3" in English); a table with its own name ("Bar") is
    shown as it is; no label gives the bare word. An unknown language uses
    Swedish (the site translator translates it)."""
    word = WORD.get(lang, WORD["sv"])
    label = str(label or "").strip()
    if not label:
        return word
    return f"{word} {label}" if re.match(r"\d", label) else label


def stamp(table_ids, slot_tables):
    """The labels of table_ids, in that order, from a slot's tables
    ({tableId, seats, label}); None when any id has no label."""
    by_id = {}
    for t in slot_tables or []:
        if isinstance(t, dict) and t.get("tableId") is not None:
            by_id[t["tableId"]] = t.get("label")
    labels = []
    for table_id in table_ids or []:
        label = by_id.get(table_id)
        if not isinstance(label, str) or not label.strip():
            return None
        labels.append(label.strip())
    return labels


def stored_labels(item):
    """A reservation's tableLabels snapshot (stripped), only when it lines up
    with tableIds and every entry is a non-blank string; otherwise None."""
    labels = (item or {}).get("tableLabels")
    ids = (item or {}).get("tableIds")
    if not isinstance(labels, list) or not isinstance(ids, list) or len(labels) != len(ids):
        return None
    if not all(isinstance(label, str) and label.strip() for label in labels):
        return None
    return [label.strip() for label in labels]

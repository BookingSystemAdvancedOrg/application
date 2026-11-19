"""Table orders, the model without AWS: request validation, course of a
dish, pacing defaults, add / fire / edit / void (partial split) / move /
close / reopen, the reference total, views and guest matching."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from shared import table_orders as t

NOW = datetime(2026, 11, 19, 17, 0, tzinfo=timezone.utc)  # 18:00 in Stockholm
ANNA = {"sub": "sub-anna", "name": "Anna"}
TABLE = {"tableId": "t-5", "label": "5", "floorName": "Matsal"}


def line(name, course, price=10000, quantity=1, hold=None, **extra):
    return {"menuItemId": name.lower(), "name": name, "unitPrice": price, "quantity": quantity, "options": [],
            "allergy": False, "alcohol": False, "course": course, "stationId": "bar" if course == "drinks" else "kok",
            "stationName": "Bar" if course == "drinks" else "Kök", "hold": hold, **extra}


def fresh_tab():
    tab = t.new_tab(tab_id="a" * 32, tenant_id="ten", location_id="loc", table=TABLE, actor=ANNA, now=NOW,
                    opened_date="2026-11-19", covers=4, guest_name="Åsa Öberg")
    t._touch(tab, NOW)
    return tab


# --- validation ----------------------------------------------------------------------

def test_new_items_merge_identical_lines_and_keep_different_ones_apart():
    items = t.parse_new_items([
        {"menuItemId": "beer", "quantity": 1},
        {"menuItemId": "beer", "quantity": 2},
        {"menuItemId": "beer", "quantity": 1, "note": "  utan   skum "},
        {"menuItemId": "steak", "quantity": 1, "options": [{"group": "Stekgrad", "choice": "Medium"}],
         "course": "mains", "hold": True, "allergy": True},
    ])
    assert [(i["menuItemId"], i["quantity"], i["note"]) for i in items] == [
        ("beer", 3, None), ("beer", 1, "utan skum"), ("steak", 1, None)]
    assert items[2]["hold"] is True and items[2]["allergy"] is True and items[2]["course"] == "mains"


@pytest.mark.parametrize("raw", [
    [], [{"menuItemId": "x", "quantity": 0}], [{"menuItemId": "x", "quantity": 21}],
    [{"menuItemId": "x", "quantity": 1, "price": 1}], [{"menuItemId": "a#b", "quantity": 1}],
    [{"menuItemId": "x", "quantity": 1, "course": "soup"}], [{"menuItemId": "x", "quantity": 1, "hold": "yes"}],
    [{"menuItemId": "x", "quantity": 15}, {"menuItemId": "x", "quantity": 6, "note": "b"}],
    [{"menuItemId": f"d{i}", "quantity": 1} for i in range(31)],
    [{"menuItemId": "x", "quantity": 1, "note": "n" * 141}],
])
def test_new_items_refuse_bad_input(raw):
    with pytest.raises(t.TabError) as exc:
        t.parse_new_items(raw)
    assert exc.value.code == "invalid_request" and exc.value.status == 400


def test_request_id_accepts_hex_and_uuid():
    assert t.normalize_request_id("ABCDEF00-1111-2222-3333-444455556666") == "abcdef00111122223333444455556666"
    with pytest.raises(t.TabError):
        t.normalize_request_id("short")


def test_void_and_edit_parsing():
    wanted, reason, note = t.parse_void([{"itemId": "i1", "quantity": 2}], "guest_changed", " ångrade ")
    assert wanted == [("i1", 2)] and reason == "guest_changed" and note == "ångrade"
    with pytest.raises(t.TabError):
        t.parse_void([{"itemId": "i1", "quantity": 1}], "free", None)
    with pytest.raises(t.TabError):
        t.parse_held_edits([{"itemId": "i1"}, {"itemId": "i1"}])
    assert t.parse_held_edits([{"itemId": "i1", "note": None, "remove": True}]) == [
        {"itemId": "i1", "note": None, "remove": True}]


# --- course of a dish ----------------------------------------------------------------

def test_course_of_follows_category_then_station_then_drinkish_names():
    assert t.course_of({"categoryId": "c1", "name": "Pizza", "course": "mains"}, category_id="c1") == "mains"
    assert t.course_of(None, category_id="starters") == "starters"
    assert t.course_of({"categoryId": "desserts", "name": "Efterrätter"}, category_id="desserts") == "desserts"
    assert t.course_of(None, category_id="c2", station_id="bar") == "drinks"
    assert t.course_of(None, category_id="c2", alcohol=True) == "drinks"
    assert t.course_of(None, category_id="c3", category_name="Öl") == "drinks"
    assert t.course_of(None, category_id="c4", category_name="Salladsbar") == "other"
    assert t.course_of({"categoryId": "c5", "name": "Vin", "course": "bogus"}, category_id="c5",
                       category_name="Vin") == "drinks"


def test_categories_with_course_defaults_and_own_rows():
    assert [c["course"] for c in t.categories_with_course(None)] == ["starters", "mains", "desserts", "drinks"]
    row = {"categories": [{"categoryId": "c1", "name": "Pizza", "course": "mains"},
                          {"categoryId": "c2", "name": "Öl"}, {"categoryId": "c3", "name": "Tillbehör"}]}
    assert [c["course"] for c in t.categories_with_course(row)] == ["mains", "drinks", "other"]


# --- pacing ---------------------------------------------------------------------------

def test_default_hold_rules():
    assert t.default_hold("drinks", {"drinks", "starters", "mains"}) is False
    assert t.default_hold("starters", {"starters", "mains"}) is False
    assert t.default_hold("mains", {"starters", "mains"}) is True
    assert t.default_hold("mains", {"mains"}) is False
    assert t.default_hold("mains", {"mains"}, [{"course": "starters", "state": "sent"}]) is True
    assert t.default_hold("mains", {"mains"}, [{"course": "starters", "state": "sent"}], {"starters"}) is False
    assert t.default_hold("desserts", {"desserts"}) is False
    assert t.default_hold("desserts", {"desserts"}, [{"course": "mains", "state": "sent"}]) is True
    assert t.default_hold("desserts", {"desserts"}, [{"course": "mains", "state": "void"}]) is False
    assert t.default_hold("other", {"starters", "mains"}) is False


def test_add_sends_now_holds_mains_and_totals():
    tab = fresh_tab()
    round_ = t.add_items(tab, [line("Cola", "drinks", 4500, 2), line("Toast", "starters", 14500),
                               line("Oxfilé", "mains", 32500, 4), line("Brûlée", "desserts", 9500, hold=False)],
                         ANNA, NOW)
    assert round_["round"] == 1 and round_["kind"] == "order" and round_["sourceId"] == "a" * 32 + "r1"
    assert round_["courses"] == ["drinks", "starters", "desserts"]
    states = {i["name"]: i["state"] for i in tab["items"]}
    assert states == {"Cola": "sent", "Toast": "sent", "Oxfilé": "held", "Brûlée": "sent"}
    assert tab["referenceTotal"] == 2 * 4500 + 14500 + 4 * 32500 + 9500
    assert tab["nextRound"] == 2 and tab["version"] == 2
    assert all(len(i["itemId"]) == 12 for i in tab["items"])
    assert tab["ttl"] == t.epoch(NOW + timedelta(hours=48))


def test_everything_held_makes_no_round_and_fire_sends_one_course():
    tab = fresh_tab()
    assert t.add_items(tab, [line("Oxfilé", "mains", hold=True), line("Brûlée", "desserts", hold=True)],
                       ANNA, NOW) is None
    assert tab["rounds"] == []
    fired = t.fire(tab, ANNA, NOW, courses=["mains"])
    assert fired["kind"] == "fire" and fired["courses"] == ["mains"] and fired["round"] == 1
    assert [i["state"] for i in tab["items"]] == ["sent", "held"]
    assert t.fire(tab, ANNA, NOW, courses=["mains"]) is None
    rest = t.fire(tab, ANNA, NOW)
    assert rest["courses"] == ["desserts"] and not t.held_items(tab)
    with pytest.raises(t.TabError):
        t.fire(tab, ANNA, NOW, item_ids=["nope"])


def test_edit_held_items_only_and_remove_without_ticket():
    tab = fresh_tab()
    t.add_items(tab, [line("Cola", "drinks"), line("Oxfilé", "mains", hold=True), line("Torsk", "mains", hold=True)],
                ANNA, NOW)
    cola, steak, cod = tab["items"]
    send = t.edit_held(tab, [{"itemId": steak["itemId"], "quantity": 3, "note": "medium", "course": "starters"},
                             {"itemId": cod["itemId"], "remove": True}], ANNA, NOW)
    assert send == [] and steak["quantity"] == 3 and steak["note"] == "medium" and steak["course"] == "starters"
    assert cod["state"] == "void" and cod["voidReason"] == "before_send" and "voidRound" not in cod
    with pytest.raises(t.TabError):
        t.edit_held(tab, [{"itemId": cola["itemId"], "quantity": 2}], ANNA, NOW)
    assert t.edit_held(tab, [{"itemId": steak["itemId"], "hold": False}], ANNA, NOW) == [steak["itemId"]]


def test_partial_void_splits_and_lowers_the_total():
    tab = fresh_tab()
    t.add_items(tab, [line("IPA", "drinks", 8900, 3)], ANNA, NOW)
    ipa = tab["items"][0]
    round_ = t.void_items(tab, [(ipa["itemId"], 1)], "guest_changed", "byter till cola", ANNA, NOW)
    assert round_["kind"] == "void" and round_["reason"] == "guest_changed" and round_["courses"] == ["drinks"]
    part = next(i for i in tab["items"] if i["state"] == "void")
    assert ipa["quantity"] == 2 and ipa["state"] == "sent"
    assert part["quantity"] == 1 and part["splitFrom"] == ipa["itemId"] and part["voidRound"] == round_["round"]
    assert round_["itemIds"] == [part["itemId"]]
    assert tab["referenceTotal"] == 2 * 8900
    whole = t.void_items(tab, [(ipa["itemId"], 2)], "wrong", None, ANNA, NOW)
    assert ipa["state"] == "void" and whole["itemIds"] == [ipa["itemId"]] and tab["referenceTotal"] == 0
    with pytest.raises(t.TabError):
        t.void_items(tab, [(ipa["itemId"], 1)], "wrong", None, ANNA, NOW)


def test_void_refuses_held_items_and_too_many():
    tab = fresh_tab()
    t.add_items(tab, [line("Cola", "drinks", quantity=2), line("Oxfilé", "mains", hold=True)], ANNA, NOW)
    cola, steak = tab["items"]
    for wanted in ([(steak["itemId"], 1)], [(cola["itemId"], 3)]):
        with pytest.raises(t.TabError):
            t.void_items(tab, wanted, "wrong", None, ANNA, NOW)


def test_move_close_and_reopen():
    tab = fresh_tab()
    t.add_items(tab, [line("Cola", "drinks"), line("Oxfilé", "mains", hold=True)], ANNA, NOW)
    moved = t.move(tab, {"tableId": "t-7", "label": "7"}, ANNA, NOW)
    assert moved["kind"] == "move" and moved["fromLabel"] == "5" and moved["toLabel"] == "7"
    assert tab["tableId"] == "t-7" and "floorName" not in tab
    with pytest.raises(t.TabError) as exc:
        t.close(tab, ANNA, NOW)
    assert exc.value.code == "held_items" and exc.value.extra["tab"]["tabId"] == tab["tabId"]
    t.close(tab, ANNA, NOW, drop_held=True)
    assert tab["status"] == "closed" and tab["closeReason"] == "paid_elsewhere"
    assert tab["items"][1]["state"] == "void" and tab["items"][1]["voidReason"] == "closed"
    assert tab["ttl"] == t.epoch(NOW + timedelta(days=14))
    with pytest.raises(t.TabError) as exc:
        t.add_items(tab, [line("Cola", "drinks")], ANNA, NOW)
    assert exc.value.code == "tab_closed"
    assert t.reopen(tab, ANNA, NOW + timedelta(minutes=14)) is True
    assert tab["status"] == "open" and "closedAt" not in tab
    t.close(tab, ANNA, NOW)
    with pytest.raises(t.TabError) as exc:
        t.reopen(tab, ANNA, NOW + timedelta(minutes=16))
    assert exc.value.code == "reopen_expired"


def test_limits_make_a_full_tab():
    tab = fresh_tab()
    tab["items"] = [dict(line("Cola", "drinks"), itemId=f"{i:012x}", state="void") for i in range(150)]
    with pytest.raises(t.TabError) as exc:
        t.add_items(tab, [line("Cola", "drinks")], ANNA, NOW)
    assert exc.value.code == "tab_full" and exc.value.status == 413
    tab = fresh_tab()
    tab["rounds"] = [{"round": i} for i in range(60)]
    with pytest.raises(t.TabError) as exc:
        t.add_items(tab, [line("Cola", "drinks")], ANNA, NOW)
    assert exc.value.code == "tab_full"


def test_history_is_capped():
    tab = fresh_tab()
    for _ in range(120):
        t._history(tab, "edit", ANNA, NOW)
    assert len(tab["history"]) == 100


def test_view_hides_keys_subs_and_ttl_and_adds_ticket_states():
    tab = fresh_tab()
    t.add_items(tab, [line("Cola", "drinks", quantity=Decimal(2))], ANNA, NOW)
    row = {"PK": "LOCATION#loc", "SK": "TAB#x", **tab}
    view = t.tab_view(row, {1: [{"stationId": "bar", "stationName": "Bar", "status": "ready"}]})
    for hidden in ("PK", "SK", "ttl", "tenantId", "openedBy"):
        assert hidden not in view
    assert all("by" not in h for h in view["history"])
    assert "addedBy" not in view["items"][0] and view["items"][0]["addedByName"] == "Anna"
    assert view["rounds"][0]["tickets"][0]["status"] == "ready"
    assert view["items"][0]["quantity"] == 2 and isinstance(view["items"][0]["quantity"], int)
    assert view["reservationId"] is None


def test_no_receipt_wording_in_the_model():
    import inspect
    source = inspect.getsource(t).lower()
    assert "kvitto" not in source and "receipt\"" not in source


# --- guests ---------------------------------------------------------------------------

def booking(rid, tables, start, minutes, status="reserved", name="Gäst", party=2):
    begin = NOW + timedelta(minutes=start)
    return {"reservationId": rid, "tableIds": tables, "status": status, "customerName": name, "partySize": party,
            "bookedFor": t.iso(begin), "endsAt": t.iso(begin + timedelta(minutes=minutes)),
            "startTime": "x", "endTime": "y"}


def test_guest_matching_prefers_arrived_then_current_then_upcoming():
    guests = t.guests_by_table([
        booking("r1", ["t1"], -60, 120, "arrived", "Åsa Öberg", 4),
        booking("r2", ["t1"], 20, 120),
        booking("r3", ["t2"], 20, 120, name="Erik Lind"),
        booking("r4", ["t3"], 80, 120, name="Omar Haddad"),
        booking("r5", ["t4"], 120, 120),
        booking("r6", ["t5"], -300, 120, "arrived"),
        booking("r7", ["t6"], -60, 120, "cancelled_by_guest"),
    ], NOW)
    assert guests["t1"]["name"] == "Åsa Öberg" and guests["t1"]["partySize"] == 4 and "upcoming" not in guests["t1"]
    assert guests["t2"]["reservationId"] == "r3" and "upcoming" not in guests["t2"]
    assert guests["t3"] == {**guests["t3"], "upcoming": True, "name": "Omar Haddad"}
    assert {"t4", "t5", "t6"}.isdisjoint(guests)


def test_guest_window_from_local_times_when_no_timestamps():
    res = {"reservationId": "r1", "tableIds": ["t1"], "status": "reserved", "customerName": "Maja",
           "partySize": 6, "date": "2026-11-19", "startTime": "18:00", "endTime": "20:00"}
    assert t.guests_by_table([res], NOW, {"timezone": "Europe/Stockholm"})["t1"]["name"] == "Maja"


def test_table_title():
    assert t.table_title("5") == "Bord 5" and t.table_title("Bar") == "Bar"

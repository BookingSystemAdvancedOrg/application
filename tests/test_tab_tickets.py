"""Kitchen and bar tickets for staff table orders (shared/tab_tickets.py):
food to Kök and drinks to the drinks station, a station without printers
printing on Kök's with its own name big, held courses listed as waiting,
the fire round, MAKULERAD and FLYTTAD tickets, relabel on move, put-once
replay, and no price, guest name or phone in any rendered byte stream.
DynamoDB is moto (test_printing's env)."""

from datetime import timedelta
from decimal import Decimal

import pytest

from shared import print_ticket as t
from shared import printing as p
from shared import routing as r
from shared import tab_tickets as tt
from test_printing import LOC_A, NOW, TENANT_A, api, env  # noqa: F401 - env is a fixture
from test_ticket_routing import add, drain, star_print

TAB_ID = "b" * 32
GUEST = "Åsa Öberg"


def item(item_id, name, course, state="sent", *, category=None, quantity=1, round_=1, **extra):
    return {"itemId": item_id, "menuItemId": f"m-{item_id}", "name": name, "unitPrice": 12500,
            "quantity": quantity, "options": [], "course": course, "category": category or course,
            "state": state, **({"round": round_} if state == "sent" else {}), "addedByName": "Anna", **extra}


def tab(items, **extra):
    out = {"tabId": TAB_ID, "tenantId": TENANT_A, "locationId": LOC_A, "status": "open", "tableId": "el-3",
           "tableLabel": "3", "covers": Decimal(4), "guestName": GUEST, "items": items, "rounds": [],
           "referenceTotal": 99900, "openedByName": "Anna"}
    out.update(extra)
    return out


def round_(n, kind, item_ids, **extra):
    return {"round": n, "kind": kind, "at": "2026-07-08T16:42:00Z", "byName": "Anna", "itemIds": item_ids,
            "courses": [], "dispatched": False, "stations": [], **extra}


ITEMS = [
    item("i1", "Pilsner", "drinks", category="c0000000001", categoryName="Öl", alcohol=True, quantity=2),
    item("i2", "Toast Skagen", "starters", quantity=2, options=[{"group": "Bröd", "choice": "Surdeg"}],
         note="Utan lök"),
    item("i3", "Oxfilé", "mains", state="held", quantity=3, options=[{"group": "Stekgrad", "choice": "Medium"}]),
    item("i4", "Crème brûlée", "desserts", state="held", quantity=2),
    item("i5", "Husets röda glas", "drinks", state="held", category="c0000000002", categoryName="Vin",
         alcohol=True),
]


def kitchen_only_printer(env, *, auto_print=True):
    """Kök has one Star printer, the drinks station none."""
    kitchen, secret, _ = add(env, "Kök 1")
    if auto_print:
        api(env, "PUT", "settings", {**p.get_settings(LOC_A), "autoPrint": True})
    status, current = api(env, "GET", "routing")
    assert status == 200
    stations = [{**s, "printerIds": [kitchen] if s["stationId"] == "kok" else []} for s in current["stations"]]
    status, _ = api(env, "PUT", "routing", {**current, "stations": [
        {k: s[k] for k in ("stationId", "name", "printerIds")} for s in stations]})
    assert status == 200
    return kitchen, secret


def tickets_of(source):
    return {x["stationId"]: x for x in r.source_tickets(LOC_A, source)}


# --- routing and the order round ---------------------------------------------------------

def test_order_round_splits_kitchen_and_bar_and_lists_waiting_per_station(env):
    kitchen_only_printer(env, auto_print=False)
    result = tt.dispatch_round(TENANT_A, LOC_A, tab(ITEMS), round_(1, "order", ["i1", "i2"]), NOW)
    assert [s["stationId"] for s in result["stations"]] == ["kok", "bar"]
    assert result["warnings"] == []  # nothing prints with auto print off: the kitchen display has them
    got = tickets_of(f"{TAB_ID}r1")
    kok, bar = got["kok"], got["bar"]
    assert [e["name"] for e in kok["entries"]] == ["Toast Skagen"] and kok["entries"][0]["course"] == "starters"
    assert [e["name"] for e in bar["entries"]] == ["Pilsner"]
    assert kok["waiting"] == [{"course": "mains", "quantity": 3}, {"course": "desserts", "quantity": 2}]
    assert bar["waiting"] == [{"course": "drinks", "quantity": 1}]
    view = r.view(kok)
    assert view["kind"] == "tab" and view["tabId"] == TAB_ID and view["round"] == 1 and view["tableId"] == "el-3"
    assert view["tableLabel"] == "3" and view["covers"] == 4 and view["waiter"] == "Anna" and view["mode"] == "table"
    assert "orderId" not in kok and "number" not in kok and "fire" not in kok
    assert all("unitPrice" not in e and "price" not in e for e in kok["entries"] + bar["entries"])
    assert GUEST not in str(kok) and GUEST not in str(bar)
    # one Query gives every round's state per station
    assert tt.round_ticket_states(LOC_A, TAB_ID) == {1: [{"stationId": "bar", "stationName": "Dryck",
                                                          "status": "sent"},
                                                         {"stationId": "kok", "stationName": "Kök",
                                                          "status": "sent"}]}
    assert tt.round_ticket_states(LOC_A, "not-a-tab#") == {}


def test_bar_without_printer_prints_on_the_kitchen_printer_with_its_own_heading(env):
    kitchen, secret = kitchen_only_printer(env)
    result = tt.dispatch_round(TENANT_A, LOC_A, tab(ITEMS), round_(1, "order", ["i1", "i2"]), NOW)
    stations = {s["stationId"]: s for s in result["stations"]}
    assert stations["kok"] == {"stationId": "kok", "stationName": "Kök", "printers": 1, "fallback": False}
    assert stations["bar"] == {"stationId": "bar", "stationName": "Dryck", "printers": 1, "fallback": True}
    assert result["warnings"] == [{"code": "station_no_printer", "stationId": "bar",
                                   "text": "Dryck har ingen skrivare, bongen skrevs ut i köket."}]
    docs = drain(star_print, env, kitchen, secret)
    assert len(docs) == 2
    food = next(d for d in docs if "Toast Skagen" in d)
    drinks = next(d for d in docs if "Pilsner" in d)
    assert "KÖK" in food and "BORD 3" in food and "Pilsner" not in food
    assert "DRYCK" in drinks and "BORD 3" in drinks and "Toast Skagen" not in drinks
    assert "Runda 1 · Anna · 18:42 · 4 gäster" in food
    assert "Väntar: Varmrätt 3, Efterrätt 2" in food and "Väntar: Dryck 1" in drinks
    assert "+ Bröd: Surdeg" in food and "Utan lök" in food
    assert "Bord 3 r1" in food and TAB_ID[:6] not in food and "Köksnota, ej kvitto" in food
    assert all(j["status"] == "PRINTED" for j in p.order_jobs(LOC_A, f"{TAB_ID}r1"))
    # "Skriv ut igen" on the kitchen display falls back too
    status, body = api(env, "POST", f"tickets/{TAB_ID}r1/bar/print", role="staff_user")
    assert status == 202 and len(body["jobIds"]) == 1


def test_online_order_bar_ticket_does_not_fall_back(env):
    kitchen_only_printer(env)
    ticket = r.tickets("a" * 32, {"orderId": "a" * 32, "number": 3}, [{"name": "Cola", "category": "drinks"}],
                       None, r.get_config(LOC_A), NOW)[0]
    assert ticket["stationId"] == "bar"
    assert p.ticket_jobs(TENANT_A, LOC_A, ticket, r.get_config(LOC_A), NOW) == []


def test_no_printer_anywhere_still_makes_display_tickets_with_a_warning(env):
    kitchen_only_printer(env)
    status, current = api(env, "GET", "routing")
    status, _ = api(env, "PUT", "routing", {**current, "stations": [
        {"stationId": s["stationId"], "name": s["name"], "printerIds": []} for s in current["stations"]]})
    assert status == 200 and p.get_settings(LOC_A)["autoPrint"]
    result = tt.dispatch_round(TENANT_A, LOC_A, tab(ITEMS), round_(1, "order", ["i2"]), NOW)
    assert result["stations"] == [{"stationId": "kok", "stationName": "Kök", "printers": 0, "fallback": False}]
    assert result["warnings"][0]["code"] == "station_no_printer"
    assert set(tickets_of(f"{TAB_ID}r1")) == {"kok"}


def test_replayed_dispatch_never_makes_a_second_ticket_or_job(env):
    kitchen_only_printer(env)
    for _ in range(3):
        tt.dispatch_round(TENANT_A, LOC_A, tab(ITEMS), round_(1, "order", ["i1", "i2"]), NOW)
    assert len(r.source_tickets(LOC_A, f"{TAB_ID}r1")) == 2
    assert len(p.order_jobs(LOC_A, f"{TAB_ID}r1")) == 2


def test_station_chosen_at_add_time_wins_and_none_makes_no_ticket(env):
    kitchen_only_printer(env, auto_print=False)
    items = [item("j1", "Lemonad", "drinks", stationId="kok"), item("j2", "Bröd", "other", stationId=None,
                                                                    station="none")]
    tt.dispatch_round(TENANT_A, LOC_A, tab(items), round_(1, "order", ["j1", "j2"]), NOW)
    got = tickets_of(f"{TAB_ID}r1")
    assert list(got) == ["kok"] and [e["name"] for e in got["kok"]["entries"]] == ["Lemonad"]


def test_item_with_no_station_at_add_time_stays_off_tickets_after_routing_changes(env):
    kitchen_only_printer(env, auto_print=False)
    # added while its category went nowhere (stationId null); the category now routes to Kök
    items = [item("n1", "Bröd", "other", category="mains", stationId=None),
             item("n2", "Bröd", "other", category="mains", state="held", stationId=None),
             item("n3", "Toast Skagen", "starters", stationId="kok")]
    tt.dispatch_round(TENANT_A, LOC_A, tab(items), round_(1, "order", ["n1", "n3"]), NOW)
    got = tickets_of(f"{TAB_ID}r1")
    assert list(got) == ["kok"] and [e["name"] for e in got["kok"]["entries"]] == ["Toast Skagen"]
    assert "waiting" not in got["kok"]
    voided = [{**items[0], "state": "void"}]
    assert tt.dispatch_void(TENANT_A, LOC_A, tab(voided), round_(2, "void", ["n1"]), NOW)["stations"] == []


def test_items_sent_by_the_round_are_never_listed_as_waiting(env):
    kitchen_only_printer(env, auto_print=False)
    items = [item("h1", "Oxfilé", "mains", state="held"), item("h2", "Crème brûlée", "desserts", state="held")]
    tt.dispatch_round(TENANT_A, LOC_A, tab(items), round_(2, "fire", ["h1"]), NOW)
    assert tickets_of(f"{TAB_ID}r2")["kok"]["waiting"] == [{"course": "desserts", "quantity": 1}]


def test_explicit_course_wins_over_category_and_other_is_ovrigt(env):
    lines = [{"name": "Toast Skagen", "category": "starters", "course": "mains"},
             {"name": "Bröd", "category": "starters", "course": "other"},
             {"name": "Sallad", "category": "starters"}]
    entries = r.split(lines, r.default_config())["kok"]
    assert [e["course"] for e in entries] == ["mains", None, "starters"]
    assert [name for name, _ in r.by_course(entries)] == ["Förrätt", "Varmrätt", "Övrigt"]


# --- fire, void, move ---------------------------------------------------------------------

def test_fire_round_prints_skicka_nu(env):
    kitchen, secret = kitchen_only_printer(env)
    items = [{**i, "state": "sent", "round": 2} if i["itemId"] == "i3" else i for i in ITEMS]
    tt.dispatch_round(TENANT_A, LOC_A, tab(items), round_(2, "fire", ["i3"], courses=["mains"]), NOW)
    kok = tickets_of(f"{TAB_ID}r2")["kok"]
    assert kok["fire"] == "mains" and [e["name"] for e in kok["entries"]] == ["Oxfilé"]
    assert kok["waiting"] == [{"course": "desserts", "quantity": 2}]
    doc = drain(star_print, env, kitchen, secret)[0]
    assert "SKICKA NU: VARMRÄTT" in doc and "+ Stekgrad: Medium" in doc and "Runda 2" in doc


def test_void_round_prints_makulerad_with_reason_and_no_allergy_slip(env):
    kitchen, secret = kitchen_only_printer(env)
    items = ITEMS + [item("v1", "Toast Skagen", "starters", state="void", quantity=1, allergy=True,
                          voidReason="guest_changed")]
    result = tt.dispatch_void(TENANT_A, LOC_A, tab(items), round_(3, "void", ["v1"], reason="guest_changed"), NOW)
    assert [s["stationId"] for s in result["stations"]] == ["kok"]
    kok = tickets_of(f"{TAB_ID}r3")["kok"]
    assert kok["kind"] == "tab_void" and kok["allergy"] is False and kok["voidReason"] == "guest_changed"
    docs = drain(star_print, env, kitchen, secret)
    assert len(docs) == 1
    assert "MAKULERAD" in docs[0] and "1 x Toast Skagen" in docs[0] and "Orsak: Gästen ändrade sig" in docs[0]
    assert "Väntar" not in docs[0] and "!! ALLERGI !!" not in docs[0]


def test_move_relabels_open_tickets_and_prints_flyttad_where_tickets_went(env):
    kitchen, secret = kitchen_only_printer(env)
    tt.dispatch_round(TENANT_A, LOC_A, tab(ITEMS), round_(1, "order", ["i1", "i2"]), NOW)
    drain(star_print, env, kitchen, secret)
    r.set_status(LOC_A, f"{TAB_ID}r1", "bar", "ready", "u", NOW)
    moved = tab(ITEMS, tableId="el-7", tableLabel="7")
    env["clock"]["now"] = NOW + timedelta(minutes=5)
    result = tt.dispatch_move(TENANT_A, LOC_A, moved, round_(2, "move", [], fromLabel="3", toLabel="7"),
                              env["clock"]["now"])
    assert [s["stationId"] for s in result["stations"]] == ["kok", "bar"]
    first = tickets_of(f"{TAB_ID}r1")
    assert first["kok"]["tableLabel"] == "7" and first["kok"]["tableId"] == "el-7"
    assert first["bar"]["tableLabel"] == "3"  # already served: left as it was
    slips = tickets_of(f"{TAB_ID}r2")
    assert {x["kind"] for x in slips.values()} == {"tab_move"} and slips["kok"]["entries"] == []
    assert r.view(slips["kok"])["fromLabel"] == "3" and r.view(slips["kok"])["toLabel"] == "7"
    docs = drain(star_print, env, kitchen, secret)
    assert len(docs) == 2 and all("FLYTTAD" in d and "Bord 3 -> Bord 7" in d for d in docs)
    # a second move does not relabel the first move's slips
    again = tab(ITEMS, tableId="el-9", tableLabel="9")
    tt.dispatch_move(TENANT_A, LOC_A, again, round_(3, "move", [], fromLabel="7", toLabel="9"), NOW)
    assert tickets_of(f"{TAB_ID}r2")["kok"]["tableLabel"] == "7"
    assert tickets_of(f"{TAB_ID}r1")["kok"]["tableLabel"] == "9"


def test_a_flyttad_slip_without_a_from_label_prints_no_table_id(env):
    kitchen, secret = kitchen_only_printer(env)
    tt.dispatch_round(TENANT_A, LOC_A, tab(ITEMS), round_(1, "order", ["i1", "i2"]), NOW)
    drain(star_print, env, kitchen, secret)
    moved = tab(ITEMS, tableId="el-7", tableLabel="7")
    env["clock"]["now"] = NOW + timedelta(minutes=5)
    tt.dispatch_move(TENANT_A, LOC_A, moved, round_(2, "move", [], fromTable="el-3", toTable="el-7", toLabel="7"),
                     env["clock"]["now"])
    slips = tickets_of(f"{TAB_ID}r2")
    assert r.view(slips["kok"]).get("fromLabel") is None
    docs = drain(star_print, env, kitchen, secret)
    assert docs and all("Bord ? -> Bord 7" in d and "el-3" not in d and TAB_ID[:6] not in d for d in docs)


def test_failed_table_order_job_leaves_the_banner_by_age(env):
    kitchen_only_printer(env)
    tt.dispatch_round(TENANT_A, LOC_A, tab(ITEMS), round_(1, "order", ["i2"]), NOW)
    job = p.order_jobs(LOC_A, f"{TAB_ID}r1")[0]
    p.fail(job, NOW, expected="PENDING")
    assert api(env, "GET", "status")[1]["failed"] == 1
    env["clock"]["now"] += timedelta(hours=25)
    assert api(env, "GET", "status")[1]["failed"] == 0


# --- menu view -------------------------------------------------------------------------------

def test_menu_view_resolves_station_course_and_prices_in_ore(env):
    kitchen_only_printer(env, auto_print=False)
    categories = [{"categoryId": "starters", "name": "Förrätter"}, {"categoryId": "mains", "name": "Varmrätter"},
                  {"categoryId": "c0000000001", "name": "Öl"},
                  {"categoryId": "c0000000003", "name": "Pizza", "course": "mains"},
                  {"categoryId": "c0000000004", "name": "Tillbehör"}]
    items = [
        {"menuItemId": "m1", "name": "Toast Skagen", "description": "Räkor", "price": Decimal("145"),
         "category": "starters", "allergens": ["gluten", "crustaceans"], "imageKey": "k.jpg"},
        {"menuItemId": "m2", "name": "Pilsner", "description": "", "price": Decimal("69.50"),
         "category": "c0000000001", "alcohol": True},
        {"menuItemId": "m3", "name": "Margherita", "description": "", "price": Decimal("125"),
         "category": "c0000000003", "options": [{"name": "Extra", "required": False, "maxChoices": 2,
                                                 "choices": [{"name": "Ost", "price": Decimal("15.5")}]}]},
        {"menuItemId": "m4", "name": "Pommes", "description": "", "price": Decimal("39"),
         "category": "c0000000004"},
        {"menuItemId": "m5", "name": "Trasig", "price": "abc", "category": "mains"},
    ]
    view = tt.menu_view(LOC_A, items, categories)
    assert [c["course"] for c in view["categories"]] == ["starters", "mains", "drinks", "mains", "other"]
    by_id = {i["menuItemId"]: i for i in view["items"]}
    assert set(by_id) == {"m1", "m2", "m3", "m4"}
    assert by_id["m1"]["price"] == 14500 and by_id["m1"]["stationId"] == "kok" and by_id["m1"]["course"] == "starters"
    assert by_id["m1"]["allergens"] == ["gluten", "crustaceans"] and by_id["m1"]["imageKey"] == "k.jpg"
    assert by_id["m2"]["price"] == 6950 and by_id["m2"]["stationId"] == "bar" and by_id["m2"]["course"] == "drinks"
    assert by_id["m2"]["stationName"] == "Dryck"
    assert by_id["m3"]["course"] == "mains" and by_id["m3"]["options"][0]["choices"][0]["price"] == 1550
    assert by_id["m4"]["course"] == "other" and by_id["m4"]["stationId"] == "kok"
    stations = {s["stationId"]: s for s in view["stations"]}
    assert stations["kok"]["kind"] == "kok" and stations["kok"]["printers"] == 1
    assert stations["bar"]["kind"] == "dryck" and stations["bar"]["printers"] == 0


def test_resolve_item_follows_routing_rules():
    config = r.default_config()
    assert tt.resolve_item(LOC_A, {"category": "mains"}, config) == ("kok", "Kök")
    assert tt.resolve_item(LOC_A, {"category": "c1", "categoryName": "Vinlista"}, config) == ("bar", "Dryck")
    assert tt.resolve_item(LOC_A, {"category": "mains", "station": "none"}, config) == (None, None)
    assert tt.resolve_item(LOC_A, {"category": "mains", "alcohol": True, "station": "bar"}, config) == ("bar", "Dryck")


# --- rendering: never a price, a guest name or a phone ---------------------------------------

def _ticket(kind, **extra):
    base = {"SK": f"TICKET#{TAB_ID}r2#kok", "sourceId": f"{TAB_ID}r2", "stationId": "kok", "stationName": "Kök",
            "kind": kind, "tabId": TAB_ID, "round": 2, "mode": "table", "tableId": "el-3", "tableLabel": "3",
            "covers": 1, "waiter": "Anna", "placedAt": "2026-01-08T17:42:00Z", "status": "sent", "allergy": False,
            "entries": [{"name": "Oxfilé", "quantity": 3, "course": "mains", "options": ["Stekgrad: Medium"]},
                        {"name": "Bröd", "quantity": 1, "course": None, "note": "Glutenfri", "allergy": True}]}
    base.update(extra)
    return base


TICKETS = [
    _ticket("tab", fire="mains", waiting=[{"course": "desserts", "quantity": 2}], allergy=True),
    _ticket("tab_void", voidReason="wrong"),
    _ticket("tab_move", entries=[], fromLabel="3", toLabel="Terrassen", tableLabel="Terrassen"),
]


@pytest.mark.parametrize("fmt", ["star", "epson", "text"])
@pytest.mark.parametrize("ticket", TICKETS, ids=[x["kind"] for x in TICKETS])
@pytest.mark.parametrize("allergy", [False, True])
def test_rendered_tab_tickets_carry_no_price_guest_or_phone(fmt, ticket, allergy):
    out = t.station_ticket(ticket, {"customerName": GUEST, "customerPhone": "0701234567"}, {}, fmt=fmt,
                           allergy=allergy)
    text = out.decode("cp1252") if isinstance(out, bytes) else str(out)
    for banned in ("125", "kr", "SEK", "Summa", "Totalt", "Att slå in", GUEST, "0701234567", "kvitto ", "Order "):
        assert banned not in text, banned
    assert "Köksnota, ej kvitto" in text


def test_tab_ticket_layout_star():
    text = t.station_ticket(TICKETS[0], None, {"timezone": "Europe/Stockholm"}).decode("cp1252")
    assert text.index("KÖK") < text.index("BORD 3") < text.index("Runda 2 · Anna · 18:42 · 1 gäst")
    assert "SKICKA NU: VARMRÄTT," in text and "ÖVRIGT" in text
    assert "-- VARMRÄTT --" in text and "-- ÖVRIGT --" in text and "ALLERGI: se notering" in text
    assert "Väntar: Efterrätt 2" in text and "Glutenfri" in text
    move = t.station_ticket(TICKETS[2], None, {}, fmt="text")
    rows = [row["text"] for row in move]
    assert "FLYTTAD" in rows and "TERRASSEN" in rows and "Bord 3 -> Terrassen" in rows
    assert not any("Oxfilé" in row for row in rows)
    void = t.station_ticket(TICKETS[1], None, {}, fmt="epson")
    assert "MAKULERAD" in void and "Orsak: Fel registrerad" in void and "SKICKA NU" not in void
    assert void.endswith('<cut type="feed"/></epos-print>')


def test_simulated_printer_shows_tab_ticket(env):
    status, body = api(env, "POST", "devices", {"name": "Sim", "protocol": "sim"})
    assert status == 201
    device_id = body["device"]["deviceId"]
    api(env, "PUT", "settings", {**p.get_settings(LOC_A), "autoPrint": True})
    tt.dispatch_round(TENANT_A, LOC_A, tab(ITEMS), round_(1, "order", ["i1", "i2"]), NOW)
    status, paper = api(env, "GET", f"devices/{device_id}/simulated")
    assert status == 200 and paper["tickets"]
    rows = [row["text"] for x in paper["tickets"] for row in x["lines"]]
    assert "BORD 3" in rows and not any(GUEST in row for row in rows)

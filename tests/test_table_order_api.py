"""table-order end to end on moto: every endpoint, idempotent replay (a
double tap or retry never makes a second round or ticket), one open tab per
table, version conflicts, move, close / reopen, staff on their own location
only and the plan feature. The ticket facade (shared/tab_tickets.py) is a
recording fake injected into sys.modules, so this suite runs without it."""

import importlib.util
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from shared import dynamo as shared_dynamo
from shared import table_orders as t
from shared import tenant as shared_tenant
from tenant_support import (LOC_A, LOC_A2, LOC_B, REGION, TENANT_A, TENANT_B, TENANT_TABLE, features_on,
                            seed_tenancy, tenant_row, with_tenant_claims)

APP = Path(__file__).parents[1] / "functions" / "table-order" / "app.py"
TABLE = "test-table-order"
MENU = "test-menu"
LAYOUT = "test-layout"
RESERVATION = "test-reservation"
NOW = datetime(2026, 11, 19, 17, 0, tzinfo=timezone.utc)  # 18:00 in Stockholm
TODAY = "2026-11-19"
COLLECTION = "/locations/{locationId}/table-orders"
ITEM = "/locations/{locationId}/table-orders/{proxy+}"


class FakeTickets(types.ModuleType):
    """shared/tab_tickets.py as the contract in docs/TABLE_ORDERS.md says."""

    def __init__(self):
        super().__init__("shared.tab_tickets")
        self.calls = []
        self.fail = False
        self.states = {}
        self.no_printer = False

    def menu_view(self, location_id, items, categories):
        return {"categories": categories,
                "items": [{"menuItemId": i["menuItemId"], "name": i["name"], "price": int(i["price"] * 100)}
                          for i in items],
                "stations": [{"stationId": "kok", "name": "Kök", "kind": "kok", "printers": 1}]}

    def resolve_item(self, location_id, line, config=None):
        if line.get("alcohol") or line.get("category") in ("drinks", "beer"):
            return "bar", "Bar"
        return "kok", "Kök"

    def _dispatch(self, kind, tenant_id, location_id, tab, round_, now):
        self.calls.append((kind, tab["tabId"], int(round_["round"]), list(round_["itemIds"])))
        if self.fail:
            raise RuntimeError("printer table down")
        stations = sorted({i["stationId"] for i in tab["items"] if i["itemId"] in round_["itemIds"]} or {"kok"})
        out = [{"stationId": s, "stationName": "Bar" if s == "bar" else "Kök", "printers": 1,
                "fallback": s == "bar" and self.no_printer} for s in stations]
        warnings = [{"code": "station_no_printer", "stationId": "bar"}] if self.no_printer and "bar" in stations else []
        return {"stations": out, "warnings": warnings}

    def dispatch_round(self, *args):
        return self._dispatch("round", *args)

    def dispatch_void(self, *args):
        return self._dispatch("void", *args)

    def dispatch_move(self, *args):
        return self._dispatch("move", *args)

    def round_ticket_states(self, location_id, tab_id):
        return self.states.get(tab_id, {})


def _pk_sk(name):
    return dict(TableName=name, BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
                AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                      {"AttributeName": "SK", "AttributeType": "S"}])


def dish(dish_id, name, price, category, **extra):
    return {"PK": f"LOCATION#{LOC_A}", "SK": f"MENU#{dish_id}", "menuItemId": dish_id, "name": name,
            "description": "", "price": Decimal(price), "category": category, "active": True, **extra}


def table_el(element_id, floor="f1", **extra):
    return {"elementId": element_id, "type": "table", "floorId": floor, "seats": Decimal(4), "shape": "square",
            "zone": "inside", **extra}


@pytest.fixture
def env(monkeypatch):
    for key, value in {"ENVIRONMENT": "dev", "AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t",
                       "AWS_SESSION_TOKEN": "t", "AWS_DEFAULT_REGION": REGION, "TABLE_ORDER_TABLE_NAME": TABLE,
                       "MENU_TABLE_NAME": MENU, "PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME": LAYOUT,
                       "RESERVATION_TABLE_NAME": RESERVATION, "PRINT_TABLE_NAME": "test-print",
                       "USER_TABLE_NAME": "test-user"}.items():
        monkeypatch.setenv(key, value)
    clock = {"now": NOW}
    monkeypatch.setattr(t, "now_utc", lambda: clock["now"])
    fake = FakeTickets()
    monkeypatch.setitem(sys.modules, "shared.tab_tickets", fake)
    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        seed_tenancy(monkeypatch)
        ddb = boto3.client("dynamodb", region_name=REGION)
        for name in (TABLE, MENU, LAYOUT, RESERVATION):
            ddb.create_table(**_pk_sk(name))
        res = boto3.resource("dynamodb", region_name=REGION)
        menu = res.Table(MENU)
        for item in (dish("cola", "Cola", "45", "drinks"), dish("toast", "Toast Skagen", "145", "starters"),
                     dish("steak", "Oxfilé", "325", "mains", options=[
                         {"name": "Stekgrad", "required": True, "maxChoices": 1,
                          "choices": [{"name": "Medium", "price": Decimal(0)},
                                      {"name": "Rare", "price": Decimal("10")}]}]),
                     dish("cod", "Torskrygg", "295", "mains"), dish("brulee", "Crème brûlée", "95", "desserts"),
                     dish("ipa", "IPA", "89", "beer", alcohol=True), dish("water", "Kranvatten", "0", "drinks"),
                     dish("gone", "Slut", "100", "mains", active=False)):
            menu.put_item(Item=item)
        menu.put_item(Item={"PK": f"LOCATION#{LOC_A}", "SK": "CATEGORIES", "version": 1, "categories": [
            {"categoryId": "starters", "name": "Förrätter"}, {"categoryId": "mains", "name": "Varmrätter"},
            {"categoryId": "desserts", "name": "Efterrätter"}, {"categoryId": "drinks", "name": "Drycker"},
            {"categoryId": "beer", "name": "Öl"}]})
        layout = res.Table(LAYOUT)
        layout.put_item(Item={"PK": f"LOCATION#{LOC_A}", "SK": "LAYOUT#ACTIVATION", "currentVersion": Decimal(1),
                              "recordType": "layoutActivationState"})
        layout.put_item(Item={"PK": f"LOCATION#{LOC_A}", "SK": "LAYOUT#v1", "version": Decimal(1), "elements": [
            {"elementId": "f1", "type": "floor", "name": "Matsal", "level": Decimal(0)},
            table_el("t1"), table_el("t2"), table_el("t3"), table_el("t4", label="Baren")]})
        bookings = res.Table(RESERVATION)
        start = NOW - timedelta(minutes=30)
        bookings.put_item(Item={
            "PK": f"LOCATION#{LOC_A}", "SK": f"RESERVATION#{TODAY}#{'b' * 32}", "reservationId": "b" * 32,
            "locationId": LOC_A, "date": TODAY, "startTime": "17:30", "endTime": "19:30", "status": "arrived",
            "bookedFor": t.iso(start), "endsAt": t.iso(start + timedelta(hours=2)), "tableIds": ["t3"],
            "partySize": Decimal(4), "customerName": "Åsa Öberg"})
        bookings.put_item(Item={"PK": f"LOCATION#{LOC_A}", "SK": f"RID#{'b' * 32}", "date": TODAY})
        spec = importlib.util.spec_from_file_location("table_order_app", APP)
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        yield types.SimpleNamespace(app=app, fake=fake, clock=clock, res=res)
        shared_tenant.reset_caches()


_seq = {"n": 0}


def rid():
    _seq["n"] += 1
    return f"{_seq['n']:032x}"


def call(env, method, proxy=None, body=None, query=None, role="owner_user", location=LOC_A, tenant=TENANT_A):
    params = {"locationId": location, **({"proxy": proxy} if proxy is not None else {})}
    event = {"version": "2.0", "routeKey": f"{method} {ITEM if proxy is not None else COLLECTION}",
             "pathParameters": params, "queryStringParameters": query,
             "body": json.dumps(body) if body is not None else None}
    response = env.app.handler(with_tenant_claims(event, tenant, role), None)
    return response["statusCode"], json.loads(response["body"])


def open_tab(env, table="t1", items=None, **extra):
    body = {"requestId": rid(), "tableId": table, "items": items if items is not None else [
        {"menuItemId": "cola", "quantity": 2}, {"menuItemId": "toast", "quantity": 1},
        {"menuItemId": "steak", "quantity": 2, "options": [{"group": "Stekgrad", "choice": "Rare"}]}], **extra}
    return call(env, "POST", body=body)


# --- open, pacing, replay ---------------------------------------------------------------

def test_open_sends_drinks_and_starters_and_holds_mains(env):
    status, body = open_tab(env)
    assert status == 201
    tab = body["tab"]
    assert tab["tableId"] == "t1" and tab["tableLabel"] == "1" and tab["floorName"] == "Matsal"
    assert {i["name"]: i["state"] for i in tab["items"]} == {"Cola": "sent", "Toast Skagen": "sent",
                                                             "Oxfilé": "held"}
    steak = next(i for i in tab["items"] if i["name"] == "Oxfilé")
    assert steak["unitPrice"] == 33500 and steak["options"] == [{"group": "Stekgrad", "choice": "Rare"}]
    assert tab["referenceTotal"] == 2 * 4500 + 14500 + 2 * 33500
    assert body["round"]["round"] == 1 and body["round"]["dispatched"] is True
    assert {s["stationId"] for s in body["round"]["stations"]} == {"kok", "bar"}
    assert tab["openedByName"] == "Owner" and body["warnings"] == []
    assert env.fake.calls == [("round", tab["tabId"], 1, body["round"]["itemIds"])]
    for hidden in ("tenantId", "ttl", "PK", "openedBy"):
        assert hidden not in tab


def test_a_double_tap_is_one_round_and_one_ticket(env):
    body = {"requestId": rid(), "tableId": "t1", "items": [{"menuItemId": "cola", "quantity": 1}]}
    first = call(env, "POST", body=body)
    second = call(env, "POST", body=body)
    assert first[0] == 201 and second[0] == 200 and second[1]["replayed"] is True
    assert second[1]["tab"]["tabId"] == first[1]["tab"]["tabId"] and len(second[1]["tab"]["rounds"]) == 1
    assert len(env.fake.calls) == 1
    tab_id = first[1]["tab"]["tabId"]
    add = {"requestId": rid(), "items": [{"menuItemId": "ipa", "quantity": 1}]}
    assert call(env, "POST", f"{tab_id}/items", add)[0] == 200
    again = call(env, "POST", f"{tab_id}/items", add)
    assert again[1]["replayed"] is True and len(again[1]["tab"]["rounds"]) == 2 and len(env.fake.calls) == 2


def test_one_open_tab_per_table_and_the_second_waiter_gets_the_tab(env):
    status, first = open_tab(env)
    status, body = open_tab(env, items=[{"menuItemId": "cola", "quantity": 1}])
    assert status == 409 and body["error"] == "table_has_open_order"
    assert body["message"] == "Bord 1 har redan en öppen beställning."
    assert body["tab"]["tabId"] == first["tab"]["tabId"]
    status, body = open_tab(env, table="t4", items=[])
    assert status == 201 and body["round"] is None and body["tab"]["version"] == 1
    status, body = open_tab(env, table="t4", items=[])
    assert body["message"] == "Baren har redan en öppen beställning."


def test_lock_conflict_inside_the_transaction_answers_with_the_tab(env, monkeypatch):
    status, first = open_tab(env)
    monkeypatch.setattr(env.app, "_open_conflict",
                        lambda *a, _calls=[]: None if not _calls.append(1) and len(_calls) == 1
                        else t.TabError("table_has_open_order", tab={"tabId": first["tab"]["tabId"]}))
    status, body = open_tab(env)
    assert status == 409 and body["tab"]["tabId"] == first["tab"]["tabId"]


def test_guest_and_covers_come_from_the_arrived_booking(env):
    status, body = open_tab(env, table="t3", items=[])
    tab = body["tab"]
    assert tab["guestName"] == "Åsa Öberg" and tab["covers"] == 4 and tab["reservationId"] == "b" * 32
    status, body = open_tab(env, table="t2", items=[], guestName="Walk in", covers=2)
    assert body["tab"]["guestName"] == "Walk in" and body["tab"]["reservationId"] is None
    status, body = open_tab(env, table="t1", items=[], reservationId="c" * 32)
    assert status == 409 and body["error"] == "booking_not_found"


def test_menu_prices_and_unavailable_dishes(env):
    status, body = open_tab(env, items=[{"menuItemId": "gone", "quantity": 1}])
    assert status == 409 and body == {"error": "menu_item_unavailable", "menuItemId": "gone",
                                      "message": "Slut går inte att beställa just nu."}
    status, body = open_tab(env, items=[{"menuItemId": "steak", "quantity": 1}])
    assert status == 400 and body["error"] == "option_required" and body["message"] == "Välj tillval för Oxfilé."
    status, body = open_tab(env, items=[{"menuItemId": "cola", "quantity": 1, "price": 1}])
    assert status == 400 and body["error"] == "invalid_request"
    status, body = open_tab(env, items=[{"menuItemId": "water", "quantity": 2}, {"menuItemId": "ipa", "quantity": 1}])
    assert status == 201 and body["tab"]["referenceTotal"] == 8900
    ipa = next(i for i in body["tab"]["items"] if i["name"] == "IPA")
    assert ipa["course"] == "drinks" and ipa["stationId"] == "bar" and ipa["alcohol"] is True


def test_tables_and_floor_errors(env):
    status, body = open_tab(env, table="nope")
    assert status == 409 and body["error"] == "table_not_found"
    env.res.Table(LAYOUT).delete_item(Key={"PK": f"LOCATION#{LOC_A}", "SK": "LAYOUT#ACTIVATION"})
    status, body = open_tab(env)
    assert status == 409 and body["message"] == "Det finns inga bord. Rita och publicera bord i Planritning."
    status, body = call(env, "POST", body={"requestId": rid(), "tableId": "t1", "items": []}, role="staff_user")
    assert body["message"] == "Det finns inga bord. Be ägaren publicera bord i Planritning."


# --- add, fire, edit, void --------------------------------------------------------------

def test_add_fire_and_ticket_states(env):
    status, body = open_tab(env)
    tab_id = body["tab"]["tabId"]
    env.fake.states[tab_id] = {1: [{"stationId": "kok", "stationName": "Kök", "status": "ready"}]}
    status, body = call(env, "POST", f"{tab_id}/items", {"requestId": rid(), "items": [
        {"menuItemId": "cod", "quantity": 1}, {"menuItemId": "brulee", "quantity": 2}]})
    assert status == 200
    items = {i["name"]: i["state"] for i in body["tab"]["items"]}
    # starters are ready on the kitchen display, so the cod goes now; desserts wait for the mains
    assert items["Torskrygg"] == "sent" and items["Crème brûlée"] == "held"
    assert body["tab"]["rounds"][0]["tickets"][0]["status"] == "ready"
    status, body = call(env, "POST", f"{tab_id}/send", {"requestId": rid(), "courses": ["mains"]})
    assert status == 200 and body["round"]["kind"] == "fire" and body["round"]["courses"] == ["mains"]
    assert [i["state"] for i in body["tab"]["items"] if i["name"] == "Oxfilé"] == ["sent"]
    status, body = call(env, "POST", f"{tab_id}/send", {"requestId": rid(), "courses": ["mains"]})
    assert status == 200 and body["round"] is None
    status, body = call(env, "POST", f"{tab_id}/send", {"requestId": rid()})
    assert body["round"]["courses"] == ["desserts"] and [c[0] for c in env.fake.calls] == ["round"] * 4


def test_add_retries_when_another_waiter_wrote_in_between(env, monkeypatch):
    status, body = open_tab(env)
    tab_id = body["tab"]["tabId"]
    real = t.transact
    hits = []

    def racing(ops):
        if not hits:
            hits.append(1)
            env.res.Table(TABLE).update_item(Key=t.tab_key(LOC_A, TODAY, tab_id),
                                             UpdateExpression="SET version = version + :one",
                                             ExpressionAttributeValues={":one": 1})
        return real(ops)
    monkeypatch.setattr(t, "transact", racing)
    status, body = call(env, "POST", f"{tab_id}/items", {"requestId": rid(), "items": [
        {"menuItemId": "ipa", "quantity": 1}]})
    assert status == 200 and len(hits) == 1 and any(i["name"] == "IPA" for i in body["tab"]["items"])


def test_patch_edits_held_items_with_the_shown_version(env):
    status, body = open_tab(env)
    tab = body["tab"]
    steak = next(i for i in tab["items"] if i["state"] == "held")
    cola = next(i for i in tab["items"] if i["name"] == "Cola")
    status, body = call(env, "PATCH", tab["tabId"], {"requestId": rid(), "expectedVersion": tab["version"] - 1,
                                                     "covers": 3})
    assert status == 409 and body["error"] == "changed_retry" and body["tab"]["version"] == tab["version"]
    status, body = call(env, "PATCH", tab["tabId"], {"requestId": rid(), "expectedVersion": tab["version"],
                                                     "items": [{"itemId": cola["itemId"], "quantity": 5}]})
    assert status == 400
    status, body = call(env, "PATCH", tab["tabId"], {
        "requestId": rid(), "expectedVersion": tab["version"], "covers": 3, "note": "Firar",
        "items": [{"itemId": steak["itemId"], "quantity": 3, "note": "en utan sås"}]})
    assert status == 200 and body["tab"]["covers"] == 3 and body["tab"]["note"] == "Firar"
    assert next(i for i in body["tab"]["items"] if i["itemId"] == steak["itemId"])["quantity"] == 3
    status, body = call(env, "PATCH", tab["tabId"], {"requestId": rid(), "expectedVersion": body["tab"]["version"],
                                                     "items": [{"itemId": steak["itemId"], "hold": False}]})
    assert status == 200 and body["round"]["kind"] == "fire" and body["tab"]["referenceTotal"] == 9000 + 14500 + 3 * 33500


def test_void_part_of_a_sent_item(env):
    status, body = open_tab(env)
    tab = body["tab"]
    cola = next(i for i in tab["items"] if i["name"] == "Cola")
    status, body = call(env, "POST", f"{tab['tabId']}/void", {
        "requestId": rid(), "expectedVersion": tab["version"], "items": [{"itemId": cola["itemId"], "quantity": 1}],
        "reason": "wrong"})
    assert status == 200 and body["round"]["kind"] == "void" and env.fake.calls[-1][0] == "void"
    assert body["tab"]["referenceTotal"] == tab["referenceTotal"] - 4500
    status, body = call(env, "POST", f"{tab['tabId']}/void", {
        "requestId": rid(), "expectedVersion": tab["version"], "items": [{"itemId": cola["itemId"], "quantity": 1}],
        "reason": "wrong"})
    assert status == 409 and body["error"] == "changed_retry"


# --- move, close, reopen ---------------------------------------------------------------

def test_move_to_a_free_table_and_not_to_an_occupied_one(env):
    status, body = open_tab(env)
    tab = body["tab"]
    open_tab(env, table="t2", items=[])
    status, body = call(env, "POST", f"{tab['tabId']}/move", {"requestId": rid(), "expectedVersion": tab["version"],
                                                               "tableId": "t2"})
    assert status == 409 and body["error"] == "table_has_open_order" and body["message"].startswith("Bord 2")
    status, body = call(env, "POST", f"{tab['tabId']}/move", {"requestId": rid(), "expectedVersion": tab["version"],
                                                               "tableId": "t3"})
    assert status == 200 and body["tab"]["tableId"] == "t3" and body["round"]["kind"] == "move"
    assert body["round"]["fromLabel"] == "1" and body["round"]["toLabel"] == "3"
    assert env.fake.calls[-1][0] == "move"
    status, listing = call(env, "GET")
    by_table = {x["tableId"]: x["tabId"] for x in listing["tables"]}
    assert by_table["t1"] is None and by_table["t3"] == tab["tabId"]
    assert open_tab(env, table="t1", items=[])[0] == 201


def test_close_refuses_held_items_then_closes_and_frees_the_table(env):
    status, body = open_tab(env)
    tab = body["tab"]
    status, body = call(env, "POST", f"{tab['tabId']}/close", {"requestId": rid(), "expectedVersion": tab["version"]})
    assert status == 409 and body["error"] == "held_items" and body["tab"]["tabId"] == tab["tabId"]
    close_body = {"requestId": rid(), "expectedVersion": tab["version"], "dropHeld": True}
    status, body = call(env, "POST", f"{tab['tabId']}/close", close_body)
    assert status == 200 and body["tab"]["status"] == "closed" and body["tab"]["closeReason"] == "paid_elsewhere"
    assert body["tab"]["closedByName"] == "Owner"
    again = call(env, "POST", f"{tab['tabId']}/close", close_body)
    assert again[0] == 200 and again[1]["replayed"] is True
    status, body = call(env, "POST", f"{tab['tabId']}/close", {"requestId": rid(), "expectedVersion": 99})
    assert status == 409
    status, listing = call(env, "GET")
    assert listing["tabs"] == [] and listing["closedToday"][0]["tabId"] == tab["tabId"]
    status, closed = call(env, "GET", "closed", query={"date": TODAY})
    assert [x["tabId"] for x in closed["tabs"]] == [tab["tabId"]]
    status, body = call(env, "POST", f"{tab['tabId']}/items", {"requestId": rid(), "items": [
        {"menuItemId": "cola", "quantity": 1}]})
    assert status == 409 and body["error"] == "tab_closed"
    row = env.res.Table(TABLE).get_item(Key=t.tab_key(LOC_A, TODAY, tab["tabId"]))["Item"]
    assert int(row["ttl"]) == t.epoch(NOW + timedelta(days=14))


def test_reopen_within_15_minutes_and_only_on_a_free_table(env):
    status, body = open_tab(env, items=[{"menuItemId": "cola", "quantity": 1}])
    tab = body["tab"]
    call(env, "POST", f"{tab['tabId']}/close", {"requestId": rid(), "expectedVersion": tab["version"]})
    env.clock["now"] = NOW + timedelta(minutes=5)
    status, body = call(env, "POST", f"{tab['tabId']}/reopen", {"requestId": rid()})
    assert status == 200 and body["tab"]["status"] == "open"
    call(env, "POST", f"{tab['tabId']}/close", {"requestId": rid(), "expectedVersion": body["tab"]["version"]})
    status, other = open_tab(env, items=[])
    assert status == 201
    status, body = call(env, "POST", f"{tab['tabId']}/reopen", {"requestId": rid()})
    assert status == 409 and body["error"] == "table_has_open_order"
    env.clock["now"] = NOW + timedelta(minutes=30)
    status, body = call(env, "POST", f"{tab['tabId']}/reopen", {"requestId": rid()})
    assert status == 409 and body["error"] == "reopen_expired"


# --- dispatch failures --------------------------------------------------------------------

def test_a_failed_dispatch_is_sent_again_by_retry_or_button(env):
    env.fake.fail = True
    body = {"requestId": rid(), "tableId": "t1", "items": [{"menuItemId": "cola", "quantity": 1}]}
    status, first = call(env, "POST", body=body)
    assert status == 201 and first["round"]["dispatched"] is False
    assert first["warnings"] == [{"code": "dispatch_failed", "text": "Bongen kunde inte skickas. Tryck Skicka igen."}]
    env.fake.fail = False
    status, again = call(env, "POST", body=body)
    assert again["replayed"] is True and again["round"]["dispatched"] is True
    status, button = call(env, "POST", f"{first['tab']['tabId']}/rounds/1/dispatch", {})
    assert status == 200 and button["round"]["dispatched"] is True and len(env.fake.calls) == 2
    assert call(env, "POST", f"{first['tab']['tabId']}/rounds/9/dispatch", {})[0] == 404


def test_bar_without_printer_warns_who_acts(env):
    env.fake.no_printer = True
    status, body = open_tab(env, items=[{"menuItemId": "ipa", "quantity": 1}])
    assert body["warnings"] == [{"code": "station_no_printer", "stationId": "bar",
                                 "text": "Bar har ingen skrivare, bongen skrevs ut i köket."}]


def test_without_the_ticket_module_orders_still_save(env, monkeypatch):
    monkeypatch.setitem(sys.modules, "shared.tab_tickets", None)
    status, body = open_tab(env, items=[{"menuItemId": "cola", "quantity": 1}])
    assert status == 201 and body["round"]["dispatched"] is False and body["warnings"][0]["code"] == "dispatch_failed"
    status, menu = call(env, "GET", "menu")
    assert status == 200 and menu["stations"] == [] and {i["menuItemId"] for i in menu["items"]} >= {"cola", "ipa"}


# --- reading ----------------------------------------------------------------------------

def test_list_polls_cheaply_and_shows_tables_and_guests(env):
    status, listing = call(env, "GET")
    assert status == 200 and listing["version"] == 0 and listing["tabs"] == []
    assert [x["label"] for x in listing["tables"]] == ["1", "2", "3", "Baren"]
    guest = next(x for x in listing["tables"] if x["tableId"] == "t3")["guest"]
    assert guest["name"] == "Åsa Öberg" and guest["partySize"] == 4 and guest["status"] == "arrived"
    assert call(env, "GET", query={"version": "0"})[1] == {"unchanged": True, "version": 0}
    open_tab(env)
    status, listing = call(env, "GET", query={"version": "0"})
    assert listing["version"] == 2 and len(listing["tabs"]) == 1 and listing["serverTime"] == "2026-11-19T17:00:00Z"
    assert next(x for x in listing["tables"] if x["tableId"] == "t1")["tabId"] == listing["tabs"][0]["tabId"]


def test_menu_lists_available_dishes_with_course(env):
    status, body = call(env, "GET", "menu")
    assert status == 200
    assert "gone" not in {i["menuItemId"] for i in body["items"]}
    assert {c["categoryId"]: c["course"] for c in body["categories"]}["beer"] == "drinks"


def test_get_one_tab_and_unknown_ids(env):
    status, body = open_tab(env)
    tab_id = body["tab"]["tabId"]
    status, got = call(env, "GET", tab_id)
    assert status == 200 and got["tab"]["tabId"] == tab_id and got["tab"]["rounds"][0]["tickets"] == []
    assert call(env, "GET", "f" * 32)[0] == 404
    assert call(env, "GET", "not-a-tab")[0] == 404
    assert call(env, "DELETE", tab_id)[0] == 405
    assert call(env, "GET", f"{tab_id}/close")[0] == 405
    status, body = call(env, "POST", f"{tab_id}/items", {"items": [{"menuItemId": "cola", "quantity": 1}]})
    assert status == 400 and body["message"] == t.MESSAGES["invalid_request"]


# --- access ------------------------------------------------------------------------------

def test_staff_only_on_their_location_and_other_tenants_see_nothing(env):
    assert call(env, "GET", role="staff_user")[0] == 200
    assert call(env, "GET", role="staff_user", location=LOC_A2)[0] == 404
    assert call(env, "GET", location=LOC_B)[0] == 404
    assert call(env, "GET", tenant=TENANT_B, location=LOC_A)[0] == 404
    status, body = open_tab(env)
    status, other = call(env, "GET", body["tab"]["tabId"], location=LOC_A2)
    assert status == 404


def test_the_ordering_feature_is_required(env):
    env.res.Table(TENANT_TABLE).put_item(Item=tenant_row(TENANT_A, features={**features_on(), "ordering": False}))
    shared_tenant.reset_caches()
    status, body = call(env, "GET")
    assert status == 403

"""Kitchen ticket routing (shared/routing.py): stations, combo split, drinks
kept off the kitchen ticket, the allergy ticket, several printers per
station, the kitchen display API, and a simulated Star (CloudPRNT) and
Epson (Server Direct Print) printer. DynamoDB is moto (test_printing's env)."""

import base64
import json
from urllib.parse import urlencode

from shared import print_ticket as t
from shared import printing as p
from shared import routing as r
from test_printing import LOC_A, ORDER_ID, api, device_call, env, order, stream  # noqa: F401 - env is a fixture

LINES = [
    {"name": "Burgarmeny", "quantity": 1, "category": "mains", "allergy": "jordnötsallergi", "note": "ingen lök",
     "components": [{"name": "Burgare"}, {"name": "Pommes"}, {"name": "Läsk", "station": "bar"}]},
    {"name": "Toast Skagen", "quantity": 2, "category": "starters"},
    {"name": "Mojito", "quantity": 1, "category": "drinks"},
    {"name": "Coca-Cola", "quantity": 2, "category": "drinks", "station": "none"},
]


def demo_order(**extra):
    return order(lines=LINES, notes=None, mode="table", tableLabel="7", **extra)


def names(entries):
    return [e["name"] for e in entries]


# --- routing (pure) ----------------------------------------------------------------------

def test_combo_splits_drinks_leave_the_kitchen_and_none_makes_no_ticket():
    split = r.split(LINES, r.default_config())
    assert list(split) == ["kok", "bar"]
    assert names(split["kok"]) == ["Burgare", "Pommes", "Toast Skagen"]
    assert names(split["bar"]) == ["Läsk", "Mojito"]
    assert all("Coca-Cola" not in names(v) for v in split.values())
    burger = split["kok"][0]
    assert burger == {"name": "Burgare", "quantity": 1, "course": "mains", "partOf": "Burgarmeny",
                      "note": "ingen lök", "allergy": "jordnötsallergi"}


def test_removed_station_falls_back_and_category_can_be_none():
    config = {**r.default_config(), "categories": {"starters": "kok", "mains": "kok", "desserts": "kok",
                                                   "drinks": "none"}}
    split = r.split([{"name": "Lax", "category": "mains", "station": "gone"},
                     {"name": "Öl", "category": "drinks"}, {"name": "Okänd"}], config)
    assert split == {"kok": [{"name": "Lax", "quantity": 1, "course": "mains"},
                             {"name": "Okänd", "quantity": 1, "course": None}]}


def test_tickets_flag_allergies_from_lines_and_order_notes():
    now = p.now_utc()
    tickets = r.tickets(ORDER_ID, {"orderId": ORDER_ID, "number": 4}, LINES[1:3], None, r.default_config(), now)
    assert [(x["stationId"], x["allergy"]) for x in tickets] == [("kok", False), ("bar", False)]
    noted = r.tickets(ORDER_ID, {}, LINES[1:3], "Gästen har nötallergi", r.default_config(), now)
    assert all(x["allergy"] for x in noted)
    assert r.mentions_allergy("Glutenfri tack") and not r.mentions_allergy("Ingen lök")


def test_config_validation():
    devices = {"d" * 32}
    ok = r.validate_config({"stations": [{"name": "Kök", "printerIds": ["d" * 32]}, {"stationId": "bar", "name": "Bar"}],
                            "categories": {"drinks": "bar"}, "allergyTicket": True, "version": 0}, devices)
    assert ok["stations"][0]["stationId"].startswith("s") and ok["categories"]["mains"] == ok["stations"][0]["stationId"]
    for bad in ({"stations": []}, {"stations": [{"name": "Kök", "printerIds": ["x"]}]},
                {"stations": [{"name": "Kök"}, {"name": "kök"}]}, {"categories": {"drinks": "nope"}},
                {"stations": [{"stationId": "none", "name": "X"}]}):
        try:
            r.validate_config({"stations": [{"name": "Kök"}], "categories": {}, "allergyTicket": True, "version": 0,
                               **bad}, devices)
        except ValueError:
            continue
        raise AssertionError(bad)


# --- tickets as printed ----------------------------------------------------------------------

def test_kitchen_ticket_has_no_drinks_and_allergies_in_bold():
    ticket = r.tickets(ORDER_ID, p.order_header(demo_order()), LINES, None, r.default_config(), p.now_utc())[0]
    out = t.station_ticket(ticket, demo_order(), {"timezone": "Europe/Stockholm"})
    text = out.decode("cp1252")
    assert "KÖK" in text and "BORD 7" in text and "#12" in text
    assert "-- FÖRRÄTT --" in text and "-- VARMRÄTT --" in text
    assert text.index("FÖRRÄTT") < text.index("VARMRÄTT")
    assert "1 x Burgare" in text and "(Burgarmeny)" in text and "2 x Toast Skagen" in text
    assert "Läsk" not in text and "Mojito" not in text and "Coca-Cola" not in text
    assert b"\x1bEALLERGI: jordn\xf6tsallergi" in out and b"\x1bE     ingen l\xf6k" in out
    allergy = t.station_ticket(ticket, demo_order(), {}, allergy=True).decode("cp1252")
    assert "!! ALLERGI !!" in allergy and "Burgare" in allergy and "Toast Skagen" not in allergy
    assert "Kontrollera allergin" in allergy


def test_epson_ticket_is_epos_xml():
    ticket = r.tickets(ORDER_ID, p.order_header(demo_order()), LINES, None, r.default_config(), p.now_utc())[1]
    xml = t.station_ticket(ticket, None, {}, fmt="epson")
    assert xml.startswith('<epos-print xmlns="http://www.epson-pos.com/schemas/2011/03/epos-print">')
    assert '<text align="center" dw="true" dh="true" em="true">BAR&#10;</text>' in xml
    assert "Läsk" in xml and "Burgare" not in xml and xml.endswith('<cut type="feed"/></epos-print>')
    assert "&lt;" in t.station_ticket({**ticket, "notes": "<b>"}, None, {}, fmt="epson")


# --- end to end: several printers, Star and Epson ---------------------------------------------------

def add(env, name, protocol="star"):
    status, body = api(env, "POST", "devices", {"name": name, "protocol": protocol})
    assert status == 201
    return body["device"]["deviceId"], body["secret"], body["device"]


def setup_stations(env):
    k1, k1s, _ = add(env, "Kök 1")
    k2, k2s, _ = add(env, "Kök 2")
    bar, bars, bar_view = add(env, "Bar", "epson")
    assert bar_view["serverUrl"] == f"https://api.example/print/epson/{bar}" and bar_view["protocol"] == "epson"
    api(env, "PUT", "settings", {**p.get_settings(LOC_A), "autoPrint": True})
    status, current = api(env, "GET", "routing")
    assert status == 200 and [s["stationId"] for s in current["stations"]] == ["kok", "bar"]
    status, saved = api(env, "PUT", "routing", {**current, "stations": [
        {"stationId": "kok", "name": "Kök", "printerIds": [k1, k2]},
        {"stationId": "bar", "name": "Bar", "printerIds": [bar]}]})
    assert status == 200 and saved["version"] == 1
    assert api(env, "PUT", "routing", {**current, "version": 0})[0] == 409
    assert api(env, "PUT", "routing", current, role="staff_user")[0] == 403
    return (k1, k1s), (k2, k2s), (bar, bars)


def star_print(env, device_id, secret):
    """A Star CloudPRNT printer: poll, fetch, confirm. -> ticket text or None."""
    poll = json.loads(device_call(env, "POST", device_id, secret, body={})["body"])
    if not poll["jobReady"]:
        return None
    got = device_call(env, "GET", device_id, secret, query={"type": t.MEDIA_TYPE, "token": poll["jobToken"]})
    device_call(env, "DELETE", device_id, secret, query={"code": "200 OK", "token": poll["jobToken"]})
    return base64.b64decode(got["body"]).decode("cp1252")


def epson_call(env, device_id, secret, form):
    event = {"routeKey": "POST /print/epson/{deviceId}", "pathParameters": {"deviceId": device_id},
             "headers": {"Authorization": "Basic " + base64.b64encode(f"{device_id}:{secret}".encode()).decode(),
                         "content-type": "application/x-www-form-urlencoded"},
             "body": urlencode(form), "requestContext": {"http": {"method": "POST", "sourceIp": "198.51.100.7"}}}
    return env["device"].handler(event, None)


def epson_print(env, device_id, secret, success=True):
    """An Epson printer with Server Direct Print: GetRequest, then SetResponse."""
    got = epson_call(env, device_id, secret, {"ConnectionType": "GetRequest", "ID": device_id, "Name": "TM-m30III"})
    assert got["statusCode"] == 200 and got["headers"]["Content-Type"].startswith("text/xml")
    if not got["body"]:
        return None
    job_id = got["body"].split("<printjobid>")[1].split("</printjobid>")[0]
    response = ('<?xml version="1.0" encoding="utf-8"?><PrintResponseInfo Version="2.00"><ePOSPrint><Parameter>'
                f'<devid>local_printer</devid><printjobid>{job_id}</printjobid></Parameter><PrintResponse>'
                f'<response success="{str(success).lower()}" code="" status="251658262" battery="0"/>'
                '</PrintResponse></ePOSPrint></PrintResponseInfo>')
    assert epson_call(env, device_id, secret, {"ConnectionType": "SetResponse", "ID": device_id,
                                               "ResponseFile": response})["statusCode"] == 200
    return got["body"]


def drain(fn, *args):
    out = []
    while (doc := fn(*args)) is not None:
        out.append(doc)
    return out


def test_demo_order_prints_kitchen_bar_and_allergy_tickets_on_every_station_printer(env):
    (k1, k1s), (k2, k2s), (bar, bars) = setup_stations(env)
    env["res"].Table("test-order").put_item(Item=demo_order())
    stream(env, demo_order(status="pending_payment"), demo_order())
    stream(env, demo_order(status="pending_payment"), demo_order())  # redelivered: nothing twice

    for device_id, secret in ((k1, k1s), (k2, k2s)):
        docs = drain(star_print, env, device_id, secret)
        assert len(docs) == 2
        kitchen = next(d for d in docs if "!! ALLERGI !!" not in d)
        allergy = next(d for d in docs if "!! ALLERGI !!" in d)
        assert "Burgare" in kitchen and "Pommes" in kitchen and "Toast Skagen" in kitchen
        assert "Läsk" not in kitchen and "Mojito" not in kitchen and "Coca-Cola" not in kitchen
        assert "ALLERGI: jordnötsallergi" in allergy and "BORD 7" in allergy
    bar_docs = drain(epson_print, env, bar, bars)
    assert len(bar_docs) == 2  # the Läsk is part of the allergy combo: the bar gets its allergy ticket too
    assert all("Läsk" in d and "Burgare" not in d for d in bar_docs) and any("Mojito" in d for d in bar_docs)
    assert all(j["status"] == "PRINTED" for j in p.order_jobs(LOC_A, ORDER_ID))

    # one full order for staff, each station's state on it
    status, summary = api(env, "GET", "status", role="staff_user")
    assert status == 200 and sorted(x["stationId"] for x in summary["tickets"][ORDER_ID]) == ["bar", "kok"]
    assert all(x["status"] == "sent" and x["allergy"] for x in summary["tickets"][ORDER_ID])


def test_epson_failure_backs_off_and_unknown_connection_type(env):
    _, _, (bar, bars) = setup_stations(env)
    stream(env, demo_order(status="pending_payment"), demo_order())
    epson_print(env, bar, bars, success=False)
    jobs = [j for j in p.order_jobs(LOC_A, ORDER_ID) if j.get("deviceId") == bar]
    assert sorted(j["status"] for j in jobs) == ["PENDING", "PENDING"] and max(j["attempts"] for j in jobs) == 1
    assert epson_call(env, bar, bars, {"ConnectionType": "Nope"})["statusCode"] == 400
    assert epson_call(env, bar, "wrong", {"ConnectionType": "GetRequest"})["statusCode"] == 401


def test_no_printers_still_makes_kitchen_display_tickets(env):
    stream(env, demo_order(status="pending_payment"), demo_order())
    assert p.order_jobs(LOC_A, ORDER_ID) == []
    status, body = api(env, "GET", "tickets", role="staff_user")
    assert status == 200 and sorted(x["stationName"] for x in body["tickets"]) == ["Bar", "Kök"]
    assert body["tickets"][0]["tableLabel"] == "7" and body["tickets"][0]["number"] == 12


def test_kitchen_display_status_reprint_and_cancel(env):
    (k1, k1s), (k2, k2s), (bar, bars) = setup_stations(env)
    env["res"].Table("test-order").put_item(Item=demo_order())
    stream(env, demo_order(status="pending_payment"), demo_order())
    drain(star_print, env, k1, k1s)
    status, ticket = api(env, "PUT", f"tickets/{ORDER_ID}/kok", {"status": "preparing"}, role="staff_user")
    assert status == 200 and ticket["status"] == "preparing"
    assert api(env, "PUT", f"tickets/{ORDER_ID}/kok", {"status": "eaten"})[0] == 400
    assert api(env, "PUT", f"tickets/{ORDER_ID}/nope", {"status": "ready"})[0] == 404
    statuses = {x["stationId"]: x["status"] for x in api(env, "GET", "status")[1]["tickets"][ORDER_ID]}
    assert statuses == {"kok": "preparing", "bar": "sent"}

    # reprint one station: only its printers, marked KOPIA
    status, body = api(env, "POST", "jobs", {"orderId": ORDER_ID, "stationId": "bar"})
    assert status == 202 and len(body["jobIds"]) == 2  # ticket + allergy ticket on the bar printer
    assert star_print(env, k1, k1s) is None
    bar_docs = drain(epson_print, env, bar, bars)
    assert len(bar_docs) == 4 and sum("KOPIA" in d for d in bar_docs) == 2
    assert api(env, "POST", f"tickets/{ORDER_ID}/kok/print")[0] == 202
    assert len(drain(star_print, env, k2, k2s)) == 4  # its first two, then the two copies

    # cancelled: tickets cancelled, open jobs dropped, a MAKULERAD slip where one was printed
    stream(env, demo_order(), demo_order(status="cancelled"))
    statuses = {x["status"] for x in api(env, "GET", "status")[1]["tickets"][ORDER_ID]}
    assert statuses == {"cancelled"}
    slips = drain(star_print, env, k1, k1s)
    assert len(slips) == 1 and "MAKULERAD #12" in slips[0]


def test_removed_printer_leaves_its_station(env):
    (k1, _), (k2, _), _ = setup_stations(env)
    api(env, "DELETE", f"devices/{k2}")
    assert api(env, "GET", "routing")[1]["stations"][0]["printerIds"] == [k1]
    assert r.get_config(LOC_A)["version"] == 2

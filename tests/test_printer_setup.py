"""Printer setup (Kom igång, "Kvittoskrivare"): Kök/Dryck kinds drive the
stations, "Inga skrivare än", and the simulated printer that exists only
outside prod. DynamoDB is moto (fixtures from test_printing)."""

import json

from shared import printing as p
from shared import routing
from test_printing import LOC_A, NOW, ORDER_ID, api, device_call, env, order, stream  # noqa: F401 - env is a fixture

LINES = [{"name": "Pizza", "quantity": 1, "category": "mains"},
         {"name": "Cola", "quantity": 2, "category": "drinks"},
         {"name": "IPA", "quantity": 1, "category": "olvin"}]


def add(env, name, kind, protocol="star"):
    status, body = api(env, "POST", "devices", {"name": name, "kind": kind, "protocol": protocol})
    assert status == 201, body
    return body


def stations(env):
    status, config = api(env, "GET", "routing")
    assert status == 200
    return {s["stationId"]: (s["name"], s["printerIds"]) for s in config["stations"]}


def paid(env):
    stream(env, order(status="pending_payment", lines=LINES), order(lines=LINES))


def jobs_by_device():
    out = {}
    for j in p.order_jobs(LOC_A, ORDER_ID):
        out.setdefault(j["deviceId"], []).append(j["SK"].split("#")[2].split("-")[1])
    return {k: sorted(v) for k, v in out.items()}


# --- kinds -> stations ------------------------------------------------------------------

def _config():
    return routing.default_config()


def test_kinds_both_kok_and_dryck():
    out = routing.kinds_config(_config(), [{"deviceId": "k", "kind": "kok"}, {"deviceId": "d", "kind": "dryck"}])
    st = {s["stationId"]: s for s in out["stations"]}
    assert st["kok"]["printerIds"] == ["k"] and st["bar"]["printerIds"] == ["d"]
    assert st["bar"]["name"] == "Dryck" and out["categories"]["drinks"] == "bar"


def test_kinds_only_kok_takes_the_drinks_too():
    out = routing.kinds_config(_config(), [{"deviceId": "k1", "kind": "kok"}, {"deviceId": "k2", "kind": "kok"}])
    st = {s["stationId"]: s for s in out["stations"]}
    assert st["kok"]["printerIds"] == ["k1", "k2"] and st["bar"]["printerIds"] == ["k1", "k2"]


def test_kinds_only_dryck_takes_the_food_too():
    out = routing.kinds_config(_config(), [{"deviceId": "d", "kind": "dryck"}])
    st = {s["stationId"]: s for s in out["stations"]}
    assert st["kok"]["printerIds"] == ["d"] and st["bar"]["printerIds"] == ["d"]


def test_kinds_none_changes_nothing():
    assert routing.kinds_config(_config(), []) is None


def test_printer_from_before_kinds_keeps_printing_where_it_did():
    config = _config()
    config["stations"][0]["printerIds"] = ["old"]
    config["stations"][1]["printerIds"] = ["bar1"]
    assert routing.infer_kind({"deviceId": "old"}, config) == "kok"
    assert routing.infer_kind({"deviceId": "bar1"}, config) == "dryck"
    out = routing.kinds_config(config, [{"deviceId": "old"}, {"deviceId": "bar1"}, {"deviceId": "k", "kind": "kok"}])
    st = {s["stationId"]: s for s in out["stations"]}
    assert st["kok"]["printerIds"] == ["old", "k"] and st["bar"]["printerIds"] == ["bar1"]


def test_owner_stations_from_before_stay():
    config = {**_config(), "stations": [*_config()["stations"], {"stationId": "s1", "name": "Pizza", "printerIds": ["k"]}]}
    out = routing.kinds_config(config, [{"deviceId": "k", "kind": "kok"}])
    st = {s["stationId"]: s for s in out["stations"]}
    assert st["s1"]["printerIds"] == ["k"]


def test_kinds_put_back_missing_stations():
    config = {**_config(), "stations": [{"stationId": "s1", "name": "Pass", "printerIds": []}],
              "categories": {"starters": "s1", "mains": "s1", "desserts": "s1", "drinks": "s1"}}
    out = routing.kinds_config(config, [{"deviceId": "d", "kind": "dryck"}])
    assert [s["stationId"] for s in out["stations"]] == ["kok", "bar", "s1"]
    assert out["categories"]["drinks"] == "bar" and out["categories"]["mains"] == "s1"


def test_annat_printer_gets_its_own_station_and_leaves_with_it():
    devices = [{"deviceId": "k" * 32, "kind": "kok"}, {"deviceId": "0123456789abcdef", "kind": "annat", "name": "Pizzaugn"}]
    out = routing.kinds_config(_config(), devices)
    st = {s["stationId"]: s for s in out["stations"]}
    assert st["a0123456789ab"] == {"stationId": "a0123456789ab", "name": "Pizzaugn", "printerIds": ["0123456789abcdef"]}
    assert st["kok"]["printerIds"] == ["k" * 32] and st["bar"]["printerIds"] == ["k" * 32]
    out["categories"]["pizza"] = "a0123456789ab"
    gone = routing.kinds_config(out, devices[:1])
    assert "a0123456789ab" not in {s["stationId"] for s in gone["stations"]}
    assert "pizza" not in gone["categories"]  # back to the default (Kök)


def _lines_config(stations):
    config = _config()
    config["stations"] = [{"stationId": sid, "name": sid, "printerIds": []} for sid in stations]
    config["categories"] = {k: v for k, v in config["categories"].items() if v in stations}
    return config


def test_custom_categories_without_a_choice_never_put_drinks_on_the_kitchen_ticket():
    lines = [{"name": "Burgare", "category": "c1a2b3c4d5"}, {"name": "IPA", "category": "c9f8e7d6c5", "alcohol": True},
             {"name": "Cola", "category": "drinks"}, {"name": "Glass", "category": "desserts"}]
    out = routing.split(lines, routing.default_config())
    assert {k: [e["name"] for e in v] for k, v in out.items()} == {"kok": ["Burgare", "Glass"], "bar": ["IPA", "Cola"]}


def test_none_means_no_ticket():
    config = routing.default_config()
    config["categories"]["drinks"] = routing.NONE
    out = routing.split([{"name": "Cola", "category": "drinks"}, {"name": "Soppa", "category": "starters",
                                                                   "station": "none"}], config)
    assert out == {}


# --- API --------------------------------------------------------------------------------

def test_kok_and_dryck_printers_get_their_tickets(env):
    kok = add(env, "Köket", "kok")["device"]["deviceId"]
    bar = add(env, "Baren", "dryck")["device"]["deviceId"]
    assert stations(env) == {"kok": ("Kök", [kok]), "bar": ("Dryck", [bar])}
    api(env, "PUT", "settings", {**p.get_settings(LOC_A), "autoPrint": True})
    paid(env)
    assert jobs_by_device() == {kok: ["kok"], bar: ["bar"]}


def test_changing_the_kind_moves_the_tickets(env):
    one = add(env, "Köket", "kok")["device"]["deviceId"]
    two = add(env, "Baren", "kok")["device"]["deviceId"]
    assert stations(env)["bar"][1] == [one, two]
    status, overview = api(env, "PUT", f"devices/{two}", {"kind": "dryck", "name": "Bar"})
    assert status == 200
    assert stations(env) == {"kok": ("Kök", [one]), "bar": ("Dryck", [two])}
    view = next(d for d in overview["devices"] if d["deviceId"] == two)
    assert view["kind"] == "dryck" and view["name"] == "Bar"
    assert api(env, "PUT", f"devices/{two}", {"kind": "kassa"})[0] == 400
    assert api(env, "PUT", f"devices/{two}", {})[0] == 400
    assert api(env, "PUT", f"devices/{two}", {"kind": "kok"}, role="staff_user")[0] == 403


def test_removing_the_last_dryck_printer_sends_drinks_to_kok(env):
    kok = add(env, "Köket", "kok")["device"]["deviceId"]
    bar = add(env, "Baren", "dryck")["device"]["deviceId"]
    assert api(env, "DELETE", f"devices/{bar}")[0] == 200
    assert stations(env) == {"kok": ("Kök", [kok]), "bar": ("Dryck", [kok])}


def test_custom_menu_category_can_be_routed(env):
    kok = add(env, "Köket", "kok")["device"]["deviceId"]
    bar = add(env, "Baren", "dryck")["device"]["deviceId"]
    _, config = api(env, "GET", "routing")
    status, saved = api(env, "PUT", "routing", {**config, "categories": {**config["categories"], "olvin": "bar"}})
    assert status == 200 and saved["categories"]["olvin"] == "bar"
    assert api(env, "PUT", "routing", {**saved, "categories": {"Öl!": "bar"}})[0] == 400
    api(env, "PUT", "settings", {**p.get_settings(LOC_A), "autoPrint": True})
    paid(env)
    assert jobs_by_device() == {kok: ["kok"], bar: ["bar"]}
    tickets = {t["stationId"]: [e["name"] for e in t["entries"]] for t in routing.source_tickets(LOC_A, ORDER_ID)}
    assert tickets == {"kok": ["Pizza"], "bar": ["Cola", "IPA"]}


def test_no_printers_yet_keeps_the_step_open(env):
    _, status = api(env, "GET", "status")
    assert status["printerCount"] == 0 and status["setupSkipped"] is False
    assert api(env, "POST", "setup", {"skipped": True}, role="staff_user")[0] == 403
    code, status = api(env, "POST", "setup", {"skipped": True})
    assert code == 200 and status["setupSkipped"] is True and status["printerCount"] == 0
    _, overview = api(env, "GET", "")
    assert "setupSkipped" not in overview["settings"]  # never sent back with a settings save
    add(env, "Köket", "kok")
    _, status = api(env, "GET", "status")
    assert status["printerCount"] == 1
    assert api(env, "POST", "setup", {"skipped": "yes"})[0] == 400
    assert api(env, "POST", "setup", {"skipped": False})[1]["setupSkipped"] is False


# --- simulated printer ------------------------------------------------------------------

def test_simulated_printer_prints_on_screen_in_dev(env):
    _, overview = api(env, "GET", "")
    assert overview["simulatorAllowed"] is True
    body = add(env, "Testskrivare", "kok", "sim")
    sim = body["device"]["deviceId"]
    assert "secret" not in body and body["device"]["simulated"] is True and body["device"]["online"] is True
    assert "serverUrl" not in body["device"]
    assert api(env, "POST", f"devices/{sim}/rotate")[0] == 409
    api(env, "PUT", "settings", {**p.get_settings(LOC_A), "autoPrint": True})
    paid(env)
    jobs = p.order_jobs(LOC_A, ORDER_ID)
    assert jobs and all(j["status"] == p.PRINTED and "queue" not in j for j in jobs)
    assert p.queue(LOC_A) == []
    assert api(env, "POST", f"devices/{sim}/test")[0] == 202
    code, paper = api(env, "GET", f"devices/{sim}/simulated")
    # one Kök printer only: it prints both the Kök and the Dryck ticket
    assert code == 200 and sorted(t["kind"] for t in paper["tickets"]) == ["station", "station", "test"]
    text = [row["text"] for t in paper["tickets"] for row in t["lines"]]
    assert "Köksnota, ej kvitto" in text and "1 x Pizza" in text and "2 x Cola" in text and "TESTUTSKRIFT" in text
    assert "DRYCK" in text and "KÖK" in text
    assert any(r.get("big") for t in paper["tickets"] for r in t["lines"])
    # the order's guest data is rendered now, never stored on the sim row
    assert all("customerPhone" not in json.dumps(r, default=str) for r in p._table().scan()["Items"] if r["SK"].startswith("SIM#"))
    # cancelling: a MAKULERAD slip lands on the simulated paper too
    env["clock"]["now"] = NOW.replace(minute=40)
    stream(env, order(lines=LINES), order(lines=LINES, status="cancelled"))
    kinds = [t["kind"] for t in api(env, "GET", f"devices/{sim}/simulated")[1]["tickets"]]
    assert kinds[0] == "cancel" and len(kinds) == 4
    assert p.sweep(NOW)["stuck"] == 0 and p.sweep(NOW)["failed"] == 0


def test_simulated_printer_cannot_log_in_as_a_printer(env):
    sim = add(env, "Testskrivare", "dryck", "sim")["device"]["deviceId"]
    resp = device_call(env, "POST", sim, "", body={"printerMAC": "00:11:62:aa:bb:cc"})
    assert resp["statusCode"] == 401


def test_real_printer_has_no_simulated_paper(env):
    real = add(env, "Köket", "kok")
    assert real["secret"] and real["device"]["serverUrl"].endswith(real["device"]["deviceId"])
    assert api(env, "GET", f"devices/{real['device']['deviceId']}/simulated")[0] == 404


def test_prod_refuses_simulated_printers(env, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    status, body = api(env, "POST", "devices", {"name": "Test", "kind": "kok", "protocol": "sim"})
    assert status == 400
    assert api(env, "GET", "")[1]["simulatorAllowed"] is False
    monkeypatch.delenv("ENVIRONMENT")
    assert p.simulator_allowed() is False  # unset counts as prod
    assert api(env, "POST", "devices", {"name": "Test", "kind": "kok", "protocol": "sim"})[0] == 400
    assert api(env, "POST", "devices", {"name": "Köket", "kind": "kok", "protocol": "star"})[0] == 201


# --- routing: Kök only, Dryck only, both, none ------------------------------------------

def test_only_kok_prints_food_and_drinks_on_kok(env):
    kok = add(env, "Köket", "kok")["device"]["deviceId"]
    assert p.get_settings(LOC_A)["autoPrint"] is True  # the first printer from setup starts printing
    paid(env)
    assert jobs_by_device() == {kok: ["bar", "kok"]}


def test_only_dryck_prints_everything_there(env):
    bar = add(env, "Baren", "dryck")["device"]["deviceId"]
    paid(env)
    assert jobs_by_device() == {bar: ["bar", "kok"]}


def test_no_printer_prints_nothing(env):
    paid(env)
    assert jobs_by_device() == {}


def test_annat_printer_gets_what_is_sent_to_it(env):
    kok = add(env, "Köket", "kok")["device"]["deviceId"]
    pizza = add(env, "Pizzaugn", "annat")["device"]["deviceId"]
    _, config = api(env, "GET", "routing")
    other = next(s for s in config["stations"] if s["kind"] == "annat")
    assert other["name"] == "Pizzaugn" and other["printerIds"] == [pizza]
    assert api(env, "PUT", "routing/categories", {"categories": {"mains": other["stationId"]}})[0] == 200
    paid(env)
    assert jobs_by_device() == {pizza: [other["stationId"]], kok: ["bar", "kok"]}  # IPA has no category choice: Kök
    api(env, "PUT", f"devices/{pizza}", {"name": "Ugnen"})
    assert stations(env)[other["stationId"]][0] == "Ugnen"


def test_removing_the_order_printer_keeps_the_others_printing(env):
    first = add(env, "Köket", "kok")["device"]["deviceId"]
    second = add(env, "Baren", "dryck")["device"]["deviceId"]
    api(env, "DELETE", f"devices/{first}")
    settings = p.get_settings(LOC_A)
    assert settings["printerId"] == second and settings["autoPrint"] is True


def test_category_routing_from_the_menu(env):
    add(env, "Köket", "kok")
    add(env, "Baren", "dryck")
    code, config = api(env, "PUT", "routing/categories", {"categories": {"c0a1b2c3d4": "bar", "starters": "none"}},
                       role="staff_user")
    assert code == 200 and config["categories"]["c0a1b2c3d4"] == "bar" and config["categories"]["starters"] == "none"
    assert {s["stationId"]: s["kind"] for s in config["stations"]} == {"kok": "kok", "bar": "dryck"}
    code, config = api(env, "PUT", "routing/categories", {"categories": {"c0a1b2c3d4": None}})
    assert code == 200 and "c0a1b2c3d4" not in config["categories"]
    assert api(env, "PUT", "routing/categories", {"categories": {"c0a1b2c3d4": "nope"}})[0] == 400
    assert api(env, "PUT", "routing/categories", {"categories": {}})[0] == 400
    assert api(env, "PUT", "routing", config, role="staff_user")[0] == 403


def test_annat_printer_named_like_a_station_keeps_names_unique(env):
    add(env, "Baren", "dryck")
    add(env, "Dryck", "annat")
    add(env, "Pizza", "annat")
    add(env, "Pizza", "annat")
    names = [n.lower() for n, _ in stations(env).values()]
    assert len(names) == len(set(names))
    status, config = api(env, "GET", "routing")
    config["allergyTicket"] = not config["allergyTicket"]
    status, body = api(env, "PUT", "routing", config)
    assert status == 200, body

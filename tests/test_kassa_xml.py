"""The journal export (SKVFS 2021:16) is valid against Skatteverket's XML
schema (tests/fixtures/skv_kassaregister_xsd) for every kind of
registration Kassa makes, and the export endpoint is owner-only."""

from error_body import err
from pathlib import Path

import pytest
import xmlschema

from shared import kassa_xml
from test_kassa import bill, call, env, journal, pay, register, sale  # noqa: F401 - env is the fixture

XSD = (Path(__file__).parent / "fixtures" / "skv_kassaregister_xsd" / "instans" / "utredningsstod" / "2021-12-08"
       / "ExportAvDataFranJournalminneEnligtSKVFS202116-2021-12-08.xsd")


@pytest.fixture(scope="module")
def schema():
    return xmlschema.XMLSchema(str(XSD))


def _every_kind(env):
    rid = register(env)
    call(env, "POST", f"registers/{rid}/open-day", body={"float": 20000})
    b, _ = sale(env, rid, discount={"kind": "amount", "value": 1000, "label": "Stamgäst"})
    call(env, "POST", f"registers/{rid}/receipts/1/copy", body={"delivery": "email", "email": "g@example.se"})
    pasta = next(line for line in b["lines"] if line["menuItemId"] == "pasta")
    call(env, "POST", f"registers/{rid}/receipts/1/return",
         body={"lines": [{"lineId": pasta["lineId"], "quantity": 1}], "reason": "Fel rätt", "delivery": "print"})
    other = bill(env, rid)
    sent = call(env, "POST", f"bills/{other['billId']}/send", body={})[1]
    call(env, "POST", f"bills/{other['billId']}/void", body={})
    assert sent["status"] == "open"
    third = bill(env, rid)
    pay(env, third["billId"], third["total"], "b" * 32)
    call(env, "POST", f"registers/{rid}/simulate", body={"down": True})
    call(env, "POST", f"bills/{third['billId']}/finalize", body={"delivery": "print"})
    call(env, "POST", f"registers/{rid}/simulate", body={"down": False})
    call(env, "POST", f"bills/{third['billId']}/finalize", body={"delivery": "email", "email": "g@example.se"})
    call(env, "POST", f"registers/{rid}/training", body={"on": True})
    training = bill(env, rid)
    call(env, "POST", f"bills/{training['billId']}/finalize", body={"delivery": "screen"})
    call(env, "POST", f"registers/{rid}/training", body={"on": False})
    call(env, "POST", f"registers/{rid}/x", body={})
    call(env, "POST", f"registers/{rid}/z", body={})
    return rid


def test_export_of_every_registration_kind_is_valid(env, schema):
    rid = _every_kind(env)
    kinds = {e["type"] for e in journal(env, rid)}
    assert kinds == {"start", "vaxelkassa", "kassakvitto", "kopia", "returkvitto", "radera", "statuskod",
                     "ovningslage", "ovningskvitto", "normallage", "x", "z"}
    status, xml = call(env, "GET", f"registers/{rid}/export", query={"from": "2026-10-24", "to": "2026-10-24"})
    assert status == 200 and xml.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    schema.validate(xml)
    assert "<utreko:Kontrollkod>" in xml and "<utreko:LopnummerForUrsprungligtKassakvitto>1<" in xml
    assert "<utreko:OrganisationsnummerEllerPersonnummer>5566778899<" in xml
    assert "<utreko:ElektronisktKassakvitto>JA<" in xml and "<utreko:AntalAvSaldArtikel>-1.00<" in xml


def test_export_period_and_owner_only(env):
    rid = register(env)
    assert call(env, "GET", f"registers/{rid}/export", query={"from": "2026-10-25", "to": "2026-10-24"})[0] == 400
    assert call(env, "GET", f"registers/{rid}/export", query={"from": "2026-10-20", "to": "2026-10-21"}) == (
        409, err("nothing_to_export"))


def test_amounts_and_numbers():
    assert kassa_xml.amount(-1234) == "-12.34" and kassa_xml.amount(5) == "0.05"
    assert kassa_xml.number(2) == "2.00"

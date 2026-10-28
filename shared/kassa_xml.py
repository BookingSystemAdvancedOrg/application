"""Journal export in XML per SKVFS 2021:16 (KRAV 4 kap. 10 §): UTF-8, XML
1.0, valid against Skatteverket's schema "Export av data från journalminne
enligt SKVFS 2021:16" (2021-12-08; a copy is in
tests/fixtures/skv_kassaregister_xsd and tests/test_kassa_xml.py validates
every export shape against it). Element order follows the schema exactly.

Times are Swedish local time without zone (DatumOchKlockslagTYPE), amounts
kronor with two decimals. Return receipts carry negative quantities and
amounts (docs/KASSA-REQUIREMENTS.md, uncertain item). Dishes are VARA.
"""

import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal

from shared import control_system as cs
from shared import kassa as k

INSTANS = "http://xmls.skatteverket.se/se/skatteverket/td/instans/utredningsstod/1.0"
KOMPONENT = "http://xmls.skatteverket.se/se/skatteverket/td/komponent/utredningsstod/1.0"
ROOT = "ExportAvDataFranJournalminneEnligtSKVFS2021-16"
ET.register_namespace("utrein", INSTANS)
ET.register_namespace("utreko", KOMPONENT)


def _q(name):
    return f"{{{KOMPONENT}}}{name}"


def _add(parent, name, value=None):
    el = ET.SubElement(parent, _q(name))
    if value is not None:
        el.text = str(value)
    return el


def amount(ore):
    return str((Decimal(int(ore or 0)) / 100).quantize(Decimal("0.01")))


def number(value):
    return str(Decimal(int(value or 0)).quantize(Decimal("0.01")))


def _t72(value):
    return (" ".join(str(value or "").split()) or "-")[:72]


def _local(location, iso_utc):
    """A stored UTC time -> Swedish local time text."""
    moment = datetime.fromisoformat(str(iso_utc).replace("Z", "+00:00"))
    return k.local(moment, location).strftime("%Y-%m-%dT%H:%M:%S")


def _company(parent, c):
    _add(parent, "ForetagetsNamn", _t72(c.get("name")))
    _add(parent, "OrganisationsnummerEllerPersonnummer", c.get("org"))
    _add(parent, "DenAdressDarForsaljningSker", _t72(c.get("address")))


def _articles(parent, lines, location, sign, fallback):
    lst = _add(parent, "SaldaArtiklarLISTA")
    for line in lines:
        art = _add(lst, "SaldArtikel")
        _add(art, "IdentitetsbeteckningForRegistreringAvArtiklar", line.get("lineId"))
        _add(art, "TypAvArtikel", "VARA")
        if line.get("category") in k.CATEGORY_NAMES:
            _add(art, "NamnPaHuvudgrupp", k.CATEGORY_NAMES[line["category"]])
        if line.get("menuItemId"):
            _add(art, "Artikelnummer", _t72(line["menuItemId"]))
        _add(art, "Artikelnamn", _t72(line.get("name")))
        _add(art, "AntalAvSaldArtikel", number(sign * int(line.get("quantity") or 0)))
        _add(art, "PrisPerEnhet", amount(line.get("unitPrice")))
        gross = line.get("gross") if line.get("gross") is not None else line.get("total")
        _add(art, "PrisPaSaldArtikel", amount(sign * int(gross or 0)))
        if int(line.get("discount") or 0):
            discounts = _add(art, "RabatterPaSaldArtikelLISTA")
            d = _add(discounts, "RabattPaSaldArtikel")
            _add(d, "BenamningPaRabatt", "Rabatt")
            _add(d, "RabattensBelopp", amount(sign * int(line["discount"])))
        _add(art, "MervardesskattesatsForSaldArtikel", number(line.get("vatRate")))
        _add(art, "MervardesskattPaSaldArtikel", amount(sign * int(line.get("vat") or 0)))
        _add(art, "ArtikelnsRegistreringstidpunkt", _local(location, line["addedAt"]) if line.get("addedAt")
             else fallback)


def _sums(parent, data, sign, *, payments_label=None):
    discount = (data.get("discount") or {}).get("amount")
    if discount:
        _add(parent, "TotaltRabattbeloppPaKvitto", amount(sign * int(discount)))
    _add(parent, "TotaltForsaljningsbeloppISvenskaKronor", amount(sign * int(data.get("total") or 0)))
    _add(parent, "MervardesskattPaForsaljningsbeloppet", amount(sign * sum(int(v["vat"]) for v in data.get("vat") or [])))
    pay = _add(parent, "TotalaForsaljningssummorPerBetalningsmedelLISTA")
    payments = data.get("payments") or [{"method": payments_label or k.CARD, "amount": data.get("total")}]
    by_method = {}
    for p in payments:
        by_method[p.get("method") or k.CARD] = by_method.get(p.get("method") or k.CARD, 0) + int(p.get("amount") or 0)
    for method, value in by_method.items():
        row = _add(pay, "TotalForsaljningssummaPerBetalningsmedel")
        _add(row, "Betalningsmedel", method)
        _add(row, "ForsaljningssummaPerBetalningsmedel", amount(sign * value))
    vats = _add(parent, "MervardesskattPaOlikaSkattesatserLISTA")
    for v in data.get("vat") or [{"rate": 0, "vat": 0}]:
        row = _add(vats, "MervardesskattPerSkattesats")
        _add(row, "Mervardesskattesats", number(v["rate"]))
        _add(row, "TotaltMervardesskattebeloppPerSkattesats", amount(sign * int(v["vat"])))


def _code_element(code, control_unit, control_system):
    """Kontrollkod (59 characters, a control unit) or Avstamningskod (113, a
    control system such as fiskaly's): the schema offers both."""
    return control_system if len(str(code or "")) == cs.AVSTAMNING_LENGTH else control_unit


def _receipt(parent, entry, data, location, kind):
    sign = -1 if kind == k.RETURN else 1
    el = _add(parent, {k.SALE: "Kassakvitto", k.RETURN: "Returkvitto", k.TRAINING: "Ovningskvitto"}[kind])
    _company(el, data.get("company") or {})
    _add(el, "DatumOchKlockslagNarReturkvittoFramstalls" if kind == k.RETURN else "DatumOchKlockslagNarKvittoFramstalls",
         entry["local"])
    if kind == k.TRAINING:
        _add(el, "LopnummerForOvningskvitto", data.get("seq"))
    else:
        _add(el, "LopnummerForKassakvitto", data.get("seq"))
    if kind == k.RETURN:
        _add(el, "LopnummerForUrsprungligtKassakvitto", data.get("originalSeq"))
    _add(el, "Kassabeteckning", entry.get("label"))
    _articles(el, data.get("lines") or [], location, sign, entry["local"])
    _sums(el, data, sign, payments_label="Övning" if kind == k.TRAINING else None)
    if kind != k.TRAINING and data.get("electronic"):
        _add(el, "ElektronisktKassakvitto", "JA")
    _add(el, "LopnummerForZDagrapportPaKvitto", data.get("zSeq"))
    _add(el, "TillverkningsnummerForKontrollsystemet", data.get("csSerial"))
    if kind != k.TRAINING:
        _add(el, _code_element(data.get("controlCode"), "Kontrollkod", "Avstamningskod"), data.get("controlCode"))


def _copy(parent, entry, data, location):
    original = data.get("original") or {}
    el = _add(parent, "KopiaAvKassakvitto")
    _company(el, original.get("company") or {})
    _add(el, "DatumOchKlockslagNarKvittoFramstalls", entry["local"])
    _add(el, "DatumOchKlockslagNarOriginalkvittoFramstalldes", data.get("originalAt"))
    _add(el, "LopnummerForKvittokopia", data.get("copySeq"))
    _add(el, "LopnummerPaOriginalkvitto", data.get("originalSeq"))
    _add(el, "Kassabeteckning", entry.get("label"))
    _articles(el, original.get("lines") or [], location, 1, data.get("originalAt"))
    _sums(el, original, 1)
    if len(str(data.get("controlCode") or "")) == cs.CODE_LENGTH:
        # The schema has room for a control unit's 59-character code only; a
        # control system's 113-character one stays in the journal (Osäkert).
        _add(el, "KontrollkodPaKopiaAvKassakvitto", data.get("controlCode"))
    if data.get("electronic"):
        _add(el, "ElektroniskKopiaAvKassakvitto", "JA")
    _add(el, "LopnummerForZDagrapportPaKvitto", data.get("zSeq"))
    _add(el, "TillverkningsnummerForKontrollsystemet", data.get("csSerial"))
    _add(el, _code_element(original.get("controlCode"), "KontrollkodForKvittotSomKopianAvser",
                           "AvstamningskodForKvittotSomKopianAvser"), original.get("controlCode"))


def _report(parent, entry, data, kind):
    el = _add(parent, "ZDagrapport" if kind == k.ZREPORT else "XDagrapport")
    c = data.get("company") or {}
    _add(el, "ForetagetsNamn", _t72(c.get("name")))
    _add(el, "OrganisationsnummerEllerPersonnummer", c.get("org"))
    _add(el, "DatumOchKlockslagNarRapportFramstalls", entry["local"])
    if kind == k.ZREPORT:
        _add(el, "LopnummerForZDagrapport", data.get("zSeq"))
    _add(el, "Kassabeteckning", entry.get("label"))
    if kind == k.ZREPORT:
        _add(el, "TillverkningsnummerForKassaregistret", data.get("serialNumber"))
        _add(el, "ModellbeteckningForKassaregistret", k.MODEL)
        _add(el, "ModellEllerProgramForKassaregistret", _t72(data.get("program") or k.PROGRAM))
        _add(el, "VersionsnummerForKassaregistret", _t72(data.get("version")))
    _add(el, "TotalForsaljningssumma", amount(data.get("totalSales")))
    if data.get("groups"):
        groups = _add(el, "TotalaForsaljningssummorForOlikaHuvudgrupperLISTA")
        for g in data["groups"]:
            row = _add(groups, "TotalForsaljningssummaForOlikaHuvudgrupper")
            _add(row, "NamnPaHuvudgrupp", _t72(g["name"]))
            _add(row, "TotalForsaljningssummaForHuvudgrupp", amount(g["amount"]))
    vats = _add(el, "MervardesskattPaOlikaSkattesatserLISTA")
    for v in data.get("vat") or [{"rate": 0, "amount": 0}]:
        row = _add(vats, "MervardesskattPerSkattesats")
        _add(row, "Mervardesskattesats", number(v["rate"]))
        _add(row, "TotaltMervardesskattebeloppPerSkattesats", amount(v["amount"]))
    _add(el, "VaxelkassansBeloppISvenskaKronor", amount(data.get("float")))
    _add(el, "AntalSaldaVaror", number(data.get("itemsSold")))
    _add(el, "AntalSaldaTjanster", number(data.get("servicesSold")))
    _add(el, "AntalKassakvitton", int(data.get("receipts") or 0))
    _add(el, "AntalKassaladoppningar", int(data.get("drawerOpenings") or 0))
    _add(el, "AntalKvittokopior", int(data.get("copies") or 0))
    _add(el, "TotaltBeloppPaKvittokopior", amount(data.get("copiesAmount")))
    _add(el, "AntalKvittonSomTagitsFramIOvningslage", int(data.get("training") or 0))
    _add(el, "TotaltBeloppIOvningslage", amount(data.get("trainingAmount")))
    pay = _add(el, "TotalaForsaljningssummorPerBetalningsmedelLISTA")
    for p in data.get("payments") or [{"method": k.CARD, "amount": 0}]:
        row = _add(pay, "TotalForsaljningssummaPerBetalningsmedel")
        _add(row, "Betalningsmedel", p["method"])
        _add(row, "ForsaljningssummaPerBetalningsmedel", amount(p["amount"]))
    _add(el, "AntalReturer", int(data.get("returns") or 0))
    _add(el, "ReturernasBelopp", amount(data.get("returnsAmount")))
    _add(el, "RabatternasBelopp", amount(data.get("discounts")))
    _add(el, "AntalOvrigaRegistreringarSomMinskatDagensForsaljningsbelopp", int(data.get("otherReductions") or 0))
    _add(el, "TotaltBeloppAvOvrigaRegistreringarSomMinskatDagensForsaljningsbelopp",
         amount(data.get("otherReductionsAmount")))
    _add(el, "AntalOavslutadeForsaljningar", int(data.get("openBills") or 0))
    _add(el, "TotaltBeloppAvOavslutadeForsaljningar", amount(data.get("openBillsAmount")))
    _add(el, "GrandTotalForsaljning", amount(data.get("grandTotalSales")))
    _add(el, "GrandTotalRetur", amount(data.get("grandTotalReturn")))
    _add(el, "GrandTotalNetto", amount(data.get("grandTotalNet")))


def _handelse(parent, entry, location):
    """One journal entry -> one or more Handelse (a removal per line)."""
    kind, data = entry.get("type"), k._plain(entry.get("data") or {})
    bodies = []
    if kind in (k.SALE, k.RETURN, k.TRAINING):
        bodies.append(lambda h: _receipt(h, entry, data, location, kind))
    elif kind == k.COPY:
        bodies.append(lambda h: _copy(h, entry, data, location))
    elif kind in (k.XREPORT, k.ZREPORT):
        bodies.append(lambda h: _report(h, entry, data, kind))
    elif kind == k.FLOAT:
        def float_change(h):
            change = _add(h, "ForandraVaxelkassa")
            _add(change, "ForandringAvVaxelkassaISvenskaKronor", amount(data.get("change")))
        bodies.append(float_change)
    elif kind in (k.START, k.TRAINING_ON, k.TRAINING_OFF):
        name = {k.START: "StartAvKassaregister", k.TRAINING_ON: "StallaOmTillOvningslage",
                k.TRAINING_OFF: "StallaOmTillNormallage"}[kind]
        bodies.append(lambda h: _add(h, name, "JA"))
    elif kind == k.STATUS:
        bodies.append(lambda h: _add(h, "StatuskodFranKontrollprogrammet", _t72(data.get("code"))))
    elif kind == k.DELETED:
        for line in data.get("lines") or []:
            def removed(h, line=line):
                el = _add(h, "RaderaRegistreradArtikel")
                _add(el, "IdentitetsbeteckningForRegistreringAvArtiklar", line.get("lineId"))
                _add(el, "TypAvArtikel", "VARA")
                if line.get("category") in k.CATEGORY_NAMES:
                    _add(el, "NamnPaHuvudgrupp", k.CATEGORY_NAMES[line["category"]])
                if line.get("menuItemId"):
                    _add(el, "Artikelnummer", _t72(line["menuItemId"]))
                _add(el, "Artikelnamn", _t72(line.get("name")))
                _add(el, "AntalAvRegistreradArtikel", number(line.get("quantity")))
                _add(el, "PrisPerEnhet", amount(line.get("unitPrice")))
                _add(el, "PrisPaRegistreradArtikel", amount(line.get("amount")))
                _add(el, "MervardesskattesatsForRegistreradArtikel", number(line.get("vatRate")))
                _add(el, "MervardesskattPaRegistreradArtikel", amount(line.get("vat")))
                _add(el, "UrsprungligRegistreringstidpunkt",
                     _local(location, line["addedAt"]) if line.get("addedAt") else entry["local"])
                _add(el, "TypAvRadering", _t72(data.get("reason")))
            bodies.append(removed)
    for body in bodies:
        h = _add(parent, "Handelse")
        if entry.get("user"):
            _add(h, "InloggadAnvandare", _t72(entry["user"]))
        _add(h, "Kassabeteckning", entry.get("label"))
        _add(h, "Registreringstidpunkt", entry["local"])
        body(h)


def export(*, register, entries, company, location, start, end, user, now, control_system_serial=None):
    """The XML file (bytes) for `entries` of one register in [start, end]
    (local "YYYY-MM-DDTHH:MM:SS")."""
    if not entries:
        raise k.Conflict("nothing_to_export")
    root = ET.Element(f"{{{INSTANS}}}{ROOT}")
    _add(root, "InloggadAnvandareVidExportAvDataFranJournalminnet", _t72(user))
    _add(root, "BeteckningPaDetKassaregisterSomExportAvDataFranJournalminnetSkerIfran", k.designation(register))
    _add(root, "TidpunktForNarExportAvDataFranJournalminnetSker", k.local_text(now, location))
    period = _add(root, "ValdPeriodForExportAvDataFranJournalminne")
    _add(period, "ValdStarttidpunktForExportAvDataFranJournalminnet", start)
    _add(period, "ValdSluttidpunktForExportAvDataFranJournalminnet", end)
    chosen = _add(root, "BeteckningPaValdaKassaregisterForExportAvDataFranJournalminnenLISTA")
    _add(chosen, "BeteckningPaValtKassaregisterForExportAvDataFranJournalminnet", k.designation(register))
    info = _add(root, "InformationOmForetaget")
    _add(info, "OrganisationsnummerEllerPersonnummer", company["org"])
    _add(info, "ForetagetsNamn", _t72(company["name"]))
    if company.get("restaurant"):
        _add(info, "NamnPaVerksamheten", _t72(company["restaurant"]))
    _add(info, "DenAdressDarForsaljningSker", _t72(company["address"]))
    systems = _add(root, "KassaregistersystemLISTA")
    system = _add(systems, "Kassaregistersystemet")
    _add(system, "TillverkningsnummerForKassaregistret", register["serialNumber"])
    _add(system, "ModellbeteckningForKassaregistret", k.MODEL)
    _add(system, "ModellEllerProgramForKassaregistret", k.PROGRAM)
    _add(system, "VersionsnummerForKassaregistret", _t72(k.version()))
    _add(system, "Kassabeteckning", k.designation(register))
    serial = next((k._plain(e.get("data") or {}).get("csSerial") for e in reversed(entries)
                   if (e.get("data") or {}).get("csSerial")), None)
    _add(system, "TillverkningsnummerForKontrollsystemet", serial or control_system_serial or cs.SIMULATOR_SERIAL)
    registrations = _add(root, "RegistreringLISTA")
    for entry in sorted(entries, key=lambda e: int(e.get("jseq") or 0)):
        _handelse(registrations, entry, location)
    ET.indent(root)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")).encode("utf-8")

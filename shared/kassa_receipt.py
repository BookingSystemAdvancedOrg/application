"""Kassa's receipts, reports and kitchen tickets as documents: a list of
rows the admin shows, CloudPRNT prints (print_ticket.document_ticket) and
the notification function e-mails (email()). One source, so the screen,
the paper and the e-mail always say the same thing.

Row shapes: {"text", "bold", "big", "center"}, {"pair": [left, right]},
{"rule": true}. "KOPIA" and "ÖVNING" are big (double width and height):
SKVFS 2014:9 5 kap. 5 § wants them at least twice the size of amounts.
The control code (59 characters from a control unit or the simulator, 113
from a control system such as fiskaly's) is split into rows of at most
CODE_ROW characters, so it wraps the same on 80 mm (48 columns) and 58 mm
(32 columns) paper and in the e-mail. Pure functions; nothing here sends
or stores.
"""

import html
import math
from datetime import datetime

from shared import kassa as k
from shared.messages import _brand_color, restaurant_name

TEST_NOTICE = "Testläge - inte ett certifierat kassaregister än"
TITLES = {k.SALE: "Kvitto", k.RETURN: "Returkvitto", k.TRAINING: "Övningskvitto", k.COPY: "Kvitto",
          k.XREPORT: "X-dagrapport", k.ZREPORT: "Z-dagrapport"}
CODE_ROW = 30
_BRANDS = {"visa": "Visa", "mastercard": "Mastercard", "amex": "American Express", "maestro": "Maestro"}


def kr(ore, *, sign=False):
    ore = int(ore or 0)
    text = f"{abs(ore) / 100:,.2f}".replace(",", " ").replace(".", ",")
    return ("-" if ore < 0 else "+" if sign and ore > 0 else "") + text


def _when(text):
    try:
        return datetime.fromisoformat(str(text)).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(text or "")


def _rate(rate):
    return f"{int(rate)} %"


def _card(p):
    if p.get("last4"):
        brand = _BRANDS.get(str(p.get("brand") or "").lower(), str(p.get("brand") or "Kort").capitalize())
        return f"Kort {brand} **** {p['last4']}"
    return "Kort"


def _company_rows(c):
    rows = [{"text": c.get("restaurant") or c.get("name"), "bold": True, "center": True}]
    if c.get("restaurant") and c.get("name") and c["name"] != c["restaurant"]:
        rows.append({"text": c["name"], "center": True})
    rows.append({"text": f"Org.nr {c.get('orgText') or c.get('org')}", "center": True})
    rows.append({"text": c.get("address"), "center": True})
    return rows


def _lines(data, sign):
    rows = []
    for line in data.get("lines") or []:
        quantity = int(line.get("quantity") or 0)
        gross = int(line.get("gross") if line.get("gross") is not None else line.get("total") or 0)
        rows.append({"pair": [f"{quantity} x {line.get('name')}", kr(sign * gross)]})
        if quantity > 1 and line.get("unitPrice") is not None:
            rows.append({"text": f"  à {kr(int(line['unitPrice']))}"})
        if line.get("note"):
            rows.append({"text": f"  {line['note']}"})
        if int(line.get("discount") or 0):
            rows.append({"pair": ["  Rabatt", kr(-sign * int(line["discount"]))]})
    return rows


def _totals(data, sign):
    rows = [{"rule": True}]
    discount = data.get("discount") or {}
    if int(discount.get("amount") or 0):
        rows.append({"pair": [f"Varav rabatt ({discount.get('label') or 'Rabatt'})", kr(-sign * int(discount["amount"]))]})
    rows.append({"pair": ["Totalt", kr(sign * int(data.get("total") or 0))], "bold": True, "big": True})
    for v in data.get("vat") or []:
        rows.append({"pair": [f"Moms {_rate(v['rate'])} (netto {kr(sign * int(v['net']))})", kr(sign * int(v["vat"]))]})
    return rows


def code_rows(code):
    """The code in even rows of at most CODE_ROW characters, never cut."""
    code = str(code or "")
    if not code:
        return [{"text": ""}]
    size = math.ceil(len(code) / math.ceil(len(code) / CODE_ROW))
    return [{"text": code[i:i + size], "code": True} for i in range(0, len(code), size)]


def _codes(data, *, copy_code=None):
    rows = [{"rule": True}, {"text": "Kontrollkod:"}, *code_rows(data.get("controlCode"))]
    if copy_code:
        rows += [{"text": "Kontrollkod för kopian:"}, *code_rows(copy_code)]
    rows.append({"text": f"Kontrollsystem: {data.get('csSerial') or ''}"})
    if data.get("offline"):
        rows.append({"text": "Offline", "bold": True})
    if data.get("electronic"):
        rows.append({"text": "Elektroniskt kassakvitto", "bold": True})
    return rows


def _receipt(entry, data, kind, *, copy_of=None, copy_code=None):
    sign = -1 if kind == k.RETURN else 1
    rows = []
    if data.get("testMode"):
        rows.append({"text": TEST_NOTICE, "bold": True, "center": True})
    if copy_of:
        rows.append({"text": "KOPIA", "bold": True, "big": True, "center": True})
    if kind == k.TRAINING:
        rows.append({"text": "ÖVNING", "bold": True, "big": True, "center": True})
    rows += _company_rows(data.get("company") or {})
    rows.append({"rule": True})
    rows.append({"pair": [TITLES[kind], f"Nr {data.get('seq')}"], "bold": True})
    rows.append({"pair": ["Datum", _when(copy_of["local"] if copy_of else entry.get("local"))]})
    rows.append({"pair": ["Kassabeteckning", entry.get("label") or ""]})
    if entry.get("registerName"):
        rows.append({"pair": ["Kassa", entry["registerName"]]})
    if data.get("billKind") == k.TABLE and data.get("tableLabel"):
        rows.append({"pair": ["Bord", data["tableLabel"]]})
    elif data.get("billKind") == k.TAKEAWAY:
        rows.append({"text": "Avhämtning"})
    if kind == k.RETURN:
        rows.append({"text": f"Avser kvitto nr {data.get('originalSeq')} ({_when(data.get('originalAt'))})"})
        rows.append({"text": f"Orsak: {data.get('reason') or ''}"})
    rows.append({"rule": True})
    rows += _lines(data, sign)
    rows += _totals(data, sign)
    if data.get("payments"):
        rows.append({"rule": True})
        label = "Återbetalt" if kind == k.RETURN else "Betalt"
        for p in data["payments"]:
            rows.append({"pair": [f"{label}, {_card(p)}", kr(sign * int(p.get("amount") or 0))]})
    if kind != k.TRAINING:
        rows += _codes(data, copy_code=copy_code)
    else:
        rows += [{"rule": True}, {"text": f"Kontrollsystem: {data.get('csSerial') or ''}"}]
    if kind == k.TRAINING:
        rows.append({"text": "ÖVNING", "bold": True, "big": True, "center": True})
    if copy_of:
        rows.append({"text": "KOPIA", "bold": True, "big": True, "center": True})
    rows.append({"text": "Tack för besöket!", "center": True})
    return rows


def _report(entry, data, kind):
    rows = []
    if (entry.get("data") or {}).get("testMode"):
        rows.append({"text": TEST_NOTICE, "bold": True, "center": True})
    c = data.get("company") or {}
    rows += [{"text": TITLES[kind], "bold": True, "big": True, "center": True},
             {"text": c.get("name"), "bold": True, "center": True},
             {"text": f"Org.nr {c.get('orgText') or c.get('org')}", "center": True}, {"rule": True},
             {"pair": ["Framtagen", _when(entry.get("local"))]},
             {"pair": ["Kassabeteckning", entry.get("label") or ""]}]
    if kind == k.ZREPORT:
        rows.append({"pair": ["Z-nummer", str(data.get("zSeq"))], "bold": True})
        rows.append({"pair": ["Tillverkningsnummer", data.get("serialNumber") or ""]})
        rows.append({"pair": ["Program", f"{data.get('program')} {data.get('version')}"]})
    if data.get("since"):
        rows.append({"pair": ["Sedan senaste Z", _when(str(data["since"]).replace("Z", "")[:19])]})
    rows += [{"rule": True}, {"pair": ["Total försäljning", kr(data.get("totalSales"))], "bold": True}]
    for g in data.get("groups") or []:
        rows.append({"pair": [f"  {g['name']}", kr(g["amount"])]})
    for v in data.get("vat") or []:
        rows.append({"pair": [f"Moms {_rate(v['rate'])}", kr(v["amount"])]})
    for p in data.get("payments") or []:
        rows.append({"pair": [f"Betalt med {p['method'].lower()}", kr(p["amount"])]})
    rows += [
        {"pair": ["Växelkassa", kr(data.get("float"))]},
        {"pair": ["Sålda varor, antal", str(data.get("itemsSold", 0))]},
        {"pair": ["Sålda tjänster, antal", str(data.get("servicesSold", 0))]},
        {"pair": ["Kassakvitton, antal", str(data.get("receipts", 0))]},
        {"pair": ["Lådöppningar", str(data.get("drawerOpenings", 0))]},
        {"pair": [f"Kvittokopior, {data.get('copies', 0)} st", kr(data.get("copiesAmount"))]},
        {"pair": [f"Övningskvitton, {data.get('training', 0)} st", kr(data.get("trainingAmount"))]},
        {"pair": [f"Returer, {data.get('returns', 0)} st", kr(data.get("returnsAmount"))]},
        *({"pair": [f"  varav moms {_rate(v['rate'])}", kr(v["amount"])]} for v in data.get("returnVat") or []),
        *({"pair": [f"  återbetalt med {p['method'].lower()}", kr(p["amount"])]} for p in data.get("refunds") or []),
        {"pair": ["Rabatter", kr(data.get("discounts"))]},
        {"pair": [f"Övriga minskningar, {data.get('otherReductions', 0)} st", kr(data.get("otherReductionsAmount"))]},
        {"pair": [f"Oavslutade notor, {data.get('openBills', 0)} st", kr(data.get("openBillsAmount"))]},
        {"rule": True},
        {"pair": ["Grand total försäljning", kr(data.get("grandTotalSales"))]},
        {"pair": ["Grand total retur", kr(data.get("grandTotalReturn"))]},
        {"pair": ["Grand total netto", kr(data.get("grandTotalNet"))], "bold": True},
    ]
    return rows


def document(entry, location=None):
    """The document of a journal entry (receipt, return, copy, training
    receipt, X or Z report)."""
    kind, data = entry.get("type"), k._plain(entry.get("data") or {})
    if kind in (k.XREPORT, k.ZREPORT):
        rows = _report(entry, data, kind)
        total = data.get("totalSales")
    elif kind == k.COPY:
        original = {**data.get("original", {}), "controlCode": data.get("originalControlCode") or
                    data.get("original", {}).get("controlCode"), "electronic": data.get("electronic"),
                    "testMode": data.get("testMode")}
        rows = _receipt(entry, original, k.SALE, copy_of={"local": data.get("originalAt")},
                        copy_code=data.get("controlCode"))
        rows.insert(-2, {"text": f"Kopia nr {data.get('copySeq')}, {_when(entry.get('local'))}"})
        total = original.get("total")
        kind_title = "Kopia av kvitto"
        return {"kind": kind, "title": kind_title, "seq": data.get("originalSeq"), "total": total, "rows": rows,
                "jseq": int(entry.get("jseq") or 0)}
    elif kind in (k.SALE, k.RETURN, k.TRAINING):
        rows = _receipt(entry, data, kind)
        total = -int(data.get("total") or 0) if kind == k.RETURN else data.get("total")
    else:
        raise ValueError(f"no document for {kind}")
    return {"kind": kind, "title": TITLES[kind], "seq": data.get("zSeq") if kind == k.ZREPORT else data.get("seq"),
            "total": total, "rows": rows, "jseq": int(entry.get("jseq") or 0)}


def kitchen_document(bill, lines, now, location):
    """A kitchen ticket for lines of a bill: no prices, never a receipt."""
    when = k.local(now, location).strftime("%H:%M")
    rows = [{"text": "Köksnota, ej kvitto", "center": True}]
    if bill.get("training"):
        rows.append({"text": "ÖVNING", "bold": True, "big": True, "center": True})
    where = f"BORD {bill.get('tableLabel')}" if bill.get("kind") == k.TABLE else "AVHÄMTNING"
    rows += [{"text": where, "bold": True, "big": True, "center": True}, {"text": f"Kassa {when}", "center": True},
             {"rule": True}]
    for line in lines:
        rows.append({"text": f"{int(line.get('quantity') or 0)} x {line.get('name')}", "bold": True})
        if line.get("note"):
            rows.append({"text": f"  {line['note']}", "bold": True})
    rows.append({"text": f"Nota {str(bill.get('billId') or '')[:8]}"})
    return {"kind": "kitchen", "title": "Köksnota", "rows": rows}


def text(document):
    out = []
    for row in document.get("rows") or []:
        if row.get("rule"):
            out.append("-" * 40)
        elif "pair" in row:
            left, right = row["pair"]
            out.append(f"{left}  {right}" if len(left) + len(right) >= 38 else left.ljust(40 - len(right)) + right)
        else:
            out.append(str(row.get("text") or ""))
    return "\n".join(out)


def email(document, tenant_row, location):
    """(subject, text, html) of an e-mailed receipt (notification sends it)."""
    name = restaurant_name(tenant_row, location)
    total = document.get("total")
    subject = f"{document.get('title') or 'Kvitto'} {kr(total)} kr - {name}"
    e = html.escape
    color = _brand_color(tenant_row)
    rows = []
    for row in document.get("rows") or []:
        style = "font-weight:600;" if row.get("bold") else ""
        style += "font-size:20px;" if row.get("big") else ""
        if row.get("rule"):
            rows.append('<tr><td colspan="2" style="border-top:1px solid #e5e5e5;padding:4px 0"></td></tr>')
        elif "pair" in row:
            rows.append(f'<tr><td style="padding:2px 12px 2px 0;{style}">{e(str(row["pair"][0]))}</td>'
                        f'<td style="padding:2px 0;text-align:right;white-space:nowrap;{style}">{e(str(row["pair"][1]))}</td></tr>')
        else:
            align = "center" if row.get("center") else "left"
            rows.append(f'<tr><td colspan="2" style="padding:2px 0;text-align:{align};word-break:break-all;{style}">'
                        f'{e(str(row.get("text") or ""))}</td></tr>')
    html_body = (
        '<!doctype html><html><body style="margin:0;background:#f4f2ee">'
        '<div style="max-width:480px;margin:0 auto;padding:32px 20px;'
        'font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.45">'
        f'<div style="background:#ffffff;border-radius:10px;padding:24px;border-top:4px solid {color}">'
        f'<table role="presentation" style="border-collapse:collapse;width:100%">{"".join(rows)}</table></div>'
        f'<p style="margin:16px 4px 0;color:#8a8a8a;font-size:12px">Du får det här mejlet för att du bad om '
        f'kvittot hos {e(name)}.</p></div></body></html>')
    return subject, text(document), html_body

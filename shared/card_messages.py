"""The Swedish receipt for an in-person card payment (shared/card_payments.py):
what the admin's "Kvitto" view prints and what "Skicka kvitto" e-mails
(notification, notice card_receipt).

A payment for an order lists the order's lines and VAT per rate (copied
onto the payment when the order settles). A free
amount has no lines, so the receipt is a card receipt only: the VAT is on
the restaurant's own cash register receipt for that sale.

The card part carries what card receipts show (EMV): brand and last four
digits, how it was read, the application name and AID, the authorisation
code. No other card data exists here. Pure functions; nothing here sends.
"""

import html
from datetime import datetime
from zoneinfo import ZoneInfo

from shared.messages import _brand_color, contact_line, money, restaurant_name
from shared.order_messages import _seller

_READ = {
    "contactless_emv": "Kontaktlös",
    "contactless_magstripe_mode": "Kontaktlös",
    "contact_emv": "Chip",
    "magnetic_stripe_track2": "Magnetremsa",
    "magnetic_stripe_fallback": "Magnetremsa",
}
_WALLET = {"apple_pay": "Apple Pay", "google_pay": "Google Pay", "samsung_pay": "Samsung Pay"}
_VERIFY = {"offline_pin": "PIN", "online_pin": "PIN", "signature": "Signatur",
           "consumer_device": "Mobil", "none": "Ingen"}


def _local(item, value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return moment.astimezone(ZoneInfo(item.get("timezone") or "Europe/Stockholm")).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, KeyError):
        return ""


def receipt(item, tenant_row, location):
    """The receipt as data (the admin renders it, the e-mail below too).
    An order's lines and VAT are on the payment once the order is paid."""
    card = item.get("card") or {}
    lines = [{"quantity": int(line.get("quantity") or 0), "name": line.get("name") or "",
              "total": int(line.get("total") or 0)} for line in item.get("lines") or []]
    vat = [{"rate": int(v["rate"]), "vat": int(v["vat"]), "net": int(v["net"])} for v in item.get("vat") or []]
    order = bool(item.get("orderId"))
    brand = str(card.get("brand") or "kort").capitalize()
    card_rows = []
    if card.get("last4"):
        card_rows.append(("Kort", f"{brand} **** {card['last4']}"))
    if card.get("wallet") or card.get("readMethod"):
        card_rows.append(("Läst", _WALLET.get(card.get("wallet"), _READ.get(card.get("readMethod"), "Kort"))))
    for label, key in (("Applikation", "applicationName"), ("AID", "aid"), ("Kontotyp", "accountType"),
                       ("Godkännandekod", "authorizationCode"), ("Svarskod", "authorizationResponseCode")):
        if card.get(key):
            card_rows.append((label, str(card[key])))
    if card.get("verification"):
        card_rows.append(("Verifiering", _VERIFY.get(card["verification"], str(card["verification"]))))
    refund = item.get("refund") or {}
    return {
        "title": "Kvitto",
        "restaurant": restaurant_name(tenant_row, location),
        "seller": _seller(tenant_row, location),
        "when": _local(item, item.get("paidAt") or item.get("createdAt")),
        "reference": item["paymentId"][:12].upper(),
        "orderNumber": int(item.get("orderNumber") or 0) or None,
        "note": item.get("note"),
        "lines": lines,
        "vat": vat,
        "total": int(item.get("amount") or 0),
        "vatOnRegister": not order,
        "card": [{"label": k, "value": v} for k, v in card_rows],
        "result": "Godkänd" if item.get("status") in ("succeeded", "refunded") else "Ej godkänd",
        "refunded": int(refund.get("amount") or 0) or None,
    }


def email(item, tenant_row, location):
    """(subject, text, html) of the receipt e-mail."""
    r = receipt(item, tenant_row, location)
    name = r["restaurant"]
    subject = f"Kvitto {money(r['total'])} - {name}"
    contact = contact_line(tenant_row, location)
    seller = r["seller"]
    seller_lines = [seller["name"]] + ([f"Org.nr {seller['org']}"] if seller.get("org") else []) \
        + ([seller["address"]] if seller.get("address") else [])
    heading = f"Beställning #{r['orderNumber']}" if r["orderNumber"] else (r["note"] or "Kortbetalning")

    text = [f"Tack för ditt besök på {name}!", "", f"{r['title']} - {heading}", f"Datum: {r['when']}", ""]
    text += [f"{line['quantity']} x {line['name']}  {money(line['total'])}" for line in r["lines"]]
    text += ["", f"Totalt: {money(r['total'])}"]
    text += [f"Moms {v['rate']} %: {money(v['vat'])} (netto {money(v['net'])})" for v in r["vat"]]
    if r["vatOnRegister"]:
        text.append("Momsen redovisas på restaurangens kassakvitto.")
    if r["refunded"]:
        text.append(f"Återbetalt: {money(r['refunded'])}")
    text += ["", "Kortbetalning, köp", *[f"{row['label']}: {row['value']}" for row in r["card"]],
             f"Resultat: {r['result']}", f"Referens: {r['reference']}", "", "Säljare: " + ", ".join(seller_lines)]
    if contact:
        text += ["", f"Frågor? Kontakta {name}: {contact}"]

    e = html.escape
    color = _brand_color(tenant_row)
    rows = "".join(
        f'<tr><td style="padding:4px 12px 4px 0">{e(str(line["quantity"]))} &times; {e(line["name"])}</td>'
        f'<td style="padding:4px 0;text-align:right;white-space:nowrap">{e(money(line["total"]))}</td></tr>'
        for line in r["lines"])
    vat_rows = "".join(
        f'<tr><td style="padding:2px 12px 2px 0;color:#6b6b6b">Moms {v["rate"]} % (netto {e(money(v["net"]))})</td>'
        f'<td style="padding:2px 0;text-align:right;color:#6b6b6b">{e(money(v["vat"]))}</td></tr>'
        for v in r["vat"])
    extra = []
    if r["vatOnRegister"]:
        extra.append("Momsen redovisas på restaurangens kassakvitto.")
    if r["refunded"]:
        extra.append(f"Återbetalt: {money(r['refunded'])}")
    card_rows = "".join(
        f'<tr><td style="padding:2px 12px 2px 0;color:#6b6b6b">{e(row["label"])}</td>'
        f'<td style="padding:2px 0;text-align:right">{e(row["value"])}</td></tr>'
        for row in [*r["card"], {"label": "Resultat", "value": r["result"]},
                    {"label": "Referens", "value": r["reference"]}])
    html_body = (
        '<!doctype html><html><body style="margin:0;background:#f4f2ee">'
        '<div style="max-width:560px;margin:0 auto;padding:32px 20px;'
        'font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.5">'
        f'<div style="background:#ffffff;border-radius:10px;padding:28px;border-top:4px solid {color}">'
        f'<h1 style="margin:0 0 4px;font-size:20px;color:#1a1a1a">{e(name)}</h1>'
        f'<p style="margin:16px 0 4px">Tack för ditt besök!</p>'
        f'<h2 style="margin:24px 0 4px;font-size:16px">{e(r["title"])} &middot; {e(heading)}</h2>'
        f'<p style="margin:0 0 12px;color:#6b6b6b">{e(r["when"])}</p>'
        f'<table role="presentation" style="border-collapse:collapse;width:100%">{rows}'
        f'<tr><td style="padding:10px 12px 4px 0;border-top:1px solid #e5e5e5;font-weight:600">Totalt</td>'
        f'<td style="padding:10px 0 4px;border-top:1px solid #e5e5e5;text-align:right;font-weight:600">'
        f'{e(money(r["total"]))}</td></tr>{vat_rows}</table>'
        + "".join(f'<p style="margin:8px 0 0;color:#6b6b6b;font-size:13px">{e(t)}</p>' for t in extra)
        + f'<h3 style="margin:20px 0 4px;font-size:14px">Kortbetalning, köp</h3>'
        f'<table role="presentation" style="border-collapse:collapse;width:100%;font-size:13px">{card_rows}</table>'
        f'<p style="margin:16px 0 0;color:#6b6b6b;font-size:13px">Säljare: {"<br>".join(e(s) for s in seller_lines)}</p>'
        + (f'<p style="margin:24px 0 0;color:#6b6b6b">Frågor? Kontakta {e(name)}: {e(contact)}</p>' if contact else "")
        + '</div>'
        f'<p style="margin:16px 4px 0;color:#8a8a8a;font-size:12px">Du får det här mejlet för att du bad om '
        f'ett kvitto hos {e(name)}.</p></div></body></html>'
    )
    return subject, "\n".join(text), html_body

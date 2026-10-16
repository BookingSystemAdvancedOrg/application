"""Guest messages for food orders (SMS + e-mail), Swedish with English for
guests who ordered in English.

    order_confirmed  the receipt e-mail (paid) - no SMS
    order_started    SMS "the restaurant has started on your order, ready
                     around HH:MM" (pickup) or "... served at table X";
                     e-mail when no SMS can be sent
    order_ready      SMS "ready to collect" (pickup orders only); e-mail when
                     no SMS can be sent
    order_cancelled  SMS + e-mail: cancelled by the restaurant, refunded

The receipt shows the seller (legal name, organisation number, address),
each line, VAT per rate and the total. Pure functions; nothing here sends.
Everything the guest typed is escaped in HTML.
"""

import html
from datetime import datetime
from zoneinfo import ZoneInfo

from shared.messages import _brand_color, contact_line, money, restaurant_name

_TEXT = {
    "sv": {
        # SMS: short and plain - one SMS part (max 160 characters), no links.
        "sms.started.pickup": "{restaurant}: Beställning #{number} bekräftad.{ready_at}{where}",
        "sms.started.table": "{restaurant}: Beställning #{number} bekräftad. Serveras till {table}.",
        "sms.ready_at": " Klar ca {ready}.",
        "sms.where": " Adress: {address}",
        "started.subject": "Vi har börjat laga din beställning #{number} - {restaurant}",
        "started.lead.pickup": "Vi har börjat laga din beställning. Beräknat klar att hämta ca {ready}.",
        "started.lead.table": "Vi har börjat laga din beställning. Den serveras till {table}.",
        "sms.ready": "{restaurant}: Beställning #{number} är klar att hämta.{where}",
        "receipt.subject": "Kvitto beställning #{number} - {restaurant}",
        "receipt.lead.pickup": ("Tack för din beställning! Du får ett sms när restaurangen har börjat laga den "
                                "och ett när den är klar att hämta."),
        "receipt.lead.table": ("Tack för din beställning! Den serveras till {table}. Du får ett sms när "
                               "restaurangen har börjat laga den."),
        "ready.subject": "Din beställning #{number} är klar att hämta - {restaurant}",
        "ready.lead": "Din beställning är klar att hämta!",
        "cancelled.subject": "Din beställning #{number} är avbruten - {restaurant}",
        "cancelled.lead": ("Tyvärr har restaurangen behövt avbryta din beställning. Hela beloppet, {amount}, "
                           "betalas tillbaka till ditt kort inom några bankdagar."),
        "hello": "Hej {name}!",
        "receipt": "Kvitto",
        "order": "Beställning",
        "date": "Datum",
        "item": "Artikel",
        "qty": "Antal",
        "amount": "Belopp",
        "total": "Totalt",
        "vat": "Moms {rate} %",
        "net": "Netto",
        "payment": "Betalning",
        "payment.card": "Kort (Stripe)",
        "reference": "Referens",
        "seller": "Säljare",
        "org": "Org.nr",
        "follow": "Följ din beställning",
        "contact": "Frågor? Kontakta {restaurant}: {contact}",
        "footer": "Du får det här mejlet för att du har beställt hos {restaurant}.",
        "pickup.at": " Hämtas på {address}.",
    },
    "en": {
        "sms.started.pickup": "{restaurant}: Order #{number} confirmed.{ready_at}{where}",
        "sms.started.table": "{restaurant}: Order #{number} confirmed. Served at {table}.",
        "sms.ready_at": " Ready around {ready}.",
        "sms.where": " Address: {address}",
        "started.subject": "We've started on your order #{number} - {restaurant}",
        "started.lead.pickup": "We've started on your order. Ready to collect around {ready}.",
        "started.lead.table": "We've started on your order. It will be served at {table}.",
        "sms.ready": "{restaurant}: Order #{number} is ready for pickup.{where}",
        "receipt.subject": "Receipt order #{number} - {restaurant}",
        "receipt.lead.pickup": ("Thanks for your order! We'll text you when the restaurant starts on it "
                                "and when it's ready to collect."),
        "receipt.lead.table": ("Thanks for your order! It will be served at {table}. We'll text you when "
                               "the restaurant starts on it."),
        "ready.subject": "Your order #{number} is ready to collect - {restaurant}",
        "ready.lead": "Your order is ready to collect!",
        "cancelled.subject": "Your order #{number} is cancelled - {restaurant}",
        "cancelled.lead": ("Unfortunately the restaurant has had to cancel your order. The full amount, {amount}, "
                           "will be refunded to your card within a few business days."),
        "hello": "Hi {name},",
        "receipt": "Receipt",
        "order": "Order",
        "date": "Date",
        "item": "Item",
        "qty": "Qty",
        "amount": "Amount",
        "total": "Total",
        "vat": "VAT {rate} %",
        "net": "Net",
        "payment": "Payment",
        "payment.card": "Card (Stripe)",
        "reference": "Reference",
        "seller": "Seller",
        "org": "Org. no.",
        "follow": "Track your order",
        "contact": "Questions? Contact {restaurant}: {contact}",
        "footer": "You are receiving this email because you ordered from {restaurant}.",
        "pickup.at": " Collect at {address}.",
    },
}

# Food orders get two SMS only: the kitchen started (confirmation) and
# ready to collect. A cancelled order is an e-mail (with the refund), never
# an SMS - cancellation SMS are for table bookings.
SMS_NOTICES = ("order_started", "order_ready")
SMS_MAX = 160  # one SMS part (GSM-7) - a second part doubles the price


def _lang(item):
    return "en" if item.get("language") == "en" else "sv"


def _t(lang, key, **values):
    return _TEXT[lang][key].format(**values)


def _first(item):
    name = (item.get("customerName") or "").strip()
    return name.split()[0][:20] if name else ""


def _local(item, value, fmt):
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return moment.astimezone(ZoneInfo(item.get("timezone") or "Europe/Stockholm")).strftime(fmt)
    except (ValueError, KeyError):
        return ""


def _table(item, lang):
    label = item.get("tableLabel") or ""
    if lang == "sv":
        return label if label.lower().startswith("bord") else f"bord {label}"
    return label if label.lower().startswith("table") else f"table {label}"


def _seller(tenant_row, location):
    tenant_row = tenant_row or {}
    address = tenant_row.get("address")
    if isinstance(address, dict):
        street = str(address.get("street") or "").strip()
        town = " ".join(str(address.get(k) or "").strip() for k in ("postalCode", "city")).strip()
        address = ", ".join(p for p in (street, town) if p)
    return {
        "name": tenant_row.get("legalName") or tenant_row.get("name") or restaurant_name(tenant_row, location),
        "org": tenant_row.get("orgNumber"),
        "address": address or (location or {}).get("address"),
    }


def _clip(value, limit):
    value = " ".join(str(value or "").split())
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "."


def sms(kind, item, tenant_row, location, link=None):
    """The order SMS: confirmation (kitchen started) or ready to collect.
    Short and plain, one SMS part: restaurant, order number, ready time /
    table, pickup address. `link` is accepted for the caller's convenience
    and not used - the guest has it in the receipt e-mail."""
    if kind not in SMS_NOTICES:
        raise ValueError(f"no SMS for {kind}")
    lang = _lang(item)
    ready = _local(item, item.get("readyEstimate"), "%H:%M")
    address = _clip((location or {}).get("address"), 60)
    values = {
        "restaurant": _clip(restaurant_name(tenant_row, location), 30),
        "number": item.get("number") or "",
        "table": _clip(_table(item, lang), 20),
        "ready_at": _t(lang, "sms.ready_at", ready=ready) if ready else "",
        "where": _t(lang, "sms.where", address=address) if address else "",
    }
    if kind == "order_started":
        text = _t(lang, f"sms.started.{'table' if item.get('mode') == 'table' else 'pickup'}", **values)
    else:
        text = _t(lang, "sms.ready", **values)
    if len(text) > SMS_MAX:  # very long names: drop the address rather than pay for two parts
        text = _t(lang, f"sms.started.{'table' if item.get('mode') == 'table' else 'pickup'}"
                  if kind == "order_started" else "sms.ready", **{**values, "where": ""})
    return text[:SMS_MAX]


def email(kind, item, tenant_row, location, link):
    """(subject, text, html)."""
    lang = _lang(item)
    name = restaurant_name(tenant_row, location)
    number = item.get("number") or ""
    ready = _local(item, item.get("readyEstimate"), "%H:%M")
    amount = money((item.get("refund") or {}).get("amount") or item.get("totalAmount") or 0)
    if kind == "order_confirmed":
        subject = _t(lang, "receipt.subject", number=number, restaurant=name)
        lead = _t(lang, f"receipt.lead.{item.get('mode', 'pickup')}", ready=ready, table=_table(item, lang))
    elif kind == "order_started":
        subject = _t(lang, "started.subject", number=number, restaurant=name)
        lead = _t(lang, f"started.lead.{item.get('mode', 'pickup')}", ready=ready, table=_table(item, lang))
    elif kind == "order_ready":
        subject = _t(lang, "ready.subject", number=number, restaurant=name)
        lead = _t(lang, "ready.lead") + (_t(lang, "pickup.at", address=location["address"])
                                         if (location or {}).get("address") else "")
    else:
        subject = _t(lang, "cancelled.subject", number=number, restaurant=name)
        lead = _t(lang, "cancelled.lead", amount=amount)
    first = _first(item)
    hello = _t(lang, "hello", name=first).replace(" !", "!").replace(" ,", ",")
    contact = contact_line(tenant_row, location)
    contact_text = _t(lang, "contact", restaurant=name, contact=contact) if contact else ""
    footer = _t(lang, "footer", restaurant=name)
    show_receipt = kind == "order_confirmed"
    seller = _seller(tenant_row, location)
    when = _local(item, item.get("confirmedAt") or item.get("createdAt"), "%Y-%m-%d %H:%M")
    lines = item.get("lines") or []
    vat = item.get("vat") or []

    text = [hello, "", lead, ""]
    if show_receipt:
        text += [f"{_t(lang, 'receipt')} - {_t(lang, 'order')} #{number}", f"{_t(lang, 'date')}: {when}", ""]
        text += [f"{line['quantity']} x {line['name']}  {money(line['total'])}" for line in lines]
        text += ["", f"{_t(lang, 'total')}: {money(item.get('totalAmount') or 0)}"]
        text += [f"{_t(lang, 'vat', rate=v['rate'])}: {money(v['vat'])} ({_t(lang, 'net')} {money(v['net'])})"
                 for v in vat]
        text += [f"{_t(lang, 'payment')}: {_t(lang, 'payment.card')}",
                 f"{_t(lang, 'reference')}: {item.get('orderId')}", "",
                 f"{_t(lang, 'seller')}: {seller['name']}"]
        if seller["org"]:
            text.append(f"{_t(lang, 'org')}: {seller['org']}")
        if seller["address"]:
            text.append(seller["address"])
    if link and kind != "order_cancelled":
        text += ["", f"{_t(lang, 'follow')}:", link]
    if contact_text:
        text += ["", contact_text]
    text += ["", footer]

    e = html.escape
    color = _brand_color(tenant_row)
    receipt_html = ""
    if show_receipt:
        rows = "".join(
            f'<tr><td style="padding:4px 12px 4px 0">{e(str(line["quantity"]))} &times; {e(line["name"])}</td>'
            f'<td style="padding:4px 0;text-align:right;white-space:nowrap">{e(money(line["total"]))}</td></tr>'
            for line in lines)
        vat_rows = "".join(
            f'<tr><td style="padding:2px 12px 2px 0;color:#6b6b6b">{e(_t(lang, "vat", rate=v["rate"]))} '
            f'({e(_t(lang, "net"))} {e(money(v["net"]))})</td>'
            f'<td style="padding:2px 0;text-align:right;color:#6b6b6b">{e(money(v["vat"]))}</td></tr>'
            for v in vat)
        seller_lines = [seller["name"]] + ([f"{_t(lang, 'org')} {seller['org']}"] if seller["org"] else []) \
            + ([seller["address"]] if seller["address"] else [])
        receipt_html = (
            f'<h2 style="margin:24px 0 4px;font-size:16px">{e(_t(lang, "receipt"))} &middot; '
            f'{e(_t(lang, "order"))} #{e(str(number))}</h2>'
            f'<p style="margin:0 0 12px;color:#6b6b6b">{e(when)}</p>'
            f'<table role="presentation" style="border-collapse:collapse;width:100%">{rows}'
            f'<tr><td style="padding:10px 12px 4px 0;border-top:1px solid #e5e5e5;font-weight:600">'
            f'{e(_t(lang, "total"))}</td><td style="padding:10px 0 4px;border-top:1px solid #e5e5e5;'
            f'text-align:right;font-weight:600">{e(money(item.get("totalAmount") or 0))}</td></tr>'
            f'{vat_rows}</table>'
            f'<p style="margin:12px 0 0;color:#6b6b6b;font-size:13px">{e(_t(lang, "payment"))}: '
            f'{e(_t(lang, "payment.card"))} &middot; {e(_t(lang, "reference"))}: {e(item.get("orderId") or "")}</p>'
            f'<p style="margin:12px 0 0;color:#6b6b6b;font-size:13px">{e(_t(lang, "seller"))}: '
            f'{"<br>".join(e(s) for s in seller_lines)}</p>'
        )
    button = (
        f'<p style="margin:24px 0 8px"><a href="{e(link, quote=True)}" style="background:{color};'
        f'color:#ffffff;text-decoration:none;padding:12px 22px;border-radius:6px;display:inline-block;'
        f'font-weight:600">{e(_t(lang, "follow"))}</a></p>' if link and kind != "order_cancelled" else "")
    html_body = (
        '<!doctype html><html><body style="margin:0;background:#f4f2ee">'
        '<div style="max-width:560px;margin:0 auto;padding:32px 20px;'
        'font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.5">'
        f'<div style="background:#ffffff;border-radius:10px;padding:28px;border-top:4px solid {color}">'
        f'<h1 style="margin:0 0 4px;font-size:20px;color:#1a1a1a">{e(name)}</h1>'
        f'<p style="margin:16px 0 4px">{e(hello)}</p><p style="margin:0 0 8px">{e(lead)}</p>'
        f'{receipt_html}{button}'
        + (f'<p style="margin:24px 0 0;color:#6b6b6b">{e(contact_text)}</p>' if contact_text else "")
        + '</div>'
        f'<p style="margin:16px 4px 0;color:#8a8a8a;font-size:12px">{e(footer)}</p>'
        '</div></body></html>'
    )
    return subject, "\n".join(text), html_body

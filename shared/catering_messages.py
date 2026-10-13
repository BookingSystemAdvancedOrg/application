"""Catering messages (Swedish) for the guest and the restaurant.

    notice                      guest                        restaurant
    catering_request_received   e-mail (link)                SMS + e-mail (admin link)
    catering_offer_sent         e-mail + SMS (link)          -
    catering_change_requested   -                            SMS + e-mail
    catering_rejected           -                            SMS + e-mail
    catering_declined           e-mail + SMS                 -
    catering_signed             e-mail (pay / invoice next)  SMS + e-mail
    catering_confirmed          e-mail                       e-mail
    catering_cancelled          e-mail + SMS                 -
    catering_expired            e-mail                       e-mail

Pure functions: request + tenant + location in, (subject, text, html) /
SMS text out. Everything the guest typed is escaped in HTML.
"""

import html

from shared.catering import COMPANY, money
from shared.messages import _brand_color, contact_line, restaurant_name

# (guest email, guest sms, owner email, owner sms)
ROUTING = {
    "catering_request_received": (True, False, True, True),
    "catering_offer_sent": (True, True, False, False),
    "catering_change_requested": (False, False, True, True),
    "catering_rejected": (False, False, True, True),
    "catering_declined": (True, True, False, False),
    "catering_signed": (True, False, True, True),
    "catering_confirmed": (True, False, True, False),
    "catering_cancelled": (True, True, False, False),
    "catering_expired": (True, False, True, False),
}


def _who(item):
    if item.get("customerType") == COMPANY and item.get("companyName"):
        return f"{item['companyName']} ({item.get('customerName')})"
    return item.get("customerName") or "Gästen"


def _when(item):
    return f"{item.get('deliveryDate')} kl {item.get('deliveryTime')}"


def _total(item):
    offer = item.get("offer") or {}
    total = (offer.get("totals") or {}).get("total") or (item.get("request") or {}).get("total") or 0
    return money(total)


def _first(item):
    name = (item.get("customerName") or "").strip()
    return name.split()[0] if name else ""


def guest_email(kind, item, tenant_row, location, link):
    name = restaurant_name(tenant_row, location)
    when, total = _when(item), _total(item)
    signed_company = item.get("customerType") == COMPANY
    texts = {
        "catering_request_received": (
            f"Vi har tagit emot din cateringförfrågan - {name}",
            f"Tack för din förfrågan om catering {when}. Restaurangen går igenom den och skickar en offert "
            f"till dig. Du kan följa förfrågan via länken nedan.",
            "Visa din förfrågan"),
        "catering_offer_sent": (
            f"Din cateringoffert från {name}",
            f"Här är offerten för catering {when}, totalt {total} inkl. moms. Du godkänner den genom att "
            f"signera med BankID - eller ber om ändringar - via länken nedan. Offerten gäller till och med "
            f"{(item.get('offer') or {}).get('validUntil') or item.get('validUntil')}.",
            "Visa och signera offerten"),
        "catering_declined": (
            f"Din cateringförfrågan - {name}",
            "Tyvärr kan restaurangen inte ta emot din cateringbeställning den här gången."
            + (f" Meddelande från restaurangen: {item.get('declineReason')}" if item.get("declineReason") else ""),
            None),
        "catering_signed": (
            f"Offerten är signerad - {name}",
            ("Tack! Offerten är signerad med BankID. Fakturan skickas till dig via e-post från restaurangens "
             "betalsystem (Stripe).") if signed_company else
            ("Tack! Offerten är signerad med BankID. Sista steget är betalningen - betala via länken nedan "
             "så är beställningen bekräftad."),
            "Visa beställningen" if signed_company else "Betala beställningen"),
        "catering_confirmed": (
            f"Din catering är bekräftad - {name}",
            f"Din cateringbeställning {when} är bekräftad. Totalt {total} inkl. moms."
            + (" Fakturan har skickats till din e-post." if signed_company else " Kvittot har skickats till din e-post."),
            "Visa beställningen"),
        "catering_cancelled": (
            f"Din catering är avbokad - {name}",
            "Tyvärr har restaurangen behövt avboka din cateringbeställning."
            + (f" Meddelande: {item.get('cancelReason')}" if item.get("cancelReason") else "")
            + " Det du har betalat betalas tillbaka. Kontakta restaurangen om du har frågor.",
            None),
        "catering_expired": (
            f"Din cateringförfrågan har gått ut - {name}",
            "Förfrågan eller offerten gick ut innan den blev klar. Skicka gärna en ny förfrågan om du "
            "fortfarande vill beställa catering.",
            None),
    }
    subject, lead, button = texts[kind]
    return _render(subject, f"Hej {_first(item)}!".replace("Hej !", "Hej!"), lead, link if button else None,
                   button, tenant_row, location, footer=f"Du får det här mejlet för att du har kontaktat {name} "
                   "om catering.")


def guest_sms(kind, item, tenant_row, location, link):
    name = restaurant_name(tenant_row, location)
    texts = {
        "catering_offer_sent": f"{name}: Din cateringoffert ({_when(item)}, {_total(item)}) är klar. "
                               f"Signera med BankID: {link}",
        "catering_declined": f"{name}: Tyvärr kan vi inte ta emot din cateringbeställning {_when(item)}. "
                             "Se mejlet för mer information.",
        "catering_cancelled": f"{name}: Tyvärr har vi behövt avboka din catering {_when(item)}. "
                              "Betalningen återbetalas. Se mejlet.",
    }
    return texts[kind]


def owner_email(kind, item, tenant_row, location, admin_link):
    name = restaurant_name(tenant_row, location)
    who, when, total = _who(item), _when(item), _total(item)
    portions = (item.get("request") or {}).get("portions")
    texts = {
        "catering_request_received": (
            f"Ny cateringförfrågan {item.get('deliveryDate')} - {who}",
            f"{who} har skickat en cateringförfrågan: {portions} portioner, {when}, "
            f"{'leverans' if item.get('fulfilment') == 'delivery' else 'avhämtning'}, enligt kalkylen {total}. "
            "Öppna förfrågan för att skicka en offert eller tacka nej."),
        "catering_change_requested": (
            f"Ändringar önskas i cateringofferten - {who}",
            f"{who} vill ändra offerten för {when}: \"{item.get('changeMessage') or ''}\". "
            "Öppna förfrågan och skicka en ny version."),
        "catering_rejected": (
            f"Cateringofferten avböjdes - {who}",
            f"{who} har avböjt offerten för {when}."
            + (f" Anledning: {item.get('rejectReason')}" if item.get("rejectReason") else "")),
        "catering_signed": (
            f"Cateringofferten är signerad - {who}",
            f"{who} har signerat offerten för {when} ({total}) med BankID. "
            + ("Fakturan skapas och skickas automatiskt." if item.get("customerType") == COMPANY
               else "Gästen betalar nu online; du får besked när betalningen är klar.")),
        "catering_confirmed": (
            f"Catering bekräftad {item.get('deliveryDate')} - {who}",
            f"Cateringen {when} för {who} är bekräftad ({total}). "
            + ("Fakturan är skickad." if item.get("customerType") == COMPANY else "Betalningen är klar.")),
        "catering_expired": (
            f"Cateringförfrågan gick ut - {who}",
            f"Förfrågan/offerten för {when} från {who} gick ut utan att bli klar."),
    }
    subject, lead = texts[kind]
    return _render(subject, "Hej!", lead, admin_link, "Öppna i admin", tenant_row, location,
                   footer=f"Skickat från {name}s bokningssystem.")


def owner_sms(kind, item, tenant_row, location, admin_link):
    who, when = _who(item), _when(item)
    texts = {
        "catering_request_received": f"Ny cateringförfrågan från {who} ({when}). Öppna: {admin_link}",
        "catering_change_requested": f"{who} vill ändra cateringofferten ({when}). Öppna: {admin_link}",
        "catering_rejected": f"{who} avböjde cateringofferten ({when}). {admin_link}",
        "catering_signed": f"{who} har signerat cateringofferten ({when}, {_total(item)}). {admin_link}",
    }
    return texts[kind]


def _render(subject, hello, lead, link, button, tenant_row, location, *, footer):
    name = restaurant_name(tenant_row, location)
    contact = contact_line(tenant_row, location)
    text = [hello, "", lead]
    if link and button:
        text += ["", f"{button}:", link]
    if contact:
        text += ["", f"Frågor? Kontakta {name}: {contact}"]
    text += ["", footer]
    e = html.escape
    color = _brand_color(tenant_row)
    button_html = (
        f'<p style="margin:24px 0 8px"><a href="{e(link, quote=True)}" style="background:{color};color:#ffffff;'
        f'text-decoration:none;padding:12px 22px;border-radius:6px;display:inline-block;font-weight:600">'
        f'{e(button)}</a></p>' if link and button else "")
    html_body = (
        '<!doctype html><html><body style="margin:0;background:#f4f2ee">'
        '<div style="max-width:560px;margin:0 auto;padding:32px 20px;'
        'font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.5">'
        f'<div style="background:#ffffff;border-radius:10px;padding:28px;border-top:4px solid {color}">'
        f'<h1 style="margin:0 0 4px;font-size:20px;color:#1a1a1a">{e(name)}</h1>'
        f'<p style="margin:16px 0 4px">{e(hello)}</p><p style="margin:0 0 8px">{e(lead)}</p>{button_html}'
        + (f'<p style="margin:24px 0 0;color:#6b6b6b">Frågor? Kontakta {e(name)}: {e(contact)}</p>' if contact else "")
        + f'</div><p style="margin:16px 4px 0;color:#8a8a8a;font-size:12px">{e(footer)}</p></div></body></html>'
    )
    return subject, "\n".join(text), html_body

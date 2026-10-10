"""Guest and restaurant messages for table bookings (email + SMS), Swedish
with English for guests who booked in English.

Pure functions: booking + tenant + location in, text out. Everything a
guest typed is escaped in HTML; nothing here sends anything.
"""

import html
import re
from datetime import date as _date

_WEEKDAYS = {
    "sv": ("måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag"),
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
}
_MONTHS = {
    "sv": ("januari", "februari", "mars", "april", "maj", "juni", "juli", "augusti",
           "september", "oktober", "november", "december"),
    "en": ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"),
}
_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
_DEFAULT_COLOR = "#1f2a24"

_TEXT = {
    "sv": {
        "hello": "Hej {name}!",
        "confirmed.subject": "Bokningsbekräftelse - {restaurant}",
        "confirmed.lead": "Ditt bord är bokat. Välkommen!",
        "changed.subject": "Din bokning är ändrad - {restaurant}",
        "changed.lead": "Restaurangen har ändrat din bokning. Så här ser den ut nu:",
        "cancelled_by_guest.subject": "Avbokning bekräftad - {restaurant}",
        "cancelled_by_guest.lead": "Din bokning är avbokad. Hoppas vi ses en annan gång!",
        "cancelled_by_restaurant.subject": "Din bokning är avbokad - {restaurant}",
        "cancelled_by_restaurant.lead": ("Tyvärr har restaurangen behövt avboka din bokning. "
                                         "Hör av dig om du har frågor eller vill boka en ny tid."),
        "reminder.subject": "Påminnelse: bord {when} - {restaurant}",
        "reminder.lead": "En påminnelse om din bokning. Vi ser fram emot ditt besök!",
        "fee_charged.subject": "Kvitto: {fee_label} - {restaurant}",
        "fee_charged.lead": ("Enligt villkoren du godkände vid bokningen har vi debiterat ditt kort "
                             "{amount} ({fee_label_lower}). Kontakta restaurangen om du har frågor."),
        "fee.no_show": "Avgift för utebliven gäst",
        "fee.late_cancel": "Avgift för sen avbokning",
        "amount": "Belopp",
        "restaurant": "Restaurang",
        "date": "Datum",
        "time": "Tid",
        "guests": "Antal",
        "guests.value": "{n} personer",
        "guests.one": "1 person",
        "address": "Adress",
        "manage": "Se eller avboka din bokning",
        "manage.reminder": "Kan du inte komma? Avboka här, så kan någon annan få bordet",
        "contact": "Frågor? Kontakta {restaurant}: {contact}",
        "footer": "Du får det här mejlet för att du har bokat bord hos {restaurant}.",
        "sms.confirmed": "{restaurant}: Ditt bord är bokat {when}, {guests}.",
        "sms.changed": "{restaurant}: Din bokning är ändrad till {when}, {guests}.",
        "sms.cancelled_by_guest": "{restaurant}: Din bokning {when} är avbokad.",
        "sms.cancelled_by_restaurant": "{restaurant}: Tyvärr har vi behövt avboka din bokning {when}. Kontakta oss: {contact}",
        "sms.reminder": "{restaurant}: Påminnelse om ditt bord {when}, {guests}.",
        "sms.fee_charged": "{restaurant}: {fee_label} {amount} har debiterats för bokningen {when}.",
        "sms.manage": "Se/avboka: {link}",
        "when": "{weekday} {day} {month} kl {time}",
    },
    "en": {
        "hello": "Hi {name},",
        "confirmed.subject": "Booking confirmation - {restaurant}",
        "confirmed.lead": "Your table is booked. See you soon!",
        "changed.subject": "Your booking has changed - {restaurant}",
        "changed.lead": "The restaurant has changed your booking. This is how it looks now:",
        "cancelled_by_guest.subject": "Cancellation confirmed - {restaurant}",
        "cancelled_by_guest.lead": "Your booking is cancelled. We hope to see you another time!",
        "cancelled_by_restaurant.subject": "Your booking is cancelled - {restaurant}",
        "cancelled_by_restaurant.lead": ("Unfortunately the restaurant has had to cancel your booking. "
                                         "Get in touch if you have questions or want a new time."),
        "reminder.subject": "Reminder: table {when} - {restaurant}",
        "reminder.lead": "A reminder about your booking. We look forward to your visit!",
        "fee_charged.subject": "Receipt: {fee_label} - {restaurant}",
        "fee_charged.lead": ("As agreed in the terms you accepted when booking, your card has been "
                             "charged {amount} ({fee_label_lower}). Contact the restaurant with any questions."),
        "fee.no_show": "No-show fee",
        "fee.late_cancel": "Late cancellation fee",
        "amount": "Amount",
        "restaurant": "Restaurant",
        "date": "Date",
        "time": "Time",
        "guests": "Guests",
        "guests.value": "{n} people",
        "guests.one": "1 person",
        "address": "Address",
        "manage": "View or cancel your booking",
        "manage.reminder": "Can't make it? Cancel here so someone else can have the table",
        "contact": "Questions? Contact {restaurant}: {contact}",
        "footer": "You are receiving this email because you booked a table at {restaurant}.",
        "sms.confirmed": "{restaurant}: Your table is booked {when}, {guests}.",
        "sms.changed": "{restaurant}: Your booking is now {when}, {guests}.",
        "sms.cancelled_by_guest": "{restaurant}: Your booking {when} is cancelled.",
        "sms.cancelled_by_restaurant": "{restaurant}: Unfortunately we had to cancel your booking {when}. Contact us: {contact}",
        "sms.reminder": "{restaurant}: Reminder of your table {when}, {guests}.",
        "sms.fee_charged": "{restaurant}: {fee_label} {amount} has been charged for your booking {when}.",
        "sms.manage": "View/cancel: {link}",
        "when": "{weekday} {day} {month} at {time}",
    },
}

GUEST_NOTICES = ("confirmed", "changed", "cancelled_by_guest", "cancelled_by_restaurant", "reminder",
                 "fee_charged")
STAFF_ONLY_NOTICES = ("fee_failed",)


def money(ore):
    kronor = int(ore) / 100
    text = f"{kronor:,.2f}".replace(",", " ").replace(".", ",")
    return (text[:-3] if text.endswith(",00") else text) + " kr"


def _fee(item, lang):
    payment = item.get("payment") or {}
    label = _t(lang, f"fee.{payment.get('kind', 'no_show')}")
    return label, money(payment.get("amount") or 0)
_WITH_LINK = ("confirmed", "changed", "reminder")


def _lang(item):
    return "en" if item.get("language") == "en" else "sv"


def _t(lang, key, **values):
    return _TEXT[lang][key].format(**values)


def when(item, lang=None):
    lang = lang or _lang(item)
    day = _date.fromisoformat(item["date"])
    return _t(lang, "when", weekday=_WEEKDAYS[lang][day.weekday()], day=day.day,
              month=_MONTHS[lang][day.month - 1], time=item["startTime"])


def _long_date(item, lang):
    day = _date.fromisoformat(item["date"])
    text = f"{_WEEKDAYS[lang][day.weekday()]} {day.day} {_MONTHS[lang][day.month - 1]} {day.year}"
    return text[:1].upper() + text[1:]


def _guests(item, lang):
    n = int(item.get("partySize") or 0)
    return _t(lang, "guests.one") if n == 1 else _t(lang, "guests.value", n=n)


def restaurant_name(tenant_row, location):
    tenant_name = (tenant_row or {}).get("name") or ""
    location_name = (location or {}).get("name") or ""
    if tenant_name and location_name and location_name.lower() not in tenant_name.lower():
        return f"{tenant_name} {location_name}"
    return tenant_name or location_name or "Restaurangen"


def contact_line(tenant_row, location):
    location = location or {}
    tenant_row = tenant_row or {}
    parts = [location.get("phoneNumber") or tenant_row.get("contactPhone"),
             location.get("email") or tenant_row.get("replyToEmail") or tenant_row.get("contactEmail")]
    return ", ".join(p for p in parts if p)


def _brand_color(tenant_row):
    color = ((tenant_row or {}).get("branding") or {}).get("primaryColor")
    return color if isinstance(color, str) and _COLOR.fullmatch(color) else _DEFAULT_COLOR


def guest_email(kind, item, tenant_row, location, link):
    """(subject, text, html) for a guest notice."""
    lang = _lang(item)
    name = restaurant_name(tenant_row, location)
    contact = contact_line(tenant_row, location)
    first_name = (item.get("customerName") or "").split(" ")[0] or ("there" if lang == "en" else "")
    rows = [
        (_t(lang, "restaurant"), name),
        (_t(lang, "date"), _long_date(item, lang)),
        (_t(lang, "time"), f"{item['startTime']}-{item['endTime']}"),
        (_t(lang, "guests"), _guests(item, lang)),
    ]
    if (location or {}).get("address"):
        rows.append((_t(lang, "address"), location["address"]))
    fee_label, amount = _fee(item, lang)
    subject = _t(lang, f"{kind}.subject", restaurant=name, when=when(item, lang), fee_label=fee_label)
    lead = _t(lang, f"{kind}.lead", amount=amount, fee_label_lower=fee_label.lower())
    if kind == "fee_charged":
        rows.append((_t(lang, "amount"), amount))
    hello = _t(lang, "hello", name=first_name).replace(" !", "!").replace(" ,", ",")
    manage_label = _t(lang, "manage.reminder" if kind == "reminder" else "manage")
    show_link = bool(link) and kind in _WITH_LINK
    contact_text = _t(lang, "contact", restaurant=name, contact=contact) if contact else ""
    footer = _t(lang, "footer", restaurant=name)

    text_lines = [hello, "", lead, ""]
    text_lines += [f"{label}: {value}" for label, value in rows]
    if show_link:
        text_lines += ["", f"{manage_label}:", link]
    if contact_text:
        text_lines += ["", contact_text]
    text_lines += ["", footer]
    text = "\n".join(text_lines)

    e = html.escape
    color = _brand_color(tenant_row)
    table_rows = "".join(
        f'<tr><td style="padding:6px 16px 6px 0;color:#6b6b6b;white-space:nowrap">{e(label)}</td>'
        f'<td style="padding:6px 0;color:#1a1a1a">{e(str(value))}</td></tr>'
        for label, value in rows
    )
    button = (
        f'<p style="margin:28px 0 8px"><a href="{e(link, quote=True)}" '
        f'style="background:{color};color:#ffffff;text-decoration:none;padding:12px 22px;'
        f'border-radius:6px;display:inline-block;font-weight:600">{e(manage_label)}</a></p>'
        if show_link else ""
    )
    html_body = (
        '<!doctype html><html><body style="margin:0;background:#f4f2ee">'
        '<div style="max-width:560px;margin:0 auto;padding:32px 20px;'
        'font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.5">'
        f'<div style="background:#ffffff;border-radius:10px;padding:28px;border-top:4px solid {color}">'
        f'<h1 style="margin:0 0 4px;font-size:20px;color:#1a1a1a">{e(name)}</h1>'
        f'<p style="margin:16px 0 4px">{e(hello)}</p><p style="margin:0 0 20px">{e(lead)}</p>'
        f'<table role="presentation" style="border-collapse:collapse">{table_rows}</table>'
        f'{button}'
        + (f'<p style="margin:24px 0 0;color:#6b6b6b">{e(contact_text)}</p>' if contact_text else "")
        + '</div>'
        f'<p style="margin:16px 4px 0;color:#8a8a8a;font-size:12px">{e(footer)}</p>'
        '</div></body></html>'
    )
    return subject, text, html_body


def guest_sms(kind, item, tenant_row, location, link):
    lang = _lang(item)
    name = restaurant_name(tenant_row, location)
    fee_label, amount = _fee(item, lang)
    text = _t(lang, f"sms.{kind}", restaurant=name, when=when(item, lang), guests=_guests(item, lang),
              contact=contact_line(tenant_row, location) or name, fee_label=fee_label, amount=amount)
    if link and kind in _WITH_LINK:
        text += " " + _t(lang, "sms.manage", link=link)
    return text


# --- restaurant ---------------------------------------------------------------

def staff_email(kind, item, tenant_row, location, admin_url):
    """(subject, text) for the restaurant: a new online booking or a guest
    cancellation. Swedish - the admin is Swedish."""
    name = restaurant_name(tenant_row, location)
    tables = ", ".join(item.get("tableIds") or [])
    lines = [
        f"Namn: {item.get('customerName') or '-'}",
        f"Datum: {_long_date(item, 'sv')}",
        f"Tid: {item['startTime']}-{item['endTime']}",
        f"Antal: {_guests(item, 'sv')}",
        f"Bord: {tables or '-'}",
        f"Telefon: {item.get('customerPhone') or '-'}",
        f"E-post: {item.get('customerEmail') or '-'}",
    ]
    if item.get("notes"):
        lines.append(f"Meddelande: {item['notes']}")
    if kind == "fee_failed":
        fee_label, amount = _fee(item, "sv")
        error = (item.get("payment") or {}).get("error") or "okänt fel"
        subject = f"Avgiften kunde inte dras: {fee_label.lower()} {amount} - {name}"
        lead = (f"{fee_label} på {amount} kunde inte dras från gästens kort ({error}). "
                "Försök igen från bokningen i admin, eller kontakta gästen.")
    elif kind == "confirmed":
        subject = f"Ny bokning {item['date']} {item['startTime']}, {_guests(item, 'sv')} - {name}"
        lead = "En gäst har bokat bord via er webbplats."
    else:
        subject = f"Avbokning {item['date']} {item['startTime']}, {_guests(item, 'sv')} - {name}"
        lead = "En gäst har avbokat sin bokning. Bordet är frigjort."
    body = [lead, ""] + lines
    if admin_url:
        body += ["", f"Se dagens bokningar: {admin_url.rstrip('/')}/bokningar?date={item['date']}"]
    return subject, "\n".join(body)

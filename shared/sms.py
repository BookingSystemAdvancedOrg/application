"""How many parts an SMS is sent (and billed) as.

Carriers and AWS charge per part: a message in the GSM 03.38 alphabet
fits 160 characters in one part, anything else (emoji, most non-Latin
scripts) is sent as UCS-2 and fits 70; longer messages are split into
parts of 153 or 67 characters (the rest carries the concatenation
header). Characters in the GSM extension table (^ { } \\ [ ~ ] | and the
euro sign) take two characters.

    from shared import sms
    sms.parts("Hej! Ditt bord är bokat ...")   # -> 1

Used by the notification Lambda at send time, so the restaurant's day row
(shared/finance.py) counts parts, which is what the invoice charges.

Where an SMS may go: ``allowed_prefixes()`` (SMS_ALLOWED_PREFIXES, default
the Nordic codes; the notification Lambda's rule) and ``textable()``, the
stricter rule for the card link SMS Booqy sends itself for a staff phone
booking: a Nordic mobile number only, so a landline never sits waiting for
a link that cannot arrive.
"""

import os
import re

GSM_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM_EXTENSION = set("^{}\\[~]|€\f")

SINGLE_GSM, MULTI_GSM = 160, 153
SINGLE_UCS2, MULTI_UCS2 = 70, 67


def is_gsm(text):
    return all(ch in GSM_BASIC or ch in GSM_EXTENSION for ch in text)


def length(text):
    """Characters as the network counts them: extension characters are two
    in GSM-7; in UCS-2 every code unit counts (astral emoji are two)."""
    if is_gsm(text):
        return sum(2 if ch in GSM_EXTENSION else 1 for ch in text)
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def parts(text):
    """Number of parts the message is sent as (0 for an empty text)."""
    if not text:
        return 0
    gsm = is_gsm(text)
    n = length(text)
    single, multi = (SINGLE_GSM, MULTI_GSM) if gsm else (SINGLE_UCS2, MULTI_UCS2)
    if n <= single:
        return 1
    return -(-n // multi)


# --- where an SMS may go --------------------------------------------------------------------

NORDIC_PREFIXES = ("+46", "+45", "+47", "+358", "+354")
# Mobile ranges (E.164): Swedish 070, 072, 073, 076 and 079; Norwegian 4 and
# 9; every Danish number; Finnish 04x and 050; Icelandic 6, 7 and 8.
_MOBILE = (
    ("+46", re.compile(r"\+467[02369]\d{7}")),
    ("+47", re.compile(r"\+47[49]\d{7}")),
    ("+45", re.compile(r"\+45\d{8}")),
    ("+358", re.compile(r"\+358(?:4\d|50)\d{4,8}")),
    ("+354", re.compile(r"\+354[678]\d{6}")),
)


def allowed_prefixes():
    """The country codes an SMS may go to: SMS_ALLOWED_PREFIXES
    (comma-separated) or the Nordic codes."""
    raw = os.environ.get("SMS_ALLOWED_PREFIXES") or ",".join(NORDIC_PREFIXES)
    return tuple(p.strip() for p in raw.split(",") if p.strip().startswith("+"))


def textable(phone):
    """An E.164 number Booqy may text a card link to: within
    allowed_prefixes() and a Nordic mobile number (a Swedish landline such
    as +468... is refused)."""
    if not isinstance(phone, str) or not phone.startswith(allowed_prefixes()):
        return False
    return any(phone.startswith(code) and pattern.fullmatch(phone) for code, pattern in _MOBILE)


def filler(phone):
    """A made-up number rather than the guest's own: the national digits
    after the first two are all the same (070 000 00 00, 070 111 11 11)."""
    if not isinstance(phone, str):
        return False
    code = next((c for c, _ in _MOBILE if phone.startswith(c)), None)
    rest = phone[len(code) + 2:] if code else ""
    return len(rest) >= 4 and rest.isdigit() and len(set(rest)) == 1

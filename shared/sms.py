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
"""

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

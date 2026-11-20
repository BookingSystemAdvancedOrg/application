"""How Booqy says a slot in the call's language (retest 2 on dev,
2026-11-20: the model read back "fredag den sjunde november" for the 27th
and then "Rättelse: ..."). Every availability and hold result carries the
server-built phrase (`spoken`) and the prompt says to read back exactly
that, so the model never builds a day, a time or a part of the day itself.

One rule for the part of the day, from the slot's time; a whole hour on a
border belongs to the earlier part, so 17:00 is "fem på eftermiddagen" and
17:30 "halv sex på kvällen":

    after 05 until 10   morgonen      (morning)
    after 10 until 12   förmiddagen   (morning in English)
    after 12 until 17   eftermiddagen (afternoon)
    after 17            kvällen       (evening)
    until 05            natten        (night)

12:00 is "mitt på dagen" (noon); 00:00 is "tolv på natten".
Languages: sv, en, no, da, de; any other gets None (the result's English
readAs is translated by the model)."""

from __future__ import annotations

import re
from datetime import date

from . import readback

LANGS = ("sv", "en", "no", "da", "de")

_WEEKDAYS = {
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "de": ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"),
}
_MONTHS = {
    "en": ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"),
    "de": ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
           "November", "Dezember"),
}
# The parts of the day, in the order night, morning, late morning, afternoon, evening.
_PARTS = {
    "sv": ("på natten", "på morgonen", "på förmiddagen", "på eftermiddagen", "på kvällen"),
    "en": ("at night", "in the morning", "in the morning", "in the afternoon", "in the evening"),
    "no": ("om natten", "om morgenen", "på formiddagen", "på ettermiddagen", "om kvelden"),
    "da": ("om natten", "om morgenen", "om formiddagen", "om eftermiddagen", "om aftenen"),
    "de": ("nachts", "morgens", "vormittags", "nachmittags", "abends"),
}
_NOON = {"sv": "klockan tolv mitt på dagen", "en": "at twelve noon", "no": "klokka tolv midt på dagen",
         "da": "klokken tolv middag", "de": "um zwölf Uhr mittags"}
_AT = {"sv": "klockan", "no": "klokka", "da": "klokken", "de": "um", "en": "at"}
# (half, quarter past, quarter to): "halv sex" counts toward the next hour, "half past five" does not.
_QUARTERS = {"sv": ("halv", "kvart över", "kvart i"), "no": ("halv", "kvart over", "kvart på"),
             "da": ("halv", "kvart over", "kvart i"), "de": ("halb", "Viertel nach", "Viertel vor"),
             "en": ("half past", "quarter past", "quarter to")}
_PERSON = {"sv": ("en person", "personer"), "no": ("en person", "personer"), "da": ("en person", "personer"),
           "de": ("eine Person", "Personen"), "en": ("one person", "people")}


def _words(lang):
    return readback._NUMBERS[lang].split()


def number(n, lang):
    """A whole number in words (0 to 999 in sv and en, 0 to 23 in no, da, de; digits above)."""
    from .tools import number_words           # one source for the Swedish and English numbers
    n = int(n)
    if lang in ("sv", "en"):
        return number_words(n, lang)
    words = _words(lang)
    return words[n] if 0 <= n < len(words) else str(n)


def ordinal(day, lang):
    """The day of the month as an ordinal: 27 -> "tjugosjunde" (sv), "twenty-seventh" (en)."""
    word = readback._ORDINALS[lang].split()[int(day) - 1]
    return word + "n" if lang == "de" else word        # "den siebenundzwanzigsten"


def part(minutes):
    """The index into _PARTS for a time in minutes after midnight (the border rule above)."""
    if minutes <= 5 * 60:
        return 0
    if minutes <= 10 * 60:
        return 1
    if minutes <= 12 * 60:
        return 2
    if minutes <= 17 * 60:
        return 3
    return 4


def period(time, lang):
    """"17:30" -> "på kvällen"."""
    hour, minute = (int(x) for x in str(time).split(":"))
    return _PARTS[lang][part(hour * 60 + minute)]


def _hour(h, lang):
    h = h % 12 or 12
    if lang == "de" and h == 1:
        return "ein"
    if lang == "en":
        return number(h, "en")
    return _words(lang)[h]


def clock(time, lang):
    """"17:30" -> "klockan halv sex på kvällen", "at half past five in the evening"."""
    if lang not in LANGS:
        return None
    hour, minute = (int(x) for x in str(time).split(":"))
    if hour == 12 and minute == 0:
        return _NOON[lang]
    return f"{_AT[lang]} {_face(hour, minute, lang)} {period(time, lang)}"


def _face(hour, minute, lang):
    """The clock face without "klockan" and the part of the day: "halv sex", "half past five"."""
    half, past, to = _QUARTERS[lang]
    this, nxt = _hour(hour, lang), _hour(hour + 1, lang)
    if lang == "de" and nxt == "ein":
        nxt = "eins"                                # "halb eins", "Viertel vor eins"
    if minute == 0:
        said = f"{this} o'clock" if lang == "en" else f"{this} Uhr" if lang == "de" else this
    elif minute == 30:
        said = f"{half} {this}" if lang == "en" else f"{half} {nxt}"
    elif minute == 15:
        said = f"{past} {this}"
    elif minute == 45:
        said = f"{to} {nxt}"
    elif lang == "sv":
        said = f"{number(minute, 'sv')} över {this}" if minute < 30 else f"{number(60 - minute, 'sv')} i {nxt}"
    elif lang == "en":
        said = f"{number(minute, 'en')} minutes past {this}"
    else:
        said = f"{hour:02d}.{minute:02d}"
    return said


def day(iso, lang):
    """"2026-11-27" -> "fredag den tjugosjunde november", "Friday the twenty-seventh of November"."""
    if lang not in LANGS:
        return None
    when = date.fromisoformat(str(iso)[:10])
    if lang == "en":
        return f"{_WEEKDAYS['en'][when.weekday()]} the {ordinal(when.day, 'en')} of {_MONTHS['en'][when.month - 1]}"
    if lang == "de":
        return (f"{_WEEKDAYS['de'][when.weekday()]}, den {ordinal(when.day, 'de')} "
                f"{_MONTHS['de'][when.month - 1]}")
    return (f"{readback.WEEKDAYS[lang][when.weekday()]} den {ordinal(when.day, lang)} "
            f"{readback.MONTHS[lang][when.month - 1]}")


def date_only(iso, lang):
    """The day of the month and the month without the weekday: "den tjugosjunde november"."""
    said = day(iso, lang)
    if said is None:
        return None
    return said.split(" ", 1)[1]


def guests(n, lang):
    """2 -> "två personer"; 1 -> "en person"."""
    if lang not in LANGS:
        return None
    one, many = _PERSON[lang]
    n = int(n)
    if n == 1:
        return one
    return f"{number(n, lang)} {many}"


def slot(iso, time, party, lang):
    """The whole slot as it is read back: "fredag den tjugosjunde november klockan fem på eftermiddagen,
    två personer". None in a language without a spoken form here."""
    if lang not in LANGS:
        return None
    said = f"{day(iso, lang)} {clock(time, lang)}"
    return f"{said}, {guests(party, lang)}" if party else said


def times(values, lang):
    """{"17:00": "klockan fem på eftermiddagen", ...} for every distinct "HH:MM" in `values`."""
    if lang not in LANGS:
        return None
    return {t: clock(t, lang) for t in dict.fromkeys(values) if isinstance(t, str) and len(t) == 5}


_OR = {"sv": "eller", "no": "eller", "da": "eller", "de": "oder", "en": "or"}


def _either(words, lang):
    """"A eller B", "A, B eller C"."""
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} {_OR[lang]} {words[-1]}"


def offer(values, lang):
    """The offered times as one phrase, in clock order, each run of times in
    the same part of the day said with that part once (retest 3 on dev
    2026-11-20: "halv fem eller halv sex på kvällen" made 16:30 evening):
    ["16:30", "17:00", "17:30"] -> "halv fem eller fem på eftermiddagen,
    eller halv sex på kvällen". 12:00 is its own run ("tolv mitt på
    dagen"). None in a language without a spoken form or without times."""
    if lang not in LANGS:
        return None
    found = sorted({(int(t[:2]), int(t[3:])) for t in values or ()
                    if isinstance(t, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t)})
    if not found:
        return None
    runs = []                                   # [(part or "noon", [faces])]
    for hour, minute in found:
        key = "noon" if (hour, minute) == (12, 0) else part(hour * 60 + minute)
        if runs and runs[-1][0] == key != "noon":
            runs[-1][1].append(_face(hour, minute, lang))
        else:
            runs.append((key, [_face(hour, minute, lang)]))
    said = [_NOON[lang].split(" ", 1)[1] if key == "noon" else f"{_either(faces, lang)} {_PARTS[lang][key]}"
            for key, faces in runs]
    return said[0] if len(said) == 1 else f"{', '.join(said[:-1])}, {_OR[lang]} {said[-1]}"

"""What a spoken reply read back (live test 2026-11-20: the caller heard
"halv sju", 18:30, and the model booked another hold, 19:30).

The server compares the reply the caller heard with the one hold that can
be booked (tools.Executor.heard): every clock time and weekday the reply
names, in canonical form, must be the hold's. `covers()` answers True (the
reply names the hold's time and nothing that contradicts it), False (it
names another time or weekday) or None (it names no time).

Times are read from digits in any script and from words in the languages
below. Half hours count toward the next hour in Swedish, Norwegian, Danish,
German and Dutch ("halv sju", "halv syv", "halb sieben", "half zeven":
18:30); in English "half past six" and the British "half seven" (19:30).
A clock time without the part of the day matches both halves of the day
("halv sju" is 06:30 or 18:30): only the clock face is compared.

`mentions_terms()` tells whether a reply said the card guarantee's fee rule
(a fee word in the caller's language)."""

from __future__ import annotations

import re
from datetime import date

PARSED = ("sv", "en", "no", "da", "de", "nl", "zh", "yue")

_NUMBERS = {
    "sv": "noll ett två tre fyra fem sex sju åtta nio tio elva tolv tretton fjorton femton sexton sjutton arton "
          "nitton tjugo tjugoett tjugotvå tjugotre",
    "no": "null ett to tre fire fem seks sju åtte ni ti elleve tolv tretten fjorten femten seksten sytten atten "
          "nitten tjue tjueen tjueto tjuetre",
    "da": "nul et to tre fire fem seks syv otte ni ti elleve tolv tretten fjorten femten seksten sytten atten "
          "nitten tyve enogtyve toogtyve treogtyve",
    "de": "null eins zwei drei vier fünf sechs sieben acht neun zehn elf zwölf dreizehn vierzehn fünfzehn sechzehn "
          "siebzehn achtzehn neunzehn zwanzig einundzwanzig zweiundzwanzig dreiundzwanzig",
    "nl": "nul een twee drie vier vijf zes zeven acht negen tien elf twaalf dertien veertien vijftien zestien "
          "zeventien achttien negentien twintig eenentwintig tweeëntwintig drieëntwintig",
    "en": "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
          "seventeen eighteen nineteen twenty twenty-one twenty-two twenty-three",
}
NUMBERS = {lang: {w: i for i, w in enumerate(words.split())} for lang, words in _NUMBERS.items()}
NUMBERS["sv"]["en"] = NUMBERS["no"]["en"] = NUMBERS["da"]["en"] = 1
NUMBERS["no"]["syv"] = 7
NUMBERS["de"]["ein"] = 1
NUMBERS["nl"]["één"] = 1
# Minutes said after the hour ("arton trettio", "seven thirty", "achtzehn Uhr dreißig").
_MINUTES = {
    "sv": {"trettio": 30, "femton": 15, "fyrtiofem": 45},
    "no": {"tretti": 30, "trettio": 30, "femten": 15, "førtifem": 45},
    "da": {"tredive": 30, "femten": 15, "femogfyrre": 45},
    "de": {"dreißig": 30, "fünfzehn": 15, "fünfundvierzig": 45},
    "nl": {"dertig": 30, "vijftien": 15, "vijfenveertig": 45},
    "en": {"thirty": 30, "fifteen": 15, "forty-five": 45, "forty five": 45},
}
# (word, minutes, hour offset): "halv sju" = 6:30 is (30, -1); "half past six" (30, 0).
_BEFORE = {
    "sv": {"halv": (30, -1), "kvart över": (15, 0), "kvart i": (45, -1)},
    "no": {"halv": (30, -1), "kvart over": (15, 0), "kvart på": (45, -1)},
    "da": {"halv": (30, -1), "kvart over": (15, 0), "kvart i": (45, -1), "kvart på": (45, -1)},
    "de": {"halb": (30, -1), "viertel nach": (15, 0), "viertel vor": (45, -1), "dreiviertel": (45, -1)},
    "nl": {"half": (30, -1), "kwart over": (15, 0), "kwart voor": (45, -1)},
    "en": {"half past": (30, 0), "half": (30, 0), "quarter past": (15, 0), "quarter to": (45, -1)},
}
# A whole hour: a marker before ("klockan sju", "um sieben") or after ("seven o'clock", "sieben Uhr").
_AT = {"sv": ("klockan", "kl", "kl."), "no": ("klokka", "klokken", "kl", "kl."), "da": ("klokken", "kl", "kl."),
       "de": ("um",), "nl": ("om",), "en": ("at",)}
_OCLOCK = {"de": ("uhr",), "nl": ("uur",), "en": ("o'clock", "oclock"), "sv": (), "no": (), "da": ()}
_WEEKDAYS = {
    "sv": "måndag tisdag onsdag torsdag fredag lördag söndag",
    "no": "mandag tirsdag onsdag torsdag fredag lørdag søndag",
    "da": "mandag tirsdag onsdag torsdag fredag lørdag søndag",
    "de": "montag dienstag mittwoch donnerstag freitag samstag sonntag",
    "nl": "maandag dinsdag woensdag donderdag vrijdag zaterdag zondag",
    "en": "monday tuesday wednesday thursday friday saturday sunday",
}
WEEKDAYS = {lang: words.split() for lang, words in _WEEKDAYS.items()}
_CJK_WEEKDAY = re.compile(r"(?:星期|禮拜|礼拜|周|週)([一二三四五六日天])")
_CJK_DAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_HAN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
               "八": 8, "九": 9}
_HAN_NUMBER = r"[零〇一二两兩三四五六七八九十\d]+"
_CJK_CLOCK = re.compile(rf"({_HAN_NUMBER})\s*[点點時时](?:\s*(半|一刻|三刻|({_HAN_NUMBER})\s*分?))?")
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹０１２３４５６７８９", "0123456789" * 3)
_DIGIT_CLOCK = re.compile(r"(?<![\d:.])([01]?\d|2[0-3])\s?[:.]\s?([0-5]\d)(?![\d:])")
_DIGIT_MARKED = re.compile(r"(?<!\w)(?:klockan|klokka|klokken|kl\.?|um|om|at|à|alle|a las)\s+([01]?\d|2[0-3])"
                           r"(?![\d:.]\d)(?!\s*(?:personer|people|guests|pers|gäster|personen|timmar|hours))", re.I)
_DIGIT_SUFFIX = re.compile(r"(?<![\d:.])([01]?\d|2[0-3])\s*(am|pm|uhr|uur|h)\b", re.I)


def _han(value):
    """A number in Han numerals or digits (1 to 59), else None."""
    value = str(value)
    if value.isdigit():
        return int(value)
    if value == "十":
        return 10
    if "十" in value:
        tens, _, ones = value.partition("十")
        return (_HAN_DIGITS.get(tens, 1) if tens else 1) * 10 + (_HAN_DIGITS.get(ones, 0) if ones else 0)
    if len(value) == 1 and value in _HAN_DIGITS:
        return _HAN_DIGITS[value]
    return None


def _alternation(words):
    return "|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True))


def _word_patterns(lang):
    """(pattern, reader) pairs for one language, longest forms first."""
    nums = NUMBERS[lang]
    num = rf"(?P<h>{_alternation(nums)}|[01]?\d|2[0-3])"
    out = []
    before = _BEFORE.get(lang) or {}
    if before:
        out.append((re.compile(rf"(?<!\w)(?P<w>{_alternation(before)})\s+{num}(?!\w)"),
                    lambda m: _shift(m.group("h"), *before[m.group("w")], nums)))
    minutes = _MINUTES.get(lang) or {}
    if minutes:
        joiner = r"\s+(?:och\s+|og\s+|und\s+|en\s+|uhr\s+|uur\s+)?"
        out.append((re.compile(rf"(?<!\w){num}{joiner}(?P<m>{_alternation(minutes)})(?!\w)"),
                    lambda m: (_hour(m.group("h"), nums), minutes[m.group("m")])))
    oclock = _OCLOCK.get(lang) or ()
    if oclock:
        out.append((re.compile(rf"(?<!\w){num}\s*(?:{_alternation(oclock)})(?!\w)"),
                    lambda m: (_hour(m.group("h"), nums), 0)))
    marks = _AT.get(lang) or ()
    if marks:
        out.append((re.compile(rf"(?<!\w)(?:{_alternation(marks)})\s+{num}(?!\w)"),
                    lambda m: (_hour(m.group("h"), nums), 0)))
    return out


def _hour(word, nums):
    return int(word) if str(word).isdigit() else nums[word]


def _shift(word, minute, offset, nums):
    hour = (_hour(word, nums) + offset) % 24
    return hour, minute


_PATTERNS = {lang: _word_patterns(lang) for lang in NUMBERS}


def clock_times(text, lang=None):
    """Every clock time the text names, as (hour, minute); words are read in
    `lang` (and English), digits in any language. A span read once is not
    read again by a shorter form ("seven thirty" is not also "at seven")."""
    low = " ".join(str(text or "").translate(_DIGITS).lower().split())
    taken, found = [], []

    def free(span):
        return all(span[1] <= a or span[0] >= b for a, b in taken)

    def add(span, value):
        if value is not None and free(span):
            taken.append(span)
            found.append(value)

    for m in _CJK_CLOCK.finditer(low):
        hour = _han(m.group(1))
        tail = m.group(2)
        minute = 30 if tail == "半" else 15 if tail == "一刻" else 45 if tail == "三刻" else (
            _han(m.group(3)) if m.group(3) else 0)
        if hour is not None and minute is not None and hour < 24 and minute < 60:
            add(m.span(), (hour, minute))
    for m in _DIGIT_CLOCK.finditer(low):
        add(m.span(), (int(m.group(1)), int(m.group(2))))
    langs = [x for x in (lang, "en") if x in _PATTERNS]
    for code in dict.fromkeys(langs):
        for pattern, read in _PATTERNS[code]:
            for m in pattern.finditer(low):
                try:
                    add(m.span(), read(m))
                except (KeyError, ValueError):
                    pass
    for m in _DIGIT_SUFFIX.finditer(low):
        hour = int(m.group(1)) + (12 if m.group(2).lower() == "pm" and int(m.group(1)) < 12 else 0)
        add(m.span(), (hour % 24, 0))
    for m in _DIGIT_MARKED.finditer(low):
        add(m.span(), (int(m.group(1)), 0))
    return found


def weekdays(text, lang=None):
    """The weekdays (0 Monday) the text names, in `lang` and English."""
    low = str(text or "").lower()
    out = {_CJK_DAY_INDEX[m.group(1)] for m in _CJK_WEEKDAY.finditer(low)}
    tokens = re.findall(r"[^\W\d_]+", low)
    for code in dict.fromkeys(x for x in (lang, "en") if x in WEEKDAYS):
        for i, name in enumerate(WEEKDAYS[code]):
            if any(t.startswith(name) for t in tokens):
                out.add(i)
    return out


def same_clock(said, hold_time):
    """A said (hour, minute) is the hold's "HH:MM" on the clock face."""
    hour, minute = (int(x) for x in str(hold_time).split(":"))
    return said[1] == minute and said[0] % 12 == hour % 12


def covers(text, day, time, lang=None):
    """True: the reply names the hold's time and no other time or weekday;
    False: it names another time or another weekday; None: it names no time.
    In a language whose words are not read here (PARSED), a reply that
    contradicts nothing and asks a question counts as the read-back."""
    times = clock_times(text, lang)
    days = weekdays(text, lang)
    weekday = date.fromisoformat(str(day)[:10]).weekday()
    if any(not same_clock(t, time) for t in times) or (days and weekday not in days):
        return False
    if times:
        return True
    if lang not in PARSED and re.search(r"[?？؟]", str(text or "")):
        return True
    return None


# A fee in the caller's language (the card guarantee's rule): any of these words.
FEE_WORDS = (
    "avgift", "fee", "gebühr", "gebuhr", "frais", "cargo", "tarifa", "penaliz", "penale", "addebit",
    "kosten", "vergoeding", "boete", "gebyr", "maksu", "opłat", "oplat", "taxa", "multa", "ücret", "ucret",
    "رسوم", "رسم", "غرامة", "هزینه", "جریمه", "плат", "штраф", "сбор", "збір", "lacag", "ganaax", "naknad",
    "kazn", "biaya", "denda", "费", "費", "料金", "キャンセル料", "수수료", "요금", "χρέωσ", "poplat",
    "díj", "dij", "taxă", "taxa", "такса", "קנס", "עמלה", "शुल्क", "ค่าธรรมเนียม", "phí", "bayad",
)
# A card guarantee without fees (the terms could not be read): the card word.
CARD_WORDS = ("kort", "card", "karte", "carte", "tarjeta", "carta", "kaart", "kortti", "kart", "karta", "cartão",
              "بطاقة", "کارت", "карт", "kaar", "kartic", "卡", "カード", "카드", "κάρτα", "garanti", "guarantee",
              "garantie", "garantía", "garanzia", "takuu", "gwarancj", "ضمان", "ضمانت", "гарант", "dammaanad",
              "jamstv", "担保", "擔保")


def mentions_terms(text, fees=True):
    """The reply said the guarantee's fee rule (`fees`), else that a card is needed."""
    low = str(text or "").lower()
    return any(w in low for w in (FEE_WORDS if fees else FEE_WORDS + CARD_WORDS))

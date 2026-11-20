"""What a spoken reply read back (live test 2026-11-20: the caller heard
"halv sju", 18:30, and the model booked another hold, 19:30).

The server compares the reply the caller heard with the one hold that can
be booked (tools.Executor.heard): every clock time and weekday the reply
names, in canonical form, must be the hold's, and so must every day of
the month, month, "today" or "tomorrow" and number of guests it names.
`covers()` answers True (the reply names the hold's time and nothing that
contradicts it), False (it names another time, day or party) or None (it
names no time).

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
from datetime import date, timedelta

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
# "18h30" (French, Portuguese and others), "6시 30분" / "6시 반" (Korean).
_DIGIT_H = re.compile(r"(?<![\d:.])([01]?\d|2[0-3])\s?h\s?([0-5]\d)(?!\d)", re.I)
_KO_CLOCK = re.compile(r"(?<!\d)([01]?\d|2[0-3])\s*시(?:\s*(반|([0-5]?\d)\s*분))?")
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
    for m in _DIGIT_H.finditer(low):
        add(m.span(), (int(m.group(1)), int(m.group(2))))
    for m in _KO_CLOCK.finditer(low):
        add(m.span(), (int(m.group(1)), 30 if m.group(2) == "반" else int(m.group(3) or 0)))
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


def covers(text, day, time, lang=None, *, party=None, today=None):
    """True: the reply names the hold's time and no other time, weekday,
    date or party size; False: it names another time, weekday, day of the
    month, month, day relative to `today` ("i morgon") or number of guests;
    None: it names no time. In a language whose words are not read here
    (PARSED), a reply that contradicts nothing and asks a question counts as
    the read-back."""
    times = clock_times(text, lang)
    days = weekdays(text, lang)
    when = date.fromisoformat(str(day)[:10])
    if any(not same_clock(t, time) for t in times) or (days and when.weekday() not in days):
        return False
    if any(d != when for d in dates(text, lang, when, today)):
        return False
    if party is not None and any(n != int(party) for n in party_sizes(text, lang)):
        return False
    if times:
        return True
    if lang not in PARSED and re.search(r"[?？؟]", str(text or "")):
        return True
    return None


# --- days of the month, months, days relative to today, party sizes --------------------------------

_MONTHS = {
    "sv": "januari februari mars april maj juni juli augusti september oktober november december",
    "no": "januar februar mars april mai juni juli august september oktober november desember",
    "da": "januar februar marts april maj juni juli august september oktober november december",
    "de": "januar februar märz april mai juni juli august september oktober november dezember",
    "nl": "januari februari maart april mei juni juli augustus september oktober november december",
    "en": "january february march april may june july august september october november december",
}
MONTHS = {lang: words.split() for lang, words in _MONTHS.items()}
_ORDINALS = {
    "sv": "första andra tredje fjärde femte sjätte sjunde åttonde nionde tionde elfte tolfte trettonde fjortonde "
          "femtonde sextonde sjuttonde artonde nittonde tjugonde tjugoförsta tjugoandra tjugotredje tjugofjärde "
          "tjugofemte tjugosjätte tjugosjunde tjugoåttonde tjugonionde trettionde trettioförsta",
    "no": "første andre tredje fjerde femte sjette sjuende åttende niende tiende ellevte tolvte trettende "
          "fjortende femtende sekstende syttende attende nittende tjuende tjueførste tjueandre tjuetredje "
          "tjuefjerde tjuefemte tjuesjette tjuesjuende tjueåttende tjueniende trettiende trettiførste",
    "da": "første anden tredje fjerde femte sjette syvende ottende niende tiende ellevte tolvte trettende "
          "fjortende femtende sekstende syttende attende nittende tyvende enogtyvende toogtyvende treogtyvende "
          "fireogtyvende femogtyvende seksogtyvende syvogtyvende otteogtyvende niogtyvende tredivte enogtredivte",
    "de": "erste zweite dritte vierte fünfte sechste siebte achte neunte zehnte elfte zwölfte dreizehnte "
          "vierzehnte fünfzehnte sechzehnte siebzehnte achtzehnte neunzehnte zwanzigste einundzwanzigste "
          "zweiundzwanzigste dreiundzwanzigste vierundzwanzigste fünfundzwanzigste sechsundzwanzigste "
          "siebenundzwanzigste achtundzwanzigste neunundzwanzigste dreißigste einunddreißigste",
    "nl": "eerste tweede derde vierde vijfde zesde zevende achtste negende tiende elfde twaalfde dertiende "
          "veertiende vijftiende zestiende zeventiende achttiende negentiende twintigste eenentwintigste "
          "tweeëntwintigste drieëntwintigste vierentwintigste vijfentwintigste zesentwintigste "
          "zevenentwintigste achtentwintigste negenentwintigste dertigste eenendertigste",
    "en": "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth "
          "fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth twenty-first "
          "twenty-second twenty-third twenty-fourth twenty-fifth twenty-sixth twenty-seventh twenty-eighth "
          "twenty-ninth thirtieth thirty-first",
}
ORDINALS = {lang: {w: i + 1 for i, w in enumerate(words.split())} for lang, words in _ORDINALS.items()}
ORDINALS["no"]["syvende"] = 7
ORDINALS["no"]["tjuesyvende"] = 27
for _w, _n in list(ORDINALS["en"].items()):
    if "-" in _w:
        ORDINALS["en"][_w.replace("-", " ")] = _n
for _w, _n in list(ORDINALS["de"].items()):      # "am siebten", "der siebte", "siebter"
    for _end in ("n", "r", "s", "m"):
        ORDINALS["de"][_w + _end] = _n
# Days relative to today (offset), longest forms first when read.
_RELATIVE = {
    "sv": {"i dag": 0, "idag": 0, "i kväll": 0, "ikväll": 0, "i morgon": 1, "imorgon": 1, "i övermorgon": 2,
           "övermorgon": 2},
    "no": {"i dag": 0, "i kveld": 0, "i morgen": 1, "i overmorgen": 2, "overmorgen": 2},
    "da": {"i dag": 0, "i aften": 0, "i morgen": 1, "i overmorgen": 2, "overmorgen": 2},
    "de": {"heute": 0, "morgen": 1, "übermorgen": 2},
    "nl": {"vandaag": 0, "vanavond": 0, "morgen": 1, "overmorgen": 2},
    "en": {"today": 0, "tonight": 0, "tomorrow": 1, "day after tomorrow": 2},
    "zh": {"今天": 0, "今晚": 0, "明天": 1, "后天": 2, "後天": 2, "今日": 0, "聽日": 1, "听日": 1},
}
_NOT_ALONE = ("may", "march")       # English words that are a month only beside a day
_CJK_DATE = re.compile(rf"(?:({_HAN_NUMBER})\s*月\s*)?({_HAN_NUMBER})\s*[日号號](?![本])")
_PERSONS = {
    "sv": ("personer", "person", "pers", "gäster", "gäst"), "no": ("personer", "person", "gjester", "gjest"),
    "da": ("personer", "person", "gæster", "gæst"), "de": ("personen", "person", "gäste", "gast"),
    "nl": ("personen", "persoon", "gasten", "gast", "mensen"),
    "en": ("people", "persons", "person", "guests", "guest"),
}
_TABLE_FOR = {"sv": r"bord\s+för", "no": r"bord\s+(?:for|til)", "da": r"bord\s+(?:til|for)",
              "de": r"tisch\s+für", "nl": r"tafel\s+voor", "en": r"table\s+for"}
_CJK_PARTY = re.compile(rf"({_HAN_NUMBER})\s*(?:位|个人|個人|人|名)")


def _month_day(text, lang):
    """(month or None, day of month) pairs the text names in `lang` and English."""
    low = " ".join(str(text or "").translate(_DIGITS).lower().replace(",", " ").split())
    out = []
    for code in dict.fromkeys(x for x in (lang, "en") if x in MONTHS):
        months = _alternation(MONTHS[code])
        ords = _alternation(ORDINALS[code])
        day = rf"(?P<d>{ords}|[0-3]?\d)(?::?[ae]|st|nd|rd|th|\.)?"
        for m in re.finditer(rf"(?<!\w){day}\s+(?:of\s+)?(?P<m>{months})(?!\w)", low):
            out.append((MONTHS[code].index(m.group("m")) + 1, _day(m.group("d"), code)))
        for m in re.finditer(rf"(?<!\w)(?P<m>{months})\s+(?:the\s+)?{day}(?!\w)", low):
            out.append((MONTHS[code].index(m.group("m")) + 1, _day(m.group("d"), code)))
        for m in re.finditer(rf"(?<!\w)(?P<m>{months})(?!\w)", low):
            if m.group("m") not in _NOT_ALONE:      # "May I have your name?"
                out.append((MONTHS[code].index(m.group("m")) + 1, None))
    for m in _CJK_DATE.finditer(low):
        out.append((_han(m.group(1)) if m.group(1) else None, _han(m.group(2))))
    return out


def _day(word, code):
    return int(word) if word.isdigit() else ORDINALS[code][word]


def dates(text, lang=None, near=None, today=None):
    """The dates the text names that can be told apart: a day of the month
    with or without the month (the year and month of `near`, the hold's
    date, when left out), a month alone (any day of it is the hold's when
    it is the hold's month) and "today", "tomorrow" (with `today`)."""
    out = []
    near = near or today
    for month, day in _month_day(text, lang) if near else ():
        try:
            if day is None:
                if month != near.month:
                    out.append(date(near.year, month, 1))
            else:
                out.append(date(near.year, month or near.month, day))
        except ValueError:
            out.append(date.min)
    if today is not None:
        low = " " + " ".join(str(text or "").lower().split()) + " "
        for code in dict.fromkeys(x for x in (lang, "en", "zh") if x in _RELATIVE):
            words = _RELATIVE[code]
            hits, spare = [], low
            for word in sorted(words, key=len, reverse=True):
                unspaced = not re.search(r"[a-zåäöæøüß]", word)
                pattern = re.escape(word) if unspaced else rf"(?<![\w']){re.escape(word)}(?!\w)"
                if code in ("de", "nl") and word == "morgen":
                    pattern = r"(?<![\w'])(?<!am )(?<!den )(?<!'s )morgen(?!\w)"
                if re.search(pattern, spare):
                    hits.append(words[word])
                    spare = re.sub(pattern, " ", spare)
            out.extend(today + timedelta(days=n) for n in hits)
    return out


def party_sizes(text, lang=None):
    """The numbers of guests the text names ("fyra personer", "table for four", "四位")."""
    low = " ".join(str(text or "").translate(_DIGITS).lower().split())
    out = []
    for code in dict.fromkeys(x for x in (lang, "en") if x in _PERSONS):
        nums = NUMBERS[code]
        num = rf"(?P<n>{_alternation(nums)}|\d{{1,3}})"
        for m in re.finditer(rf"(?<!\w){num}\s+(?:{_alternation(_PERSONS[code])})(?!\w)", low):
            out.append(_hour(m.group("n"), nums))
        for m in re.finditer(rf"(?<!\w){_TABLE_FOR[code]}\s+{num}(?!\w)", low):
            out.append(_hour(m.group("n"), nums))
    for m in _CJK_PARTY.finditer(low):
        n = _han(m.group(1))
        if n:
            out.append(n)
    return out


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


def _term(word):
    """A word in a spaced Latin, Greek or Cyrillic script starts a word ("fee", never "coffee");
    Chinese, Japanese, Korean, Thai and Arabic-script words are found anywhere. A word of three
    letters is whole, or plural ("fees", never "feedback")."""
    if len(word) <= 3 and word.isascii():
        return re.compile(rf"(?<![^\W\d_]){re.escape(word)}(?:s|es)?(?![^\W\d_])")
    if re.match(r"[a-zà-öø-ÿāăąćčďęěğıłńňőřśşšťůűźżžα-ωа-яёіїє]", word):
        return re.compile(rf"(?<![^\W\d_]){re.escape(word)}")
    return re.compile(re.escape(word))


_FEES = [_term(w) for w in FEE_WORDS]
_CARDS = [_term(w) for w in FEE_WORDS + CARD_WORDS]


def mentions_terms(text, fees=True):
    """The reply said the guarantee's fee rule (`fees`), else that a card is needed."""
    low = str(text or "").lower()
    return any(p.search(low) for p in (_FEES if fees else _CARDS))

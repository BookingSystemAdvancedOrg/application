"""Restaurant words inside whole sentences (site languages retest, findings 1
and 2): the Booqy custom terminology goes to Amazon Translate with every
call, and the reviewed glossary answers the short kit labels.

The real terminology is infrastructure site-translate/terminology.csv
(imported by CI). Here a fake Translate stands in: without the terminology it
makes the mistakes the live test found (bord as a spreadsheet table, platser
as web sites, tim as "hr"), with it it uses the terms below, which are rows of
that CSV."""

import csv
import io
import re

import pytest

from shared import site_translate as st
from test_site_translate import app, call  # noqa: F401  (fixture)

LANGS = ("en", "es", "ar", "fa")
FSI, PDI = "⁨", "⁩"
TERMINOLOGY = "test-site-translate-terms"

# Rows of infrastructure site-translate/terminology.csv.
TERMS_CSV = """sv,en,es,ar,fa
bord,table,mesa,طاولة,میز
bordet,table,mesa,الطاولة,میز
borden,tables,mesas,الطاولات,میزها
bordsbokning,table booking,reserva de mesa,حجز طاولة,رزرو میز
boka bord,book a table,reservar mesa,احجز طاولة,رزرو میز
platser,seats,plazas,مقاعد,صندلی
på plats,at the restaurant,en el local,في المطعم,در رستوران
sällskap,party,grupo,مجموعة,گروه
sällskapet,party,grupo,المجموعة,گروه
gäster,guests,comensales,ضيوف,مهمانان
avbokning,cancellation,cancelación,إلغاء الحجز,لغو رزرو
kortgaranti,card guarantee,garantía con tarjeta,ضمان بالبطاقة,ضمانت کارت
förrätt,starter,entrante,مقبلات,پیش‌غذا
varmrätt,main course,plato principal,طبق رئيسي,غذای اصلی
efterrätt,dessert,postre,حلوى,دسر
dryck,drink,bebida,مشروب,نوشیدنی
avhämtning,pickup,para recoger,استلام من المطعم,تحویل حضوری
hemleverans,home delivery,entrega a domicilio,توصيل إلى المنزل,ارسال به منزل
öppettider,opening hours,horario,ساعات العمل,ساعات کاری
tim,h,h,ساعة,ساعت
org.nr,reg. no.,n.º de registro,رقم التسجيل,شماره ثبت
"""
_rows = list(csv.reader(io.StringIO(TERMS_CSV)))
TERMS = {row[0]: dict(zip(_rows[0][1:], row[1:])) for row in _rows[1:]}

# What the live test saw without a terminology.
WRONG = {
    "bord": {"en": "spreadsheet", "es": "tabla", "ar": "جدول", "fa": "جدول"},
    "bordet": {"en": "the spreadsheet", "es": "la tabla", "ar": "الجدول", "fa": "جدول"},
    "borden": {"en": "spreadsheets", "es": "tablas", "ar": "الجداول", "fa": "جداول"},
    "platser": {"en": "sites", "es": "sitios", "ar": "مواقع", "fa": "سایت ها"},
    "tim": {"en": "hr", "es": "hr", "ar": "hr", "fa": "hr"},
}
BAD_WORDS = {lang: {w[lang] for w in WRONG.values()} for lang in LANGS}


class TermTranslate:
    """Word-by-word stand-in for Amazon Translate that honours a terminology
    (longest term first, case-insensitive, like the real one) and keeps the
    translate="no" spans as they are."""

    def __init__(self):
        self.calls = []

    def translate_text(self, **kwargs):
        self.calls.append(kwargs)
        lang = kwargs["TargetLanguageCode"]
        terms = TERMS if TERMINOLOGY in kwargs.get("TerminologyNames", []) else {}
        out, pos = [], 0
        for m in st._SPAN.finditer(kwargs["Text"]):
            out.append(self._words(kwargs["Text"][pos:m.start()], lang, terms))
            out.append(m.group(0))
            pos = m.end()
        out.append(self._words(kwargs["Text"][pos:], lang, terms))
        return {"TranslatedText": "".join(out)}

    @staticmethod
    def _words(text, lang, terms):
        sources = sorted(terms, key=len, reverse=True)
        pattern = "|".join(re.escape(t) for t in sources) or r"(?!x)x"
        text = re.sub(r"(?<!\w)(" + pattern + r")(?!\w)",
                      lambda m: "\x00" + terms[m.group(1).lower()][lang] + "\x01", text, flags=re.IGNORECASE)

        def wrong(m):
            word = m.group(0)
            return WRONG[word.lower()][lang] if word.lower() in WRONG else word
        parts = re.split(r"(\x00[^\x01]*\x01)", text)
        return "".join(p[1:-1] if p.startswith("\x00") else re.sub(r"\w+", wrong, p) for p in parts)


@pytest.fixture
def terms(app, monkeypatch):  # noqa: F811
    fake = TermTranslate()
    monkeypatch.setitem(st._clients, "translate", fake)
    monkeypatch.setattr(st, "GLOSSARY", st.load_glossary())
    monkeypatch.setenv("SITE_TRANSLATE_TERMINOLOGY", TERMINOLOGY)
    monkeypatch.setenv("SITE_TRANSLATE_TERMINOLOGY_HASH", "abc123")
    app["fake"] = fake
    return app


def translate(app, lang, texts):
    status, body, _ = call(app, {"lang": lang, "texts": texts})
    assert status == 200 and body["partial"] is False, body
    return body["texts"]


# --- the Lambda passes the terminology -------------------------------------------------------

def test_every_translate_call_names_the_terminology(terms):
    translate(terms, "es", ["Välj ett bord för hela sällskapet i kväll"])
    assert [c["TerminologyNames"] for c in terms["fake"].calls] == [[TERMINOLOGY]]
    assert terms["fake"].calls[0]["Settings"] == {"Formality": "INFORMAL"}


def test_translate_one_passes_terminology_names(monkeypatch):
    fake = TermTranslate()
    monkeypatch.setitem(st._clients, "translate", fake)
    monkeypatch.setenv("SITE_TRANSLATE_TERMINOLOGY", "site-translate-terms")
    st.translate_one("Boka bord", "en")
    assert fake.calls[-1] == {"Text": "Boka bord", "SourceLanguageCode": "sv", "TargetLanguageCode": "en",
                              "TerminologyNames": ["site-translate-terms"]}


def test_without_a_terminology_configured_none_is_sent(monkeypatch):
    fake = TermTranslate()
    monkeypatch.setitem(st._clients, "translate", fake)
    monkeypatch.delenv("SITE_TRANSLATE_TERMINOLOGY", raising=False)
    st.translate_one("Boka bord", "en")
    assert "TerminologyNames" not in fake.calls[-1]


def test_cache_key_is_v3_and_changes_with_the_terminology(monkeypatch):
    assert st.VERSION == "v4"
    monkeypatch.setenv("SITE_TRANSLATE_TERMINOLOGY", TERMINOLOGY)
    monkeypatch.setenv("SITE_TRANSLATE_TERMINOLOGY_HASH", "one")
    first = st.cache_key("en", "Boka bord")
    monkeypatch.setenv("SITE_TRANSLATE_TERMINOLOGY_HASH", "two")
    assert st.cache_key("en", "Boka bord") != first
    monkeypatch.delenv("SITE_TRANSLATE_TERMINOLOGY")
    assert st.cache_key("en", "Boka bord") not in (first, st.cache_key("es", "Boka bord"))


def test_an_old_wrong_cache_entry_is_not_served(terms):
    # What v2 stored for this text (no terminology) must not come back.
    marked, _ = st.protect("Välj ett bord för hela sällskapet i kväll")
    old = __import__("hashlib").sha256(f"v2|en||{marked}".encode()).hexdigest()
    terms["cache"].put_item(Item={"PK": f"T#en#{old}", "text": "Choose a spreadsheet", "ttl": 9999999999})
    assert "table" in translate(terms, "en", ["Välj ett bord för hela sällskapet i kväll"])[0]


# --- whole sentences, every language ---------------------------------------------------------

SENTENCES = [
    # (Swedish, the terms that must be in it)
    ("Välj ett bord för hela sällskapet i kväll", ("bord", "sällskapet")),
    ("Bord 9 + 10 · 6 platser", ("platser",)),
    ("fredag 20 november kl 18:00-20:00 · 4 personer · bord 1", ("bord",)),
    ("Borden släpps om du inte bekräftar", ("borden",)),
    ("Bordet är bokat för fyra gäster", ("bordet", "gäster")),
    ("2 tim (Middag), kan förlängas på plats om bordet är ledigt", ("tim", "på plats", "bordet")),
    ("Avbokning är gratis, men vi tar kortgaranti för stora sällskap", ("avbokning", "kortgaranti", "sällskap")),
    ("Förrätt, varmrätt och efterrätt med en dryck", ("förrätt", "varmrätt", "efterrätt", "dryck")),
    ("Avhämtning och hemleverans varje dag, se öppettider", ("avhämtning", "hemleverans", "öppettider")),
    ("Bordsbokning: boka bord direkt här", ("bordsbokning", "boka bord")),
]


@pytest.mark.parametrize("lang", LANGS)
def test_restaurant_words_in_sentences_use_the_terminology(terms, lang):
    out = translate(terms, lang, [sv for sv, _ in SENTENCES])
    for (sv, words), text in zip(SENTENCES, out):
        for word in words:
            want = TERMS[word][lang]
            assert want.lower() in text.lower(), (lang, sv, word, text)
        for bad in BAD_WORDS[lang]:
            assert not re.search(r"(?<!\w)" + re.escape(bad) + r"(?!\w)", text), (lang, sv, bad, text)


def test_without_the_terminology_the_fake_makes_the_live_mistakes(terms, monkeypatch):
    # Guards the test above: the fake really is wrong without a terminology.
    monkeypatch.delenv("SITE_TRANSLATE_TERMINOLOGY")
    out = translate(terms, "es", ["Bord 9 + 10 · 6 platser"])
    assert "tabla" in out[0].lower() or "sitios" in out[0]


# --- the short kit labels (glossary), every language -----------------------------------------

LABELS = {
    "Bord 9 · 2 platser": {
        "en": "Table 9 · 2 seats", "es": "Mesa 9 · 2 plazas",
        "ar": f"الطاولة {FSI}9{PDI} · {FSI}2{PDI} مقاعد", "fa": f"میز {FSI}9{PDI} · {FSI}2{PDI} صندلی"},
    "4 pl": {"en": "4 seats", "es": "4 plazas", "ar": f"{FSI}4{PDI} مقاعد", "fa": f"{FSI}4{PDI} صندلی"},
    "6 platser": {"en": "6 seats", "es": "6 plazas", "ar": f"{FSI}6{PDI} مقاعد", "fa": f"{FSI}6{PDI} صندلی"},
    "Bord 3, 4 platser": {"en": "Table 3, 4 seats", "es": "Mesa 3, 4 plazas",
                          "ar": f"الطاولة {FSI}3{PDI}، {FSI}4{PDI} مقاعد", "fa": f"میز {FSI}3{PDI}، {FSI}4{PDI} صندلی"},
    "Mer än 12 personer?": {"en": "More than 12 people?", "es": "¿Más de 12 personas?",
                            "ar": f"أكثر من {FSI}12{PDI} أشخاص؟", "fa": f"بیش از {FSI}12{PDI} نفر؟"},
    "Fler än 12 personer? Ring": {"en": "More than 12 people? Call", "es": "¿Más de 12 personas? Llama al",
                                  "ar": f"أكثر من {FSI}12{PDI} أشخاص؟ اتصل على",
                                  "fa": f"بیش از {FSI}12{PDI} نفر؟ تماس:"},
    "· org.nr": {"en": "· Reg. no.", "es": "· N.º de registro", "ar": "· رقم التسجيل", "fa": "· شماره ثبت"},
    "(org.nr 556677-8899)": {"en": "(reg. no. 556677-8899)", "es": "(n.º de registro 556677-8899)",
                             "ar": f"(رقم التسجيل {FSI}556677-8899{PDI})", "fa": f"(شماره ثبت {FSI}556677-8899{PDI})"},
    "2 tim": {"en": "2 h", "es": "2 h", "ar": f"{FSI}2{PDI} ساعة", "fa": f"{FSI}2{PDI} ساعت"},
    "Bokningstid: 2 tim, kan inte förlängas.": {
        "en": "Booking length: 2 h, cannot be extended.", "es": "Duración de la reserva: 2 h, no se puede ampliar.",
        "ar": f"مدة الحجز: {FSI}2{PDI} ساعة، لا يمكن تمديدها.", "fa": f"مدت رزرو: {FSI}2{PDI} ساعت، قابل تمدید نیست."},
    "Bokningstid: 1 tim 30 min, kan förlängas på plats om bordet är ledigt.": {
        "en": "Booking length: 1 h 30 min, can be extended at the restaurant if the table is free.",
        "es": "Duración de la reserva: 1 h 30 min, se puede ampliar en el local si la mesa está libre.",
        "ar": f"مدة الحجز: {FSI}1{PDI} ساعة و{FSI}30{PDI} دقيقة، يمكن تمديدها في المطعم إذا كانت الطاولة متاحة.",
        "fa": f"مدت رزرو: {FSI}1{PDI} ساعت و {FSI}30{PDI} دقیقه، اگر میز آزاد باشد در رستوران قابل تمدید است."},
    "Bord 2 hålls åt dig:": {"en": "Table 2 is held for you:", "es": "Te guardamos la mesa 2:",
                             "ar": f"الطاولة {FSI}2{PDI} محجوزة لك:", "fa": f"میز {FSI}2{PDI} برای شما نگه داشته می‌شود:"},
}


@pytest.mark.parametrize("lang", LANGS)
def test_short_kit_labels_come_from_the_glossary(terms, lang):
    sv = list(LABELS)
    assert translate(terms, lang, sv) == [LABELS[s][lang] for s in sv]
    assert terms["fake"].calls == []  # free: no Translate call for any of them

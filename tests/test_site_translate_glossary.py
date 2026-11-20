"""The reviewed glossary of site-kit UI strings (shared/site_translate_glossary.json)
and how site_translate.glossary() applies it: the live test cases where Amazon
Translate had no context (SPEC site languages, live test findings 1 and 3)."""

import json
import re

import pytest

from shared import site_translate as st

LANGS = ("en", "es", "ar", "fa")
FSI, PDI = "⁨", "⁩"


def gloss(lang, text, keep=()):
    marked, tokens = st.protect(text, keep)
    return st.glossary(lang, text, marked, tokens)


# --- the file itself ------------------------------------------------------------------------

def test_file_shape_every_language_and_no_duplicates():
    data = json.loads(st.GLOSSARY_FILE.read_text(encoding="utf-8"))
    assert data["columns"] == ["sv", *LANGS]
    seen = set()
    for row in data["entries"]:
        assert len(row) == 5 and all(isinstance(x, str) and x.strip() == x and x for x in row), row
        assert row[0] not in seen, row[0]
        seen.add(row[0])
    assert len(st.GLOSSARY) == len(data["entries"]) > 400


def test_placeholders_match_in_every_language():
    for sv, values in st.GLOSSARY.items():
        wanted = sorted(re.findall(r"\{\d+\}", sv))
        assert wanted == [f"{{{i}}}" for i in range(len(wanted))], sv
        for lang in LANGS:
            assert sorted(re.findall(r"\{\d+\}", values[lang])) == wanted, (sv, lang)
            assert values[lang].count("{") == values[lang].count("}") == len(wanted), (sv, lang)


def test_house_style_no_long_dashes():
    raw = st.GLOSSARY_FILE.read_text(encoding="utf-8")
    for bad in (chr(0x2013), chr(0x2014), "&ndash;", "&mdash;"):
        assert bad not in raw


def test_template_keys_match_what_protect_makes():
    # A key with {0} only works when protect() turns the live text into that
    # template; e.g. "135 kr/port" -> "{0}/port".
    for sv in st.GLOSSARY:
        if "{" not in sv:
            continue
        sample = sv
        for i in range(10):
            sample = sample.replace(f"{{{i}}}", str(12 + i))
        marked, _ = st.protect(sample)
        assert st.template(marked) == sv, sv


# --- the live test cases --------------------------------------------------------------------

def test_table_means_a_restaurant_table_not_a_spreadsheet():
    assert gloss("en", "Bekräfta bord") == "Confirm your table"
    assert gloss("es", "Bekräfta bord") == "Confirmar mesa"
    assert "جدول" not in gloss("ar", "Bekräfta bord") and "جدول" not in gloss("fa", "Bekräfta bord")
    assert gloss("en", "Bord") == "Table" and gloss("es", "Bord") == "Mesa"


def test_held_tables_are_whole_sentences_singular_and_plural():
    assert gloss("en", "Bord 2 hålls åt dig i 14:59") == "Table 2 is held for you for 14:59"
    assert gloss("en", "Borden 4 hålls åt dig i 9:30") == "Tables 4 are held for you for 9:30"
    assert gloss("es", "Bord 2 hålls åt dig i 14:59") == "Te guardamos la mesa 2 durante 14:59"


def test_done_step_is_done_not_clear():
    assert gloss("en", "Klart") == "Done" and gloss("en", "Klart:") == "Done:"
    assert gloss("es", "Klart") == "Listo"
    assert gloss("ar", "Klart") == "تم" and "واضح" not in gloss("ar", "Klart")


def test_party_is_the_number_of_guests():
    assert gloss("en", "Dag & sällskap") == "Day & party size"
    assert "عدد الأشخاص" in gloss("ar", "Dag & sällskap")
    assert "نفرات" in gloss("fa", "Dag & sällskap")


def test_order_home_is_delivery():
    assert gloss("en", "Beställ hem") == "Order delivery"
    assert gloss("es", "Beställ hem") == "Pedir a domicilio"
    assert gloss("en", "Beställ avhämtning") == "Order for pickup"


def test_booking_length_hours():
    assert gloss("en", "2 tim, kan inte förlängas") == "2 h, cannot be extended"
    assert gloss("en", "Bokningstid:") == "Booking length:"
    assert gloss("es", "1 tim 30 min, kan inte förlängas") == "1 h 30 min, no se puede ampliar"


def test_kl_is_translated_not_left_as_kl():
    assert gloss("en", "kl") == "at" and gloss("es", "kl") == "a las"
    assert gloss("en", "kl 18:00") == "at 18:00"
    assert gloss("ar", "kl 18:00") == "الساعة " + FSI + "18:00" + PDI


def test_price_per_portion():
    assert gloss("en", "/port") == "per portion"
    assert gloss("en", "135 kr/port") == "135 kr per portion"
    assert gloss("fa", "135 kr/port") == FSI + "135 kr" + PDI + " برای هر پرس"


def test_live_timer_label_with_the_number_protected():
    assert gloss("en", "Bordet hålls i 14:59") == "Table held for 14:59"
    assert gloss("ar", "Bordet hålls i 14:59") == "الطاولة محجوزة لمدة " + FSI + "14:59" + PDI
    assert gloss("fa", "Bordet hålls i 03:07").startswith("میز به مدت " + FSI + "03:07" + PDI)


def test_notes_placeholder_and_split_legal_sentence():
    assert gloss("en", "Allergier, barnstol, firande…") == "Allergies, high chair, celebration…"
    assert gloss("ar", "Allergier, barnstol, firande…") and gloss("fa", "Allergier, barnstol, firande…")
    assert gloss("en", "Till slutet av dagen du ställde dig i kö") == "Until the end of the day you joined the queue"


def test_names_stay_inside_reviewed_templates():
    keep = ["ANAR Restaurang & Bar", "Dagens fisk"]
    assert gloss("en", "Karta: ANAR Restaurang & Bar", keep) == "Map: ANAR Restaurang & Bar"
    assert gloss("ar", "Karta: ANAR Restaurang & Bar", keep) == "الخريطة: " + FSI + "ANAR Restaurang & Bar" + PDI
    assert gloss("en", "Portioner Dagens fisk", keep) == "Portions of Dagens fisk"


def test_trailing_colon_falls_back_to_the_entry_without_it():
    assert gloss("en", "Innehåller:") == "Contains:"
    assert gloss("es", "Allergi:") == "Alergia:"


def test_unknown_texts_and_partial_matches_are_left_to_translate():
    assert gloss("en", "Välkommen till vår restaurang") is None
    assert gloss("en", "Bekräfta bord nu") is None
    assert gloss("en", "{0} personer") is None  # a literal brace is not a placeholder


@pytest.mark.parametrize("lang", LANGS)
def test_every_entry_answers_its_own_swedish_text(lang):
    for sv, values in st.GLOSSARY.items():
        if "{" in sv:
            continue
        assert gloss(lang, sv) == values[lang], sv


def test_isolates_only_in_rtl_and_only_around_latin_or_digits():
    marked, tokens = st.protect("Ring 08-123 45 67 före 18:00")
    out = st.restore(marked.replace("Ring", "اتصل").replace("före", "قبل"), tokens, "ar")
    assert out == "اتصل " + FSI + "08-123 45 67" + PDI + " قبل " + FSI + "18:00" + PDI
    assert st.restore(marked, tokens, "en") == "Ring 08-123 45 67 före 18:00"
    assert st.restore(marked, tokens) == "Ring 08-123 45 67 före 18:00"


# --- table aria-labels and "pers" (retest findings 1 and 2) ---------------------------------

@pytest.mark.parametrize("lang,expected", [
    ("en", "Table 4, 6 seats, available"),
    ("es", "Mesa 4, 6 plazas, libre"),
    ("ar", "الطاولة ⁨4⁩، ⁨6⁩ مقاعد، متاحة"),
    ("fa", "میز ⁨4⁩، ⁨6⁩ صندلی، آزاد"),
])
def test_table_aria_label_available(lang, expected):
    assert gloss(lang, "Bord 4, 6 platser, ledigt") == expected


@pytest.mark.parametrize("status", ["ledigt", "valt", "upptaget", "går inte att välja",
                                    "går inte att välja: bokat", "går inte att välja: spärrat"])
def test_table_aria_label_every_status_in_every_language(status):
    for lang in LANGS:
        out = gloss(lang, f"Bord 12, 2 platser, {status}")
        assert out and "12" in out and "2" in out and "Bord" not in out, (lang, status)


@pytest.mark.parametrize("lang,word", [("en", "people"), ("es", "personas"), ("ar", "أشخاص"), ("fa", "نفر")])
def test_pers_abbreviation(lang, word):
    assert word in gloss(lang, "3 pers")
    assert word in gloss(lang, "3-6 pers")
    assert gloss(lang, "pers") == word

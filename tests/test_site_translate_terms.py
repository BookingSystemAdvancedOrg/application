"""Retest findings 1 and 2: Amazon Translate's custom terminology is only a
hint (case-insensitive, never guaranteed) and was ignored in Arabic and
Persian, also for a capitalised first word. In ar and fa the restaurant words
are therefore put into the source as protected target words."""

import json

import pytest

from shared import site_translate as st
from test_site_translate import app, call  # noqa: F401  (fixture)

FSI, PDI = "⁨", "⁩"


class IgnoringTerminology:
    """A Translate that ignores the terminology (what the live ar/fa calls
    did): a Swedish word is turned into a wrong one, but spans stay."""

    WRONG = {"bordet": "الجدول", "borden": "الجداول", "bord": "جدول", "platser": "مواقع", "pers": "اضغط"}

    def __init__(self):
        self.calls = []

    def translate_text(self, **kwargs):
        self.calls.append(kwargs)
        text = kwargs["Text"]
        out, pos = [], 0
        for m in st._SPAN.finditer(text):
            out.append(self._words(text[pos:m.start()]))
            out.append(m.group(0))
            pos = m.end()
        out.append(self._words(text[pos:]))
        return {"TranslatedText": "".join(out)}

    def _words(self, text):
        import re
        return re.sub(r"\w+", lambda m: self.WRONG.get(m.group(0).lower(), m.group(0)), text)


@pytest.fixture
def ignoring(app, monkeypatch):  # noqa: F811
    fake = IgnoringTerminology()
    monkeypatch.setitem(st._clients, "translate", fake)
    app["fake"] = fake
    return app


def run(app, lang, text):
    status, body, _ = call(app, {"lang": lang, "texts": [text]})
    assert status == 200 and body["partial"] is False, body
    return body["texts"][0]


@pytest.mark.parametrize("lang,expected", [("ar", "الطاولة"), ("fa", "میز")])
def test_sentence_initial_capitalised_word_gets_the_term(ignoring, lang, expected):
    assert expected in run(ignoring, lang, "Bordet är reserverat")
    assert "الجدول" not in run(ignoring, lang, "Bordet är reserverat")


@pytest.mark.parametrize("lang,expected", [("ar", "الطاولات"), ("fa", "میزها")])
def test_plural_and_lowercase(ignoring, lang, expected):
    assert expected in run(ignoring, lang, "Borden är fulla")
    assert expected in run(ignoring, lang, "alla borden är fulla")


def test_pers_abbreviation_is_people_not_press(ignoring):
    assert "أشخاص" in run(ignoring, "ar", "Passar 3 pers och mer")
    assert "نفر" in run(ignoring, "fa", "Passar 3 pers och mer")


def test_apply_terms_is_case_insensitive_and_skips_protected_parts():
    marked, tokens = st.protect("Bordet BORD Bordsnummer Ågården", ["Ågården"])
    out = st.apply_terms(marked, "ar")
    assert out.count('<span translate="no">الطاولة</span>') == 1
    assert '<span translate="no">طاولة</span>' in out
    assert '<span translate="no">Ågården</span>' in out
    assert "بحث" not in out
    # a name that contains a term stays untouched
    marked, _ = st.protect("Bord Ägg", ["Bord Ägg"])
    assert st.apply_terms(marked, "ar") == marked


def test_apply_terms_only_in_ar_and_fa():
    marked, _ = st.protect("Bordet")
    assert st.apply_terms(marked, "en") == marked and st.apply_terms(marked, "es") == marked
    assert st.apply_terms(marked, "fa") != marked


def test_unwrap_terms_leaves_real_tokens():
    marked, tokens = st.protect("Bordet kl 19:00")
    sent = st.apply_terms(marked, "ar")
    assert st.unwrap_terms(sent, "ar").count('translate="no"') == 1
    assert st.restore(st.unwrap_terms(sent, "ar"), tokens, "ar") is not None


def test_terms_in_the_glossary_file_match_the_shape():
    data = json.loads(st.GLOSSARY_FILE.read_text(encoding="utf-8"))
    seen = set()
    for row in data["terms"]:
        assert len(row) == 5 and all(isinstance(x, str) and x for x in row), row
        assert row[0] == row[0].lower() and row[0] not in seen, row[0]
        seen.add(row[0])
    assert {"bord", "bordet", "borden", "pers", "platser"} <= seen
    raw = st.GLOSSARY_FILE.read_text(encoding="utf-8")
    assert chr(0x2013) not in raw and chr(0x2014) not in raw

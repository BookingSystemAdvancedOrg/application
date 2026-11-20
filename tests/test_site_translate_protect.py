"""shared/site_translate.py pure parts: validation, protecting names and
numbers with translate="no", the cache key and the output check."""

import pytest

from shared import site_translate as st


def body(**extra):
    return {"lang": "en", "texts": ["Boka bord"], **extra}


@pytest.mark.parametrize("lang", ["en", "es", "ar", "fa"])
def test_the_four_targets_are_accepted(lang):
    assert st.validate(body(lang=lang), prod=True)[0] == lang


@pytest.mark.parametrize("lang", ["sv", "de", "fa-AF", "EN"])
def test_other_languages_are_unsupported(lang):
    with pytest.raises(ValueError, match="unsupported_lang"):
        st.validate(body(lang=lang), prod=True)


@pytest.mark.parametrize("extra", [
    {"lang": None}, {"lang": 5}, {"texts": []}, {"texts": "Boka"}, {"texts": ["x"] * 101},
    {"texts": [" "]}, {"texts": [3]}, {"texts": ["a" * 3001]}, {"texts": ["a" * 2900] * 7},
    {"keep": "Anar"}, {"keep": ["x"] * 201}, {"keep": [""]}, {"keep": ["a" * 121]},
    {"slug": "Not a slug!"},
])
def test_bad_bodies_are_invalid(extra):
    with pytest.raises(ValueError, match="invalid_request"):
        st.validate(body(**extra), prod=False)


def test_limits_are_inclusive():
    lang, texts, keep, slug = st.validate(body(texts=["a" * 3000] * 6, keep=["k"] * 200), prod=False)
    assert len(texts) == 6 and keep == ["k"] and slug is None


def test_slug_only_outside_prod():
    assert st.validate(body(slug="anar"), prod=False)[3] == "anar"
    with pytest.raises(ValueError, match="invalid_request"):
        st.validate(body(slug="anar"), prod=True)


def test_keep_terms_are_deduplicated_longest_first():
    keep = st.validate(body(keep=["Anar", " Kebab Shiraz ", "Anar"]), prod=True)[2]
    assert keep == ["Kebab Shiraz", "Anar"]


def tokens_of(text, keep=()):
    return st.protect(text, keep)[1]


@pytest.mark.parametrize("text, token", [
    ("Ring oss på +46 8 123 45 67", "+46 8 123 45 67"),
    ("Ring 070-123 45 67 i dag", "070-123 45 67"),
    ("Öppet 18:00 i kväll", "18:00"),
    ("Öppet 18.00 i kväll", "18.00"),
    ("Lunch 11-14 varje dag", "11-14"),
    ("Från 245 kr per person", "245 kr"),
    ("Pris 245:- totalt", "245:-"),
    ("Pris 1 245,50 kr totalt", "1 245,50 kr"),
    ("Kostar SEK 300 nu", "SEK 300"),
    ("Spara 10% i dag", "10%"),
    ("Datum 2026-12-24 kl", "2026-12-24"),
    ("Mejla info@anar.se nu", "info@anar.se"),
    ("Se https://anar.se/meny.", "https://anar.se/meny"),
    ("Org.nr 556677-8899 här", "556677-8899"),
])
def test_numbers_and_contacts_are_protected(text, token):
    assert token in tokens_of(text)


def test_keep_terms_whole_words_case_sensitive():
    assert tokens_of("Välkommen till Anar på Djurgårdsgatan 3", ["Anar", "Djurgårdsgatan 3"]) == [
        "Anar", "Djurgårdsgatan 3"]
    assert tokens_of("Ananas och anar", ["Anar"]) == []
    assert tokens_of("Anarkist", ["Anar"]) == []


def test_longest_match_wins_and_nothing_nests():
    marked, tokens = st.protect("Lägg till Kebab Shiraz 2", ["Kebab", "Kebab Shiraz 2"])
    assert tokens == ["Kebab Shiraz 2"]
    assert marked == 'Lägg till <span translate="no">Kebab Shiraz 2</span>'


def test_source_html_is_escaped():
    marked, tokens = st.protect("A & B <b>", ["B <b>"])
    assert marked == 'A &amp; <span translate="no">B &lt;b&gt;</span>'
    assert tokens == ["B &lt;b&gt;"]


def test_needs_translation_only_with_free_letters():
    assert not st.needs_translation(st.protect("Anar", ["Anar"])[0])
    assert not st.needs_translation(st.protect("18:00 - 22:00")[0])
    assert not st.needs_translation(st.protect("245 kr")[0])
    assert st.needs_translation(st.protect("Boka bord hos Anar", ["Anar"])[0])


def test_cache_key_depends_on_text_language_and_formality():
    a = st.cache_key("en", "Boka")
    assert a.startswith("T#en#") and len(a) == len("T#en#") + 64
    assert a == st.cache_key("en", "Boka")
    assert a != st.cache_key("en", "Boka ")
    assert st.cache_key("es", "Boka").split("#")[2] != a.split("#")[2]


def test_restore_strips_the_markup_and_unescapes():
    marked, tokens = st.protect("Lägg till Kebab & ris för 245 kr", ["Kebab & ris"])
    out = 'Add <span translate="no">Kebab &amp; ris</span> for <span translate="no">245 kr</span>'
    assert st.restore(out, tokens) == "Add Kebab & ris for 245 kr"


def test_restore_accepts_reordered_tokens():
    tokens = st.protect("Öppet 11-14 och 17-22", [])[1]
    out = 'Open <span translate="no">17-22</span> and <span translate="no">11-14</span>'
    assert st.restore(out, tokens) == "Open 17-22 and 11-14"


@pytest.mark.parametrize("out", [
    "Add Kebab for 245 kr",                                                   # markup lost
    'Add <span translate="no">Kebab</span> for <span translate="no">245 SEK</span>',  # token changed
    'Add <span translate="no">Kebab</span> <span translate="no">Kebab</span> 245 kr',  # duplicated
    'Add <span translate="no">Kebab</span> for <span translate="no">245 kr</span> <span>x</span>',
])
def test_restore_rejects_changed_tokens(out):
    tokens = st.protect("Lägg till Kebab för 245 kr", ["Kebab"])[1]
    assert st.restore(out, tokens) is None


def test_restore_replaces_long_dashes_outside_tokens():
    tokens = st.protect("Öppet 18" + chr(0x2013) + "22", [])[1]
    out = "Open " + chr(0x2014) + " always" + chr(0x2014) + 'ish <span translate="no">18' + chr(0x2013) + "22</span>"
    assert st.restore(out, tokens) == "Open, always-ish 18" + chr(0x2013) + "22"

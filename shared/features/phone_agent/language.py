"""The caller's language (owner requirement 2026-11-13: start in Swedish,
switch when the caller speaks another language).

The voice provider transcribes with a multilingual model; Booqy decides the
language of the call from the caller's own words, so the art. 50 short
disclosure, the system prompt and the provider's `language_detection` system
tool all follow one decision that tests can drive. Deterministic and cheap:
function words and letters per language, and a switch only on a clear win,
so "ok", "ja" or a name never flips the call.

`CALL_LANGUAGES` are the languages the multilingual voice model speaks well
that the agent may switch to; Swedish is always first.
"""

from __future__ import annotations

import re

CALL_LANGUAGES = ("sv", "en", "no", "da", "fi", "de", "fr", "es", "it", "pl", "ar")
NAMES = {   # for the system prompt ("answer in <name>")
    "sv": "Swedish", "en": "English", "no": "Norwegian", "da": "Danish", "fi": "Finnish", "de": "German",
    "fr": "French", "es": "Spanish", "it": "Italian", "pl": "Polish", "ar": "Arabic",
}
NAMES_SV = {   # for admin and the call list
    "sv": "svenska", "en": "engelska", "no": "norska", "da": "danska", "fi": "finska", "de": "tyska",
    "fr": "franska", "es": "spanska", "it": "italienska", "pl": "polska", "ar": "arabiska",
}

_WORDS = {
    "sv": "jag och vill inte det är har ni hej tack bord boka för imorgon klockan kan ett vad hur med på vi du "
          "ja nej gärna skulle vara personer kväll också",
    "en": "i and want not the is have you hello hi thanks thank table book for tomorrow at can a what how with "
          "we please would like people evening yes my do does",
    "no": "jeg ikke hva hvordan vil gjerne bestille dere takk hei bord klokka kan og er har med et personer "
          "kveld også",
    "da": "jeg ikke hvad hvordan vil gerne bestille jer tak hej bord klokken kan og er har med et personer "
          "aften også",
    "fi": "minä haluan haluaisin pöytä pöydän varata varaus kiitos moi hei huomenna kello ja ei on onko "
          "teillä mitä kuinka hengelle henkilölle illalla",
    "de": "ich und möchte nicht der die das ist haben sie hallo danke tisch reservieren für morgen um uhr "
          "bitte wir gerne einen personen abend",
    "fr": "je et voudrais veux pas le la est avez vous bonjour merci table réserver pour demain heures nous "
          "une personnes soir s'il plaît",
    "es": "yo y quiero quisiera el la es tienen usted hola gracias mesa reservar para mañana las nosotros "
          "por favor una personas noche",
    "it": "io e vorrei voglio non il è avete lei ciao buongiorno grazie tavolo prenotare per domani alle noi "
          "favore un persone sera",
    "pl": "ja chciałbym chciałabym chcę nie jest macie pan pani dzień dobry dziękuję stolik zarezerwować dla "
          "jutro o godzinie proszę my osób wieczorem",
}
WORDS = {lang: frozenset(words.split()) for lang, words in _WORDS.items()}
# Letters that only some of the languages use: one point each for those languages.
_LETTERS = (
    (re.compile(r"[øæ]"), ("no", "da")),
    (re.compile(r"[äö]"), ("sv", "fi", "de")),
    (re.compile(r"[ßü]"), ("de",)),
    (re.compile(r"[ñ¿¡]"), ("es",)),
    (re.compile(r"[çèêëœ]"), ("fr",)),
    (re.compile(r"[łśżźćńąę]"), ("pl",)),
)
_ARABIC = re.compile(r"[؀-ۿ]")
_TOKEN = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?")
MIN_SCORE = 2


def scores(text) -> dict:
    low = str(text or "").casefold()
    tokens = _TOKEN.findall(low)
    out = {lang: sum(1 for t in tokens if t in words) for lang, words in WORDS.items()}
    for pattern, langs in _LETTERS:
        if pattern.search(low):
            for lang in langs:
                out[lang] += 1
    if len(_ARABIC.findall(low)) >= 3:
        out["ar"] = MIN_SCORE + len(tokens)
    return out


def detect(text, allowed=CALL_LANGUAGES):
    """The language of one caller utterance, or None when it does not tell
    clearly (too short, a tie): the call then keeps its language."""
    ranked = sorted(((score, lang) for lang, score in scores(text).items() if lang in allowed), reverse=True)
    if not ranked or ranked[0][0] < MIN_SCORE:
        return None
    if len(ranked) > 1 and ranked[1][0] >= ranked[0][0]:
        return None
    return ranked[0][1]


def next_language(current, text, allowed=CALL_LANGUAGES):
    """The call's language after the caller said `text`."""
    found = detect(text, allowed)
    return found if found and found != current else current

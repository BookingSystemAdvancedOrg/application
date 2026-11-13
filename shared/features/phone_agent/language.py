"""The caller's language (owner requirement 2026-11-13: start in Swedish,
then speak whatever language the caller speaks).

Two signals, one decision per turn:
  * `detect()`: function words and letters of eleven common languages, a
    switch only on a clear win ("ok", "ja" or a name never flips the call).
    Fast and deterministic: it sets the language the system prompt asks for
    before the model runs (and drives the simulator).
  * the model's own tag: every reply starts with `[<code>]` (`tagged()`), so
    any language the voice model speaks is followed, not only the eleven.

`VOICE_LANGUAGES` are the languages ElevenLabs' multilingual low-latency
model speaks (eleven_flash_v2_5); the call may switch to any of them. The
art. 50 short disclosure exists in each (copy.DISCLOSURE_SHORT).
"""

from __future__ import annotations

import re

CALL_LANGUAGES = ("sv", "en", "no", "da", "fi", "de", "fr", "es", "it", "pl", "ar")   # detect() knows these
NAMES = {   # for the system prompt ("answer in <name>")
    "sv": "Swedish", "en": "English", "no": "Norwegian", "da": "Danish", "fi": "Finnish", "de": "German",
    "fr": "French", "es": "Spanish", "it": "Italian", "pl": "Polish", "ar": "Arabic", "pt": "Portuguese",
    "nl": "Dutch", "tr": "Turkish", "ru": "Russian", "uk": "Ukrainian", "cs": "Czech", "sk": "Slovak",
    "hu": "Hungarian", "ro": "Romanian", "bg": "Bulgarian", "hr": "Croatian", "el": "Greek", "ja": "Japanese",
    "zh": "Chinese", "ko": "Korean", "hi": "Hindi", "id": "Indonesian", "ms": "Malay", "fil": "Filipino",
    "vi": "Vietnamese", "ta": "Tamil",
}
VOICE_LANGUAGES = tuple(NAMES)
_TAG = re.compile(r"^\s*\[([a-z]{2,3})(?:[-_][A-Za-z]{2})?\]\s*")

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


def tagged(text):
    """(language, text without the tag) of a model reply that starts with
    `[<code>]`; (None, text) when it has none or names a language the voice
    cannot speak."""
    m = _TAG.match(str(text or ""))
    if not m:
        return None, text
    code = m.group(1).lower()
    code = {"nb": "no", "nn": "no", "tl": "fil"}.get(code, code)
    return (code if code in VOICE_LANGUAGES else None), str(text)[m.end():]


def next_language(current, text, allowed=CALL_LANGUAGES):
    """The call's language after the caller said `text`."""
    found = detect(text, allowed)
    return found if found and found != current else current

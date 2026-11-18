"""The caller's language (owner requirement 2026-11-13 and 2026-11-17: start
in Swedish, then speak whatever language the caller speaks, Somali, Russian
and German as much as Swedish and English).

The model decides, the voice provider follows, the fast detector is a hint:
  * `hint()`: function words, letters and scripts, before the model runs, so
    the system prompt already asks for the caller's language in the very
    first reply after a switch. A clear win only ("ok", "ja" or a name never
    counts). It also drives the scripted simulator.
  * the model's own tag: every reply starts with `[<code>]` (`tag()`), so any
    language the model knows is recognised, not only the ones the word lists
    know.
  * `decide()`: hysteresis between the two, so one word, a name or a street
    never flips the call, and a language the voice cannot speak is answered
    by the fixed "I cannot speak your language" sentences instead.

`voice_languages(model)` are the languages the ElevenLabs TTS model speaks;
the call may switch to any of them. The list depends on the model
(PHONE_AGENT_TTS_MODEL): flash v2.5 speaks 32, v4 Turbo and v3 conversational
also speak Somali, Persian, Amharic and more [VERIFY IN SANDBOX: the v4 and
v3 lists come from ElevenLabs' release notes, not from the account].

`AWS_LANGUAGES` are the languages of the Amazon Connect + Lex V2 line (calls
whose callRef starts with CONNECT_PREFIX): one Lex locale and one Polly voice
each, from aws_languages.json (Terraform keeps an identical copy in
infrastructure/phone-test-line/languages.json). `call_languages(call_ref)`
picks the list for a call.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

NAMES = {   # every language the model may tag, for the prompt ("answer in <name>") and the provider
    "sv": "Swedish", "en": "English", "no": "Norwegian", "da": "Danish", "fi": "Finnish", "de": "German",
    "fr": "French", "es": "Spanish", "it": "Italian", "pl": "Polish", "ar": "Arabic", "pt": "Portuguese",
    "nl": "Dutch", "tr": "Turkish", "ru": "Russian", "uk": "Ukrainian", "cs": "Czech", "sk": "Slovak",
    "hu": "Hungarian", "ro": "Romanian", "bg": "Bulgarian", "hr": "Croatian", "el": "Greek", "ja": "Japanese",
    "zh": "Chinese", "ko": "Korean", "hi": "Hindi", "id": "Indonesian", "ms": "Malay", "fil": "Filipino",
    "vi": "Vietnamese", "ta": "Tamil",
    # v4 / v3 languages beyond flash v2.5
    "so": "Somali", "fa": "Persian", "ckb": "Central Kurdish (Sorani)", "am": "Amharic", "bs": "Bosnian",
    "sr": "Serbian", "he": "Hebrew", "th": "Thai", "ur": "Urdu", "bn": "Bengali", "ps": "Pashto",
    "sw": "Swahili", "et": "Estonian", "lv": "Latvian", "lt": "Lithuanian", "sl": "Slovenian",
    "is": "Icelandic", "mk": "Macedonian", "sq": "Albanian", "hy": "Armenian", "ka": "Georgian",
    "az": "Azerbaijani", "kk": "Kazakh", "uz": "Uzbek", "pa": "Punjabi",
    # languages the model may recognise but no voice model speaks: the call does not switch
    "ti": "Tigrinya", "kmr": "Northern Kurdish (Kurmanji)", "om": "Oromo",
    # the Connect + Lex line only (AWS_LANGUAGES)
    "ca": "Catalan",
}
# ElevenLabs eleven_flash_v2_5 / eleven_turbo_v2_5 (the default until the sandbox test).
FLASH_LANGUAGES = ("sv", "en", "no", "da", "fi", "de", "fr", "es", "it", "pl", "ar", "pt", "nl", "tr", "ru",
                   "uk", "cs", "sk", "hu", "ro", "bg", "hr", "el", "ja", "zh", "ko", "hi", "id", "ms", "fil",
                   "vi", "ta")
# eleven_v4_turbo and eleven_v3_conversational [VERIFY IN SANDBOX].
V4_LANGUAGES = FLASH_LANGUAGES + ("so", "fa", "ckb", "am", "bs", "sr", "he", "th", "ur", "bn", "ps", "sw", "et",
                                  "lv", "lt", "sl", "is", "mk", "sq", "hy", "ka", "az", "kk", "uz", "pa")
_MODEL_LANGUAGES = {"eleven_v4_turbo": V4_LANGUAGES, "eleven_v3_conversational": V4_LANGUAGES,
                    "eleven_multilingual_v2": FLASH_LANGUAGES[:29]}
DEFAULT_TTS_MODEL = "eleven_flash_v2_5"
# Every language any supported voice model speaks: what the owner's config may list.
VOICE_LANGUAGES = V4_LANGUAGES
CALL_LANGUAGES = VOICE_LANGUAGES   # kept for callers of the old name
# Languages where speech recognition is weak (ElevenLabs' own ratings): the prompt
# asks the model to repeat every detail and to have names spelled.
LOW_ASR = ("so", "am", "ckb", "ps", "ti", "kmr", "om", "sw", "ur", "pa")
_ALIASES = {"nb": "no", "nn": "no", "tl": "fil", "ku": "kmr", "iw": "he", "prs": "fa", "zh-cn": "zh",
            "pt-br": "pt", "sh": "sr"}
_TAG = re.compile(r"^\s*\[([A-Za-z]{2,3})(?:[-_][A-Za-z]{2,4})?\]\s*")


# The Amazon Connect + Lex V2 line: our code -> {lexLocale, connectLanguage, voice, engine}.
# Verified 2026-11-18 against the Lex V2 locale list, Polly's voice list and the Connect Set voice
# docs [VERIFY ON A REAL CALL: the pairing, design risk 1]. Norwegian, Chinese and Cantonese are
# not on the line (the bot has no locale for them). The file must stay byte-identical to
# infrastructure/phone-test-line/languages.json (tests pin the key set).
AWS_LANGUAGES = json.loads((Path(__file__).with_name("aws_languages.json")).read_text("utf-8"))["languages"]
AWS_CALL_LANGUAGES = tuple(AWS_LANGUAGES)
CONNECT_PREFIX = "connect-"     # callRef of a call on the Connect + Lex line
# A Connect call switches language at most this often (each switch restarts the Lex session in the
# flow); after that it stays in its language (`languageSwitches` on the call row).
MAX_CONNECT_SWITCHES = 3
_LOCALE_LANGUAGE = {spec["lexLocale"]: code for code, spec in AWS_LANGUAGES.items()}


def is_connect(call_ref) -> bool:
    return str(call_ref or "").startswith(CONNECT_PREFIX)


def call_languages(call_ref=None, model=None):
    """The languages a call may switch to: the AWS list on the Connect line,
    else what the ElevenLabs TTS model speaks."""
    return AWS_CALL_LANGUAGES if is_connect(call_ref) else voice_languages(model)


def locale_language(locale):
    """Our code for a Lex locale (sv_SE -> sv), else None."""
    return _LOCALE_LANGUAGE.get(str(locale or ""))


def voice_languages(model=None):
    """The languages the configured TTS model speaks."""
    model = model or os.environ.get("PHONE_AGENT_TTS_MODEL") or DEFAULT_TTS_MODEL
    return _MODEL_LANGUAGES.get(model, FLASH_LANGUAGES)


def name(code):
    return NAMES.get(code, code)


# --- the fast hint ------------------------------------------------------------------------------

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
    "so": "waxaan rabaa miis fadlan haa maya mahadsanid iyo berri caawa qof saacadda ma waan waxaad salaan "
          "hadda dad galabta",
    "tr": "ve bir masa için yarın lütfen teşekkür istiyorum kişilik saat merhaba evet hayır akşam rezervasyon "
          "kişi ben",
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
    (re.compile(r"[ışğ]"), ("tr",)),
)
# Scripts that name the language on their own (three letters or more), first match wins.
_SCRIPTS = (
    (re.compile(r"[ሀ-፿]"), None),           # Ethiopic: Amharic or Tigrinya, only the model can tell
    (re.compile(r"[Ͱ-Ͽ]"), "el"),
    (re.compile(r"[֐-׿]"), "he"),
    (re.compile(r"[ऀ-ॿ]"), "hi"),
    (re.compile(r"[฀-๿]"), "th"),
    (re.compile(r"[가-힯]"), "ko"),
    (re.compile(r"[぀-ヿ]"), "ja"),
    (re.compile(r"[一-鿿]"), "zh"),
)
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_UKRAINIAN = re.compile(r"[іїєґ]")
_ARABIC_SCRIPT = re.compile(r"[؀-ۿݐ-ݿ]")
_SORANI = re.compile(r"[ڕڵێۆ]")
_PERSIAN = re.compile(r"[پچژگ]")
_ARABIC_ONLY = re.compile(r"[ةيى]")        # Arabic letters Persian writes differently
_PERSIAN_YEH = re.compile(r"ی")
_TOKEN = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?")
MIN_SCORE = 2


def _script(low):
    """A language read from the script alone, else None."""
    for pattern, lang in _SCRIPTS:
        if len(pattern.findall(low)) >= 3:
            return lang or ""
    if len(_CYRILLIC.findall(low)) >= 3:
        return "uk" if _UKRAINIAN.search(low) else "ru"
    if len(_ARABIC_SCRIPT.findall(low)) >= 3:
        if _SORANI.search(low):
            return "ckb"
        if _PERSIAN.search(low) or (_PERSIAN_YEH.search(low) and not _ARABIC_ONLY.search(low)):
            return "fa"
        return "ar"
    return None


def scores(text) -> dict:
    low = str(text or "").casefold()
    tokens = _TOKEN.findall(low)
    out = {lang: sum(1 for t in tokens if t in words) for lang, words in WORDS.items()}
    for pattern, langs in _LETTERS:
        if pattern.search(low):
            for lang in langs:
                out[lang] += 1
    script = _script(low)
    if script:
        out[script] = MIN_SCORE + len(tokens)
    elif script == "":                     # a script that names no single language: no word list counts
        out = {lang: 0 for lang in out}
    return out


def detect(text, allowed=None):
    """The language of one caller utterance, or None when it does not tell
    clearly (too short, a tie): the call then keeps its language."""
    ranked = sorted(((score, lang) for lang, score in scores(text).items()
                     if allowed is None or lang in allowed), reverse=True)
    if not ranked or ranked[0][0] < MIN_SCORE:
        return None
    if len(ranked) > 1 and ranked[1][0] >= ranked[0][0]:
        return None
    return ranked[0][1]


hint = detect


def next_language(current, text, allowed=None):
    """The call's language after the caller said `text` (the hint alone)."""
    found = detect(text, allowed)
    return found if found and found != current else current


# --- the model's tag --------------------------------------------------------------------------

def normalise(code):
    code = str(code or "").lower()
    return _ALIASES.get(code, code)


def tag(text):
    """(code, rest) of a model reply that starts with `[<code>]`: code is a
    language the model named (known in NAMES), else None; rest is the reply
    without the tag."""
    m = _TAG.match(str(text or ""))
    if not m:
        return None, text
    code = normalise(m.group(1))
    return (code if code in NAMES else None), str(text)[m.end():]


def tagged(text):
    """(code, text without the tag); code is None unless the voice speaks it."""
    code, rest = tag(text)
    return (code if code in VOICE_LANGUAGES else None), rest


# --- the decision ------------------------------------------------------------------------------

# A caller asking for a language ("can we speak English", "auf Deutsch bitte").
_ASKED = re.compile(
    r"\b(english|engelska|englanniksi|englisch|anglais|inglés|ingles|svenska|swedish|schwedisch|deutsch|german|"
    r"tyska|french|français|franska|spanish|español|spanska|arabic|arabiska|العربية|عربي|somali|soomaali|"
    r"somaliska|russian|ryska|русски|по-русски|turkish|türkçe|turkiska|polish|polski|polska|finnish|suomeksi|"
    r"finska|persian|farsi|فارسی|persiska|kurdish|kurdiska|amharic|amhariska|tigrinya|tigrinja)\b", re.I)


def asked_for_language(text) -> bool:
    return bool(_ASKED.search(str(text or "")))


def words(text) -> int:
    return len(_TOKEN.findall(str(text or "")))


def decide(current, hint_code, tag_code, text, *, allowed, previous_tag=None):
    """(language, unsupported) after one turn.

    `language` is the call's language from now on; `unsupported` is a
    language code the model recognised that the voice cannot speak (or the
    owner did not allow), else None. A switch needs the model's tag to differ
    from the current language and one more sign: the hint agrees, the caller
    said three words or more, the caller asked for a language, or the
    previous turn was tagged the same. A caller still speaking the current
    language (the hint says so) never switches, whatever the tag."""
    if tag_code is None:
        return (hint_code if hint_code in allowed else current), None
    if tag_code == current:
        return current, None
    asked = asked_for_language(text)
    if hint_code == current and not asked:
        return current, None
    sure = hint_code == tag_code or words(text) >= 3 or asked or previous_tag == tag_code
    if not sure:
        return current, None
    if tag_code not in allowed:
        return current, tag_code
    return tag_code, None

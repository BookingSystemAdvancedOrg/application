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
    "ca": "Catalan", "yue": "Cantonese", "cy": "Welsh",
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
_ALIASES = {"nb": "no", "nn": "no", "tl": "fil", "ku": "kmr", "iw": "he", "prs": "fa", "zh-cn": "zh", "zh-hk": "yue",
            "pt-br": "pt", "sh": "sr"}
_TAG = re.compile(r"^\s*\[([A-Za-z]{2,3})(?:[-_][A-Za-z]{2,4})?\]\s*")


# The Amazon Connect + Lex V2 line: our code -> {lexLocale, connectLanguage, voice, engine}.
# Verified 2026-11-18 against the Lex V2 locale list, Polly's voice list and the Connect Set voice
# docs [VERIFY ON A REAL CALL: the pairing, design risk 1]. The file must stay byte-identical to
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
    # Bosnian, Croatian and Serbian (Latin script) as one group: "ja" and "da" are left out (Swedish yes).
    "bs": "želim želio željela bih sto stol rezervisati rezervirati rezervaciju za sutra molim hvala dobar "
          "dan večeras osobe osoba sati može li imate trebam",
    "id": "saya mau ingin pesan meja untuk besok malam orang terima kasih tolong bisa apakah ada selamat "
          "pagi siang reservasi",
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
_ARABIC_ONLY = re.compile(r"[ةيىأإ]")      # Arabic letters (and hamza seats) Persian writes differently
_PERSIAN_YEH = re.compile(r"[یک]")
_URDU = re.compile(r"[ٹڈڑںےھ]")            # letters Urdu has and Arabic and Persian do not
# Common words that tell Persian from Arabic when the letters do not (both use the Arabic script).
_FA_WORDS = frozenset("است هست می میخواهم میخوام برای فردا شب نفر میز رزرو ممنون لطفا لطفاً سلام بله نه "
                      "دارید خواهش ساعت امشب".split())
_AR_WORDS = frozenset("أريد اريد من فضلك شكرا شكراً طاولة حجز غدا غداً مساء أشخاص اشخاص هل نعم لا مرحبا "
                      "الساعة الليلة لو سمحت".split())
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
        if _URDU.search(low):
            return "ur"
        words = set(re.findall(r"[؀-ۿݐ-ݿ]+", low.replace("\u200c", "")))
        fa, ar = len(words & _FA_WORDS), len(words & _AR_WORDS)
        if fa != ar:
            return "fa" if fa > ar else "ar"
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
#
# Switch rules (owner 2026-11-20, MULTILINGUAL.md stage 1). The call starts in Swedish. A switch needs:
#   * an explicit request ("can we speak English", "auf Deutsch bitte") that the reply follows, or the
#     caller's yes to Booqy's own "shall we continue in ...?" question; or
#   * strong evidence: at least MIN_WORDS content words, the model's tag and the word hint agree; or
#   * the same signal twice: the model tagged the same language in the previous turn too (MIN_WORDS words).
# Never on fewer than MIN_WORDS content words (restaurant and location names do not count), never within
# MIN_TURNS_AFTER_SWITCH caller turns after a switch, never more than MAX_SWITCHES times per call. When
# the reply is in another language but no rule allows the switch, Booqy asks a short question in both
# languages instead (`Decision.ask`). A language the line cannot speak is reported (`unsupported`), never
# answered silently in Swedish.

MIN_WORDS = 3
MIN_TURNS_AFTER_SWITCH = 2
MAX_SWITCHES = MAX_CONNECT_SWITCHES

# A caller naming a language ("can we speak English", "auf Deutsch", "بالعربي"): word -> code.
_REQUEST_WORDS = {
    "sv": "svenska swedish schwedisch suédois suedois sueco svedese szwedzku szwedzki isveççe swedia švedski "
          "svenskt سوئدی السويدية",
    "en": "english engelska englisch anglais inglés ingles inglese angielsku angielski ingilizce inggris engleski "
          "englanniksi انگلیسی الإنجليزية الانجليزية",
    "de": "deutsch german tyska allemand alemán aleman tedesco niemiecku niemiecki almanca jerman njemački "
          "nemački آلمانی الألمانية",
    "fr": "french franska français francais französisch francés frances francese francusku francuski fransızca "
          "prancis فرانسوی الفرنسية",
    "es": "spanish spanska español espanol spanisch espagnol spagnolo hiszpańsku hiszpański ispanyolca spanyol "
          "španjolski španski اسپانیایی الإسبانية",
    "it": "italian italienska italiano italienisch italien italiano włosku włoski italyanca italia talijanski "
          "italijanski ایتالیایی الإيطالية",
    "pl": "polish polska polski polsku polnisch polonais polaco polacco lehçe polandia poljski لهستانی البولندية",
    "tr": "turkish turkiska türkçe turkce türkisch turc turco turecku turecki turki turski ترکی التركية",
    "ar": "arabic arabiska arabisch arabe árabe arabo arabsku arapça arab arapski عربي العربية بالعربي "
          "بالعربية عربی",
    "fa": "persian persiska farsi persisch persan persa persiano persku farsça persia perzijski فارسی بفارسی "
          "الفارسية",
    "bs": "bosnian bosniska bosnisch bosanski bosanskom bosnio boşnakça hrvatski hrvatskom srpski srpskom "
          "croatian kroatiska serbian serbiska",
    "id": "indonesian indonesiska indonesisch indonésien indonesio indonesiano indonezyjsku endonezce indonesia",
    "da": "danish danska dänisch dansk",
    "fi": "finnish finska finnisch suomeksi suomi",
    "nl": "dutch nederländska holländska niederländisch nederlands",
    "pt": "portuguese portugisiska portugiesisch português portugues",
    "ru": "russian ryska russisch русски русском по-русски",
    "so": "somali somaliska soomaali",
    "ckb": "kurdish kurdiska sorani",
    "am": "amharic amhariska",
    "ti": "tigrinya tigrinja",
}
_REQUEST = {}
for _code, _words in _REQUEST_WORDS.items():
    for _word in _words.split():
        _REQUEST.setdefault(_word.casefold(), _code)
_ASKED = re.compile(r"(?<![\w])(" + "|".join(sorted((re.escape(w) for w in _REQUEST), key=len, reverse=True))
                    + r")(?![\w])", re.I)


def requested_languages(text) -> set:
    """Every language the caller named ("English", "på svenska", "فارسی")."""
    return {_REQUEST[m.group(1).casefold()] for m in _ASKED.finditer(str(text or ""))
            if m.group(1).casefold() in _REQUEST}


def asked_for_language(text) -> bool:
    return bool(requested_languages(text))


def words(text) -> int:
    return len(_TOKEN.findall(str(text or "")))


def content_words(text, names=()) -> int:
    """Words of the utterance that are not a restaurant, location or other proper name of this call."""
    skip = {t for name in names or () for t in _TOKEN.findall(str(name or "").casefold())}
    return sum(1 for t in _TOKEN.findall(str(text or "").casefold()) if t not in skip)


def without_names(text, names=()):
    """The utterance without the call's proper names (for the hint)."""
    out = str(text or "")
    for name in sorted((str(n) for n in names or () if n), key=len, reverse=True):
        out = re.sub(r"(?<!\w)" + re.escape(name) + r"(?!\w)", " ", out, flags=re.I)   # whole words only
    return out


class Decision:
    """The call's language after one turn: `language` (the call's language from now on), `unsupported`
    (a language the model recognised that the line cannot speak), `reason` for the log (same, capped,
    explicit, asked, short, unsupported, dwell, strong, repeated, uncertain, hint) and `ask` (a language
    the reply is in that the call did not switch to: Booqy asks whether to continue in it)."""

    __slots__ = ("language", "unsupported", "reason", "ask")

    def __init__(self, language, unsupported=None, reason="same", ask=None):
        self.language, self.unsupported, self.reason, self.ask = language, unsupported, reason, ask

    def __iter__(self):            # (language, unsupported), as decide() always answered
        return iter((self.language, self.unsupported))

    def __repr__(self):
        return f"Decision({self.language!r}, {self.unsupported!r}, {self.reason!r}, ask={self.ask!r})"


def _dwelling(turns_since_switch):
    return turns_since_switch is not None and int(turns_since_switch) <= MIN_TURNS_AFTER_SWITCH


def pre_switch(current, hint_code, text, *, allowed, names=(), turns_since_switch=None, switches=0,
               max_switches=MAX_SWITCHES):
    """The language the prompt asks for before the model runs: the hint's
    language when it already is strong evidence (allowed, MIN_WORDS content
    words, not right after a switch, switches left), else the call's."""
    if hint_code in (None, current) or hint_code not in allowed:
        return current
    if max_switches is not None and int(switches or 0) >= max_switches:
        return current
    if content_words(text, names) < MIN_WORDS or _dwelling(turns_since_switch):
        return current
    return hint_code


def decision(current, hint_code, tag_code, text, *, allowed, previous_tag=None, names=(), turns_since_switch=None,
             switches=0, max_switches=None, asked_switch=None, prompt_lang=None) -> Decision:
    """The switch rules above for one turn. `tag_code` is the model's `[xx]`
    tag (None: the reply is in `prompt_lang`, the language the prompt asked
    for); `asked_switch` the language Booqy asked about in the previous turn."""
    if tag_code is None:
        tag_code = prompt_lang if prompt_lang else (
            pre_switch(current, hint_code, text, allowed=allowed, names=names, turns_since_switch=turns_since_switch,
                       switches=switches, max_switches=max_switches))
        if tag_code != current:
            return Decision(tag_code, None, "hint")
    if tag_code == current:
        return Decision(current, None, "same")
    if max_switches is not None and int(switches or 0) >= max_switches:
        return Decision(current, None, "capped")
    explicit = tag_code in requested_languages(without_names(text, names))
    if explicit or (asked_switch and tag_code == asked_switch):
        reason = "explicit" if explicit else "asked"
        return Decision(current, tag_code, reason) if tag_code not in allowed else Decision(tag_code, None, reason)
    if content_words(text, names) < MIN_WORDS:
        return Decision(current, None, "short", ask=tag_code)
    if tag_code not in allowed:
        return Decision(current, tag_code, "unsupported")
    if _dwelling(turns_since_switch):
        return Decision(current, None, "dwell", ask=tag_code)
    if hint_code == tag_code:
        return Decision(tag_code, None, "strong")
    if previous_tag == tag_code and hint_code in (None, tag_code):
        return Decision(tag_code, None, "repeated")
    return Decision(current, None, "uncertain", ask=tag_code)


def decide(current, hint_code, tag_code, text, *, allowed, previous_tag=None, **rules):
    """(language, unsupported) after one turn (see `decision`)."""
    return tuple(decision(current, hint_code, tag_code, text, allowed=allowed, previous_tag=previous_tag, **rules))

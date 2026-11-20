"""Language identification and the switch rules. Owner: builder 'listen'.

Evidence per caller utterance (MULTILINGUAL.md section 3, "Confidence and
hysteresis rules"; Transcribe identify-language replaces the local LID
model there: owner 2026-11-20, AWS only):
  * Transcribe LID scores: the first utterance of the call runs on an
    identify-language stream (candidates_for()), later utterances only on
    a probe (Transcriber.probe) when the text hint or the model tag points
    elsewhere, or the ASR confidence is under LanguageConfig.low_asr_confidence;
  * the text hint: shared language.hint() on the transcript, with tenant
    proper nouns (restaurant, location, menu names) stripped first
    (`strip_names`) so "Trattoria Roma" or "kebab" never count;
  * the explicit request: shared language.asked_for_language();
  * the model tag of the previous reply (follow()).

LanguageTracker is pure (no I/O). It implements rules 1 to 9 with the
LanguageConfig numbers and returns one LanguageDecided per utterance.
The brain stays the authority on the call row: when session.call_turn
switches on its own (the model tag, language.decide) the orchestrator
calls `follow()` so both agree, and a switch decided here is applied with
AgentBrain.set_language before the turn runs. Switch cap: one count shared
with the call row (`languageSwitches`, language.MAX_CONNECT_SWITCHES).
Explicit requests (rule 1) are matched with the extended phrase table
EXPLICIT below (a superset of the shared language._ASKED: it also names the
target language, which the shared helper does not); a request that names
no known language still counts as "asked" through the shared helper and
then only asks for a LID probe.
Unsupported (rule 8): a confident language outside languages.SPOKEN
(fa, bs, id, so ...) gives LanguageAction.UNSUPPORTED with the code; the
brain answers with the shared fixed sentences and the callback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from voice_gateway import languages
from voice_gateway.config import LanguageConfig
from voice_gateway.events import LanguageAction, LanguageDecided, LanguageEvidence
from shared.features.phone_agent import language as pa_language

# --- explicit requests (rule 1) -----------------------------------------------------------------

_EXPLICIT_WORDS: dict[str, str] = {
    "sv": "svenska swedish schwedisch suédois sueco svedese szwedzku isveççe",
    "en": "english engelska englisch anglais inglés ingles inglese angielsku ingilizce",
    "de": "deutsch german tyska allemand alemán aleman tedesco niemiecku almanca",
    "fr": "french français francais franska französisch francés frances francese francuski fransızca",
    "es": "spanish español espanol spanska spanisch espagnol spagnolo hiszpańsku ispanyolca",
    "ar": "arabic arabiska arabisch arabe árabe arabo arabski arapça",
    "fa": "persian farsi persiska persisch persan persa perski",
    "so": "somali soomaali somaliska",
    "ru": "russian ryska russisch russe ruso russo rosyjsku rusça",
    "tr": "turkish türkçe turkce turkiska türkisch turc turco turecku",
    "pl": "polish polski polska polnisch polonais polaco polacco lehçe",
    "fi": "finnish suomeksi finska finnisch finlandais",
    "bs": "bosnian bosanski bosanskom bosniska bosnisch bosnien",
    "id": "indonesian indonesiska indonesisch indonesio",
    "da": "danish dansk danska dänisch danois danés",
    "no": "norwegian norska norsk norwegisch",
    "it": "italian italiano italienska italienisch italien",
    "pt": "portuguese português portugues portugisiska portugiesisch",
    "nl": "dutch nederlands holländska niederländisch",
    "uk": "ukrainian ukrainska ukrainisch українською",
    "kmr": "kurdish kurdiska kurdisch",
    "am": "amharic amhariska",
    "ti": "tigrinya tigrinja",
}
_EXPLICIT_SCRIPT = {          # Arabic and Cyrillic words: no word boundaries, matched as substrings
    "ar": ("العربية", "عربي", "بالعربي", "عربى"),
    "fa": ("فارسی", "فارسي", "بالفارسی", "فارسى"),
    "en": ("الانجليزية", "انجليزي", "الإنجليزية", "إنجليزي", "بالانجليزي", "английски", "по-английски"),
    "de": ("الألمانية", "ألماني", "по-немецки"),
    "fr": ("الفرنسية", "فرنسي", "по-французски"),
    "ru": ("русски", "по-русски", "русский"),
    "tr": ("التركية", "تركي"),
    "so": ("الصومالية",),
    "sv": ("السويدية", "سويدي"),
}
_EXPLICIT_LATIN = {code: re.compile(r"(?<!\w)(?:" + "|".join(re.escape(w) for w in words.split())
                                    + r")(?!\w)", re.I)
                   for code, words in _EXPLICIT_WORDS.items()}
# "bahasa" alone is a word in some languages' Latin text; only the full phrase names Indonesian
_EXPLICIT_LATIN["id"] = re.compile(r"(?<!\w)(?:bahasa indonesia|indonesian|indonesiska|indonesisch|indonesio)(?!\w)", re.I)
EXPLICIT = _EXPLICIT_LATIN

_YES = frozenset("ja jaa japp jo gärna okej ok visst precis yes yeah yep sure please okay oui sí si da tak evet "
                 "نعم اه".split())
_NO = frozenset("nej nä no nope non nein nie hayır hayir لا".split())
_TOKEN = re.compile(r"[^\W_]+")


AWAY_PROBE_CONFIDENCE = 0.7


def explicit_targets(text: str) -> list[str]:
    """Language codes the caller asks for ("can we speak English"), in the
    order they occur. Empty when none is named."""
    low = str(text or "").casefold()
    found: list[tuple[int, str]] = []
    for code, rx in EXPLICIT.items():
        m = rx.search(low)
        if m:
            found.append((m.start(), code))
    for code, words in _EXPLICIT_SCRIPT.items():
        for w in words:
            i = low.find(w)
            if i >= 0:
                found.append((i, code))
                break
    seen: list[str] = []
    for _, code in sorted(found):
        if code not in seen:
            seen.append(code)
    return seen


# --- inputs -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class UtteranceInfo:
    at_ms: int
    text: str
    speech_ms: int
    asr_language: str                                  # language of the stream that produced the text
    asr_confidence: float | None = None
    lid_scores: tuple[tuple[str, float], ...] = ()     # our codes, from Transcribe LID, may be empty
    first: bool = False                                # first caller utterance of the call


def candidates_for(caller_e164: str | None, cfg: LanguageConfig, *, current: str = "sv") -> tuple[str, ...]:
    """Up to 5 our-codes for an identify-language stream: cfg.always_candidates,
    then the caller's country candidates (longest prefix first), then
    cfg.default_extra_candidates; only languages Transcribe can hear
    (languages.to_transcribe), no duplicates, `current` first."""
    country: tuple[str, ...] = ()
    number = str(caller_e164 or "")
    for prefix in sorted(cfg.country_candidates, key=len, reverse=True):
        if number.startswith(prefix):
            country = tuple(cfg.country_candidates[prefix])
            break
    out: list[str] = []
    for code in (current, *cfg.always_candidates, *country, *cfg.default_extra_candidates):
        code = pa_language.normalise(code)
        if code not in out and languages.to_transcribe(code):
            out.append(code)
    return tuple(out[:5])


def strip_names(text: str, names: tuple[str, ...]) -> str:
    """The text without the tenant's proper nouns (case-insensitive, whole
    words). A multi-word name goes as a phrase and, for its longer words
    that are no function words of any language, word by word too (a caller
    says "Roma" for "Trattoria Roma")."""
    out = str(text or "")
    common = frozenset().union(*pa_language.WORDS.values())
    parts: list[str] = []
    for name in sorted({n.strip() for n in names if n and n.strip()}, key=len, reverse=True):
        parts.append(name)
        for word in _TOKEN.findall(name):
            if len(word) >= 4 and word.casefold() not in common and word != name:
                parts.append(word)
    for part in parts:
        out = re.sub(r"(?<!\w)" + re.escape(part) + r"(?!\w)", " ", out, flags=re.I)
    return re.sub(r"\s+", " ", out).strip()


# --- the tracker --------------------------------------------------------------------------------

class LanguageTracker:
    def __init__(self, cfg: LanguageConfig, *, spoken: tuple[str, ...], names: tuple[str, ...] = (),
                 current: str = "sv", switches: int = 0, caller_e164: str | None = None) -> None:
        self.cfg, self.spoken, self.names = cfg, tuple(spoken), tuple(names)
        self.current, self.switches = current, switches
        self.caller = caller_e164
        self.turn = 0
        self._switch_turn = -10                      # the caller turn at which the last switch happened
        self._left: dict[str, int] = {}              # language -> turn it was left
        self._prev: tuple[str, float, int, int] | None = None     # top code, p, speech_ms, words (last turn)
        self._asked: set[str] = set()                # candidates already asked about (rule 5: once)
        self._pending_ask: str | None = None
        self._misses = 0                             # consecutive poor-audio utterances (rule 7)

    @property
    def locked(self) -> bool:
        """max_auto_switches reached: only explicit requests switch now."""
        return self.switches >= self.cfg.max_auto_switches

    # --- helpers ---------------------------------------------------------------------------

    def _clean(self, utt: UtteranceInfo) -> str:
        return strip_names(utt.text, self.names)

    def _hint(self, utt: UtteranceInfo) -> str | None:
        clean = self._clean(utt)
        return pa_language.detect(clean) if clean else None

    def _explicit(self, text: str) -> str | None:
        targets = explicit_targets(text)
        if not targets:
            return None
        for code in targets:
            if code != self.current:
                return code
        return targets[0]

    def _stay(self) -> bool:
        return self.turn - self._switch_turn - 1 < self.cfg.stay_turns_after_switch

    def _can_auto_switch_to(self, code: str, first: bool) -> bool:
        if self.locked or code == self.current:
            return False
        if self._stay() and not first:
            return False
        left = self._left.get(code)
        return not (left is not None and self.turn - left <= self.cfg.stay_turns_after_switch)

    def _switch(self, code: str, *, automatic: bool) -> None:
        self._left[self.current] = self.turn
        self.current = code
        self._switch_turn = self.turn
        if automatic:
            self.switches += 1

    def _result(self, utt_at: int, action: LanguageAction, candidate: str | None, reason: str) -> LanguageDecided:
        return LanguageDecided(utt_at, action, self.current, candidate, reason, self.switches)

    def evidence(self, utt: UtteranceInfo) -> list[LanguageEvidence]:
        """The pieces of evidence behind a decision, for the event bus."""
        out: list[LanguageEvidence] = []
        words = pa_language.words(self._clean(utt))
        scores = sorted(utt.lid_scores, key=lambda x: -x[1])
        if scores:
            out.append(LanguageEvidence(utt.at_ms, "transcribe_lid", scores[0][0], scores[0][1], utt.speech_ms, words))
        hint = self._hint(utt)
        if hint:
            out.append(LanguageEvidence(utt.at_ms, "hint", hint, None, utt.speech_ms, words))
        explicit = self._explicit(utt.text)
        if explicit:
            out.append(LanguageEvidence(utt.at_ms, "explicit", explicit, None, utt.speech_ms, words))
        if utt.asr_confidence is not None and utt.asr_confidence < self.cfg.low_asr_confidence:
            out.append(LanguageEvidence(utt.at_ms, "asr_confidence", None, utt.asr_confidence, utt.speech_ms, words))
        return out

    # --- rules 1 to 9 ----------------------------------------------------------------------

    def observe(self, utt: UtteranceInfo) -> LanguageDecided:
        """One caller utterance -> the decision (rules 1 to 9)."""
        cfg, spoken = self.cfg, self.spoken
        self.turn += 1
        clean = self._clean(utt)
        words = pa_language.words(clean)
        hint = pa_language.detect(clean) if clean else None
        scores = sorted(utt.lid_scores, key=lambda x: -x[1])
        top, p = (scores[0] if scores else (None, 0.0))
        explicit = self._explicit(utt.text)
        pending, self._pending_ask = self._pending_ask, None
        prev, self._prev = self._prev, ((top, p, utt.speech_ms, words) if top else None)
        at = utt.at_ms

        # answer to our own question (rule 5): "ja" means the candidate
        if pending and not explicit:
            toks = _TOKEN.findall(utt.text.casefold())
            if toks and toks[0] in _YES and pending in spoken and pending != self.current:
                self._switch(pending, automatic=False)
                return self._result(at, LanguageAction.SWITCH, pending, "asked")
            if toks and toks[0] in _NO:
                return self._result(at, LanguageAction.KEEP, pending, "asked_declined")

        # rule 1: an explicit request switches at once, whatever the length
        if explicit:
            if explicit == self.current:
                return self._result(at, LanguageAction.KEEP, explicit, "explicit_same")
            if explicit in spoken:
                self._switch(explicit, automatic=False)
                return self._result(at, LanguageAction.SWITCH, explicit, "explicit")
            return self._result(at, LanguageAction.UNSUPPORTED, explicit, "explicit")

        # rule 8: a language we hear but cannot speak (fa, bs, id, so ...)
        long_enough = utt.speech_ms >= cfg.min_speech_ms and words >= cfg.min_content_words
        if top and top not in spoken and p >= cfg.strong_p and long_enough and hint in (None, top):
            return self._result(at, LanguageAction.UNSUPPORTED, top, "lid")
        # A weak LID top (the language is not among the stream's candidates, so Transcribe guesses one of them)
        # does not veto the text hint: live 2026-11-20 Persian on a +46 line scored sv 0.40 while the script
        # said fa, and the booking ran on text nobody could read.
        if hint and hint not in spoken and words >= cfg.min_content_words \
                and (top is None or top == hint or p < cfg.uncertain_low):
            return self._result(at, LanguageAction.UNSUPPORTED, hint, "hint")

        # rule 2: strong switch, rule 4 (short evidence alone never switches)
        if top and top != self.current and top in spoken and p >= cfg.strong_p and long_enough:
            agrees = hint == top or (hint is None and utt.asr_language == top)
            if agrees and self._can_auto_switch_to(top, utt.first):
                self._switch(top, automatic=True)
                return self._result(at, LanguageAction.SWITCH, top, "first_utterance" if utt.first else "strong")

        # rule 3: accumulated, two utterances in a row with the same top language
        if top and top != self.current and top in spoken and p >= cfg.accumulated_p and prev and prev[0] == top \
                and prev[1] >= cfg.accumulated_p and prev[2] + utt.speech_ms >= cfg.min_speech_ms \
                and prev[3] + words >= cfg.min_content_words and not (hint == self.current and p < cfg.strong_p) \
                and self._can_auto_switch_to(top, False):
            self._switch(top, automatic=True)
            return self._result(at, LanguageAction.SWITCH, top, "accumulated")

        # rule 5: uncertain, ask once in the current language plus the candidate
        second = scores[1][1] if len(scores) > 1 else 0.0
        rival = (top, p) if top and top != self.current and top in spoken else None
        if rival and cfg.uncertain_low <= p < cfg.strong_p and (p - second) < cfg.uncertain_margin \
                and (utt.speech_ms >= cfg.min_speech_ms or words >= cfg.min_content_words) \
                and rival[0] not in self._asked and not self.locked and not self._stay() \
                and hint in (None, rival[0]):
            self._asked.add(rival[0])
            self._pending_ask = rival[0]
            return self._result(at, LanguageAction.ASK, rival[0], "uncertain")

        # rule 7: poor audio, never switch
        low_asr = utt.asr_confidence is not None and utt.asr_confidence < cfg.low_asr_confidence
        if low_asr and (not scores or p < cfg.poor_audio_p):
            self._misses += 1
            if self._misses >= cfg.poor_audio_misses_before_callback:
                return self._result(at, LanguageAction.CALLBACK, None, "poor_audio")
            if self._misses >= cfg.poor_audio_misses_before_ask:
                return self._result(at, LanguageAction.ASK, None, "poor_audio")
            return self._result(at, LanguageAction.REPEAT, None, "poor_audio")
        self._misses = 0
        return self._result(at, LanguageAction.KEEP, None, "weak" if scores else "no_evidence")

    def wants_probe(self, utt: UtteranceInfo) -> tuple[str, ...] | None:
        """Candidates for a LID probe on this utterance, or None (no probe):
        the hint or an unnamed language request points elsewhere, or the ASR
        confidence is low, and no LID scores are present yet. A request that
        names its language needs no probe (rule 1 acts at once and the
        reopen re-transcribes)."""
        if utt.lid_scores:
            return None
        cfg = self.cfg
        # Away from the start language the caller may go back to it, and the fixed stream then writes words of the
        # wrong language with a middling confidence (live 2026-11-20: Swedish on en-GB came back as English
        # nonsense at 0.66, real English at 1.00, real Swedish over the line at 0.75 or more): probe below
        # AWAY_PROBE_CONFIDENCE there. In the start language the low ASR confidence alone decides.
        bar = cfg.low_asr_confidence if self.current == cfg.start_language else \
            max(cfg.low_asr_confidence, AWAY_PROBE_CONFIDENCE)
        probe = utt.asr_confidence is not None and utt.asr_confidence < bar
        if cfg.probe_on_hint and not explicit_targets(utt.text):
            hint = self._hint(utt)
            if hint and hint != self.current:
                probe = True
            if pa_language.asked_for_language(utt.text):
                probe = True
        if not probe:
            return None
        return candidates_for(self.caller, cfg, current=self.current)

    def follow(self, code: str, at_ms: int, reason: str = "model_tag") -> LanguageDecided | None:
        """The brain switched (call row): adopt it, count it, start the
        stay-turns hold. None when it already was the current language."""
        code = pa_language.normalise(code)
        if code == self.current:
            return None
        self._switch(code, automatic=True)
        return LanguageDecided(at_ms, LanguageAction.SWITCH, self.current, code, reason, self.switches)

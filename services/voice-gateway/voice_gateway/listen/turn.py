"""Semantic turn detection (end of the caller's turn). Owner: builder 'listen'.

Pure: TurnDetector consumes VAD events, partials, finals and clock ticks
and decides when the caller's turn is over. The silence threshold is
chosen per moment from the TAIL of the current transcript and the
assistant context (TurnConfig):

  tail kind (classify_tail)          threshold
  complete clause                    base_silence_ms
  complete clause answering the      answer_silence_ms (two words or more:
    assistant's question                "Nej, ... tack" goes on after one)
  short answer after a yes/no or     short_answer_silence_ms
    number question ("ja", "fyra")
  after an open question             open_question_silence_ms (minimum)
  a number that completes a time     base_silence_ms (the clause is done:
    ("klockan sju", "kl 19:00") or     "klockan sju" is said in one go),
    a final ending in . ? ! after a    after a number question
    single number word                 answer_silence_ms + 200
  filler ("ehm", "öh", "um", "uh")   filler_silence_ms (max_silence_ms right
                                       after a dangling word: "för ehm")
  dangling word ("för", "och",       dangling_silence_ms
    "till", "klockan", "and", "for")
  number being spoken ("noll sju     number_silence_ms
    tre", "18", "halv")
  never more than max_silence_ms; a TurnHold is published whenever the
  threshold rises above base_silence_ms (metrics, backchannel).

Turn end: silence since VadPause >= threshold -> wait up to
SttConfig.final_wait_ms for the SttFinal covering the speech (a final
already in: end at once; a partial whose last word ends within
TurnConfig.covered_slack_ms of the end of speech, on a complete clause of
two words or more (never a short answer: "Nej ... tack"), covers it too: Transcribe's final comes about 550 ms after
the last word, the partial about 100 ms) -> TurnEnd with the final text
(else the last partial, reason "silence"). max_utterance_ms of speech -> TurnEnd
"max_length". Speech under min_speech_ms with no words is noise: no turn.
Several finals in one turn are joined with a space. Word lists per
language live in TAILS (sv and en complete; de, fr, es, ar, pl, tr, it
best effort, reviewed later), matched on lowercase tokens.
"""

from __future__ import annotations

import re
from typing import Literal

from voice_gateway.config import SttConfig, TurnConfig
from voice_gateway.events import AssistantContext, SttFinal, SttPartial, TurnEnd, TurnHold, VadPause, VadResume, VadStart

TailKind = Literal["empty", "complete", "short_answer", "filler", "dangling", "number"]

# Words the recogniser writes for a hesitation sound when it follows a dangling word. Live on booqy-dev
# (2026-11-20) Transcribe sv-SE heard "boka bord för, ehm..." as "boka bord för en.": an "en" right after a
# preposition gets the filler wait (a real "för en", a party of one, waits only that half second longer).
# A lone letter is a hesitation fragment too (live: "ehm" came back as a final of its own, "N."), unless the
# language has it as a word.
ONE_LETTER_WORDS = frozenset("i å ö a o y e u".split())
# Only after a preposition: "klockan ett" stays a time (number).
HESITATION_LOOKALIKES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "sv": (frozenset({"en", "ett", "e"}), frozenset("för till på i med om från vid åt".split())),
}

TAILS: dict[str, dict[str, frozenset[str]]] = {
    "sv": {
        "filler": frozenset("eh ehm öh öhm äh ähm hm hmm mm alltså typ liksom asså".split()),
        "dangling": frozenset("för och till på i med att som men eller klockan kl vid om från den det en ett "
                              "är var vill ska skulle kan har hade jag vi".split()),
        "number": frozenset("noll ett en två tre fyra fem sex sju åtta nio tio elva tolv tretton fjorton femton "
                            "sexton sjutton arton nitton tjugo trettio fyrtio femtio halv kvart".split()),
        "short_answer": frozenset("ja nej japp nä absolut precis gärna okej visst stämmer".split()),
    },
    "en": {
        "filler": frozenset("uh um er erm hm hmm mm like".split()),
        "dangling": frozenset("for and to at on in with that but or the a an is was would will can i we "
                              "about from of".split()),
        "number": frozenset("zero oh one two three four five six seven eight nine ten eleven twelve thirteen "
                            "fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty "
                            "half quarter".split()),
        "short_answer": frozenset("yes no yeah yep nope sure right correct okay ok".split()),
    },
    "de": {
        "filler": frozenset("äh ähm ehm hm hmm öh also halt".split()),
        "dangling": frozenset("für und zu auf in mit dass aber oder um am an von der die das ein eine ist war will "
                              "möchte ich wir".split()),
        "number": frozenset("null eins zwei drei vier fünf sechs sieben acht neun zehn elf zwölf halb viertel".split()),
        "short_answer": frozenset("ja nein genau gerne okay stimmt klar".split()),
    },
    "fr": {
        "filler": frozenset("euh hum ben bah".split()),
        "dangling": frozenset("pour et à au de du des avec que mais ou le la les un une je nous vers".split()),
        "number": frozenset("zéro un deux trois quatre cinq six sept huit neuf dix onze douze".split()),
        "short_answer": frozenset("oui non d'accord exactement volontiers ok".split()),
    },
    "es": {
        "filler": frozenset("eh este pues bueno em".split()),
        "dangling": frozenset("para y a en con que pero o el la los las un una de del por".split()),
        "number": frozenset("cero uno dos tres cuatro cinco seis siete ocho nueve diez once doce".split()),
        "short_answer": frozenset("sí si no claro vale exacto ok".split()),
    },
    "it": {
        "filler": frozenset("ehm cioè allora beh".split()),
        "dangling": frozenset("per e a in con che ma o il la i le un una di del da".split()),
        "number": frozenset("zero uno due tre quattro cinque sei sette otto nove dieci undici dodici".split()),
        "short_answer": frozenset("sì si no certo esatto ok".split()),
    },
    "pl": {
        "filler": frozenset("yyy eee hmm no".split()),
        "dangling": frozenset("na i do w z że ale lub albo o dla".split()),
        "number": frozenset("zero jeden dwa trzy cztery pięć sześć siedem osiem dziewięć dziesięć".split()),
        "short_answer": frozenset("tak nie dobrze zgadza ok".split()),
    },
    "tr": {
        "filler": frozenset("ee şey yani hmm".split()),
        "dangling": frozenset("için ve ile bir bu şu da de ama veya saat".split()),
        "number": frozenset("sıfır bir iki üç dört beş altı yedi sekiz dokuz on".split()),
        "short_answer": frozenset("evet hayır tamam doğru olur".split()),
    },
    "ar": {
        "filler": frozenset("اه ايه يعني امم".split()),
        "dangling": frozenset("ل و في على مع ان لكن او الساعة".split()),
        "number": frozenset("صفر واحد اثنين ثلاثة اربعة خمسة ستة سبعة ثمانية تسعة عشرة".split()),
        "short_answer": frozenset("نعم لا تمام صحيح طيب".split()),
    },
}

_TOKEN = re.compile(r"[^\W_]+(?:['\u2019-][^\W_]+)*")
# A number right after these is a clock time said in one go ("klockan sju", "kl 19"): the clause is complete.
TIME_MARKERS = frozenset("klockan kl halv kvart at um om à alle saat godzina godzinę".split())
_SENTENCE_END = re.compile(r"[.?!]\s*$")
_HESITATION = re.compile(r"^(?:e+h*m*|ä+h*m*|ö+h*m*|u+[hm]+|h+m+|m{2,}|er+m*|a+h+m+)$")
IDENTIFY_FINAL_WAIT_MS = 1500  # the most a turn waits for the identify stream's final (first utterance)
# Live 2026-11-20: that final came 1.8 s after the end of speech (550 + 1250), its partials 1.5 to 2.5 s behind the audio. At 900 ms the
# turn ended on "jag vill boka ett bord" / "för 8 personer på lördag" and the rest of the sentence ("klockan sju") was lost.
EMPTY_GRACE_MS = 1500        # speech heard but no words yet: Transcribe may lag, wait this long before dropping
# A hesitation sound Transcribe leaves out. Live on booqy-dev (2026-11-20) "boka bord för, ehm... fyra" came back as
# "boka bord för." with nothing for the "ehm": the turn ended 1.4 s after it, inside the caller's pause. A voiced
# burst after a pause that brings no word (every word the recogniser reported ends before the burst began) and is
# short is a hesitation: it gets the filler wait, after a dangling word the longest wait.
WORDLESS_BURST_MAX_MS = 900  # longer voiced stretches without words are Transcribe lag, not an "ehm"
WORDLESS_SLACK_MS = 60       # word end times and VAD onsets disagree by a frame or two (at least)


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(str(text or "").lower())


def classify_tail(text: str, language: str,
                  asked: Literal["yes_no", "open", "number", None] = None) -> TailKind:
    """The kind of the transcript's end (digits count as a number)."""
    toks = _tokens(text)
    if not toks:
        return "empty"
    sets = TAILS.get(language) or {}
    filler, dangling = sets.get("filler", frozenset()), sets.get("dangling", frozenset())
    number, short = sets.get("number", frozenset()), sets.get("short_answer", frozenset())
    last = toks[-1]
    if last in filler or _HESITATION.match(last):
        return "filler"
    if len(last) == 1 and last.isalpha() and last not in ONE_LETTER_WORDS \
            and not any(last in words for words in sets.values()):
        return "filler"
    sounds, after = HESITATION_LOOKALIKES.get(language, (frozenset(), frozenset()))
    if len(toks) > 1 and last in sounds and toks[-2] in after:
        return "filler"
    has_digit = lambda t: any(c.isdigit() for c in t)
    if asked == "yes_no" and len(toks) <= 4 and all(t in short for t in toks):
        return "short_answer"
    if asked == "number" and len(toks) <= 2 and all(t in number or has_digit(t) for t in toks):
        return "short_answer"
    if has_digit(last):
        return "number"
    if last in number:
        if last in dangling and not (len(toks) > 1 and (toks[-2] in number or has_digit(toks[-2]))):
            return "dangling"
        return "number"
    if last in dangling:
        return "dangling"
    return "complete"


class TurnDetector:
    def __init__(self, cfg: TurnConfig, stt: SttConfig, *, language: str = "sv") -> None:
        self.cfg, self.stt, self.language = cfg, stt, language
        self.ctx: AssistantContext | None = None
        # False while the live stream identifies the language (the first utterance): its final carries the
        # language scores, so the turn waits for it instead of ending on a partial (the Listener sets this).
        self.partial_ends = True
        self.reset()

    # --- state -------------------------------------------------------------------------------

    def reset(self) -> None:
        """After a TurnEnd (or a barge-in turn handled elsewhere)."""
        self._state = "idle"                    # idle | speaking | paused | ended
        self._clear_text()
        self._speech_start = self._eos = 0
        self._pause_start: int | None = None
        self._speech_ms = 0
        self._last_hold: tuple[str, int] | None = None
        self._ignore_before = -1
        self._burst: tuple[int, int] | None = None   # (start, end) of the last voiced stretch after a pause
        self._sticky_burst = False                   # this pause was judged a hesitation (wordless burst)

    def _clear_text(self) -> None:
        self._finals: list[str] = []
        self._confs: list[float] = []
        self._partial = ""
        self._covered = False
        self._last_word_ms = -1                 # audio time the last reported word ends
        self._partial_word_ms = -1              # the same for the latest partial only

    def set_context(self, ctx: AssistantContext) -> None:
        self.ctx = ctx

    def set_language(self, language: str) -> None:
        self.language = language

    def text(self) -> str:
        """What the caller said so far in this turn (finals and the latest partial)."""
        return self._text() if self._state in ("speaking", "paused") else ""

    def _text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._finals + ([self._partial] if self._partial else []))).strip()

    # --- threshold ---------------------------------------------------------------------------

    def _asked(self) -> Literal["yes_no", "open", "number", None]:
        ctx = self.ctx
        if ctx is None:
            return None
        if ctx.asked_question is None and ctx.awaiting_confirmation:
            return "yes_no"
        return ctx.asked_question

    def _kind(self, text: str, asked) -> TailKind:
        """classify_tail, with a number that completes a time (or a final that ends the sentence after one
        number word) counted as a complete clause."""
        kind = classify_tail(text, self.language, asked)
        if kind != "number":
            return kind
        toks = _tokens(text)
        is_num = lambda t: any(c.isdigit() for c in t) or t in (TAILS.get(self.language) or {}).get("number", ())
        if len(toks) > 1 and toks[-2] in TIME_MARKERS:
            return "complete"
        if len(toks) > 1 and is_num(toks[-2]):
            return kind                                  # digits in a row: a phone number being read
        if self._covered and self._finals and _SENTENCE_END.search(self._finals[-1]):
            return "complete"
        return kind

    def _after_dangling(self, text: str) -> bool:
        """The hesitation sounds at the end follow a dangling word ("för ehm", "for um")."""
        sets = TAILS.get(self.language) or {}
        filler, dangling = sets.get("filler", frozenset()), sets.get("dangling", frozenset())
        toks = _tokens(text)
        words = frozenset().union(*sets.values()) if sets else frozenset()
        # a lone letter is a hesitation fragment too (live 2026-11-20: "ehm" came back as "N.")
        while toks and (toks[-1] in filler or _HESITATION.match(toks[-1]) or (
                len(toks[-1]) == 1 and toks[-1].isalpha() and toks[-1] not in ONE_LETTER_WORDS
                and toks[-1] not in words)):
            toks.pop()
        return bool(toks) and toks[-1] in dangling

    def _covering_partial(self) -> bool:
        """The latest partial already holds the caller's last words (its last word ends near the end of speech)."""
        return bool(self._partial) and self._partial_word_ms >= self._eos - self.cfg.covered_slack_ms

    def _decide(self) -> tuple[int, str | None]:
        cfg, asked = self.cfg, self._asked()
        text = self._text()
        kind = self._kind(text, asked)
        thr, reason = cfg.base_silence_ms, None
        if self._sticky_burst or self._wordless_burst():
            # Sticky for the pause: live voice-A the final of "för" arrived inside the pause with its end time
            # stretched over the "ehm", the burst no longer looked wordless and the wait fell back to 1400 ms.
            # On the first utterance the identify stream's partials lag ("boka bord för" arrives as "jag skulle vilja bok"
            # inside the pause, live voice-A 2026-11-20): the tail of the text says nothing yet, the burst stays a hesitation.
            lagging = self._sticky_burst and not self.partial_ends
            longest = kind in ("dangling", "filler") or (self._sticky_burst and (self._after_dangling(text) or lagging))
            self._sticky_burst = self._state == "paused" and longest      # only "för ... ehm" stays held
            return (cfg.max_silence_ms if longest else min(cfg.filler_silence_ms, cfg.max_silence_ms)), "filler"
        if kind == "short_answer":
            thr = cfg.short_answer_silence_ms
        elif kind == "filler":
            thr, reason = cfg.filler_silence_ms, "filler"
            if self._after_dangling(text):
                thr = cfg.max_silence_ms              # "för, ehm...": the caller is looking for the word
        elif kind == "dangling":
            thr, reason = cfg.dangling_silence_ms, "dangling"
        elif kind == "number":
            thr, reason = cfg.number_silence_ms, "number"
        words = len(_tokens(text))
        if kind == "complete" and asked is not None and words >= 2:
            thr = min(thr, cfg.answer_silence_ms)        # the caller answered the question (one word: "Nej ... tack")
        elif kind == "number" and asked == "number":
            thr = cfg.answer_silence_ms + 200            # a number given to a number question
        elif asked == "open" and thr < cfg.open_question_silence_ms:
            thr, reason = cfg.open_question_silence_ms, "open_question"
        return min(thr, cfg.max_silence_ms), (reason if thr > cfg.base_silence_ms else None)

    def ends_on_partial(self) -> bool:
        """The turn may end on the latest partial (no wait for the final)."""
        # Not a short answer: "Nej ... tack, det går bra" goes on after a comma pause (fake voice-E), the final says.
        text = self._text()
        return self.partial_ends and self._covering_partial() and len(_tokens(text)) >= 2 \
            and self._kind(text, self._asked()) == "complete"

    def _wordless_burst(self) -> bool:
        """The caller's last voiced stretch (after a pause, short) brought no word: a hesitation sound."""
        if self._state != "paused" or self._burst is None or not self._text():
            return False
        start, end = self._burst
        # Transcribe stretches a word's end time towards the next sound (live: "för" ran into the "ehm"), so
        # the burst counts as wordless while the words end before its middle.
        return end - start <= WORDLESS_BURST_MAX_MS and self._last_word_ms < start + max(WORDLESS_SLACK_MS,
                                                                                        (end - start) // 2)

    def threshold_ms(self) -> int:
        """The silence threshold that applies right now."""
        if self._state == "idle":
            return self.cfg.base_silence_ms
        return self._decide()[0]

    def _hold(self, at_ms: int) -> list[TurnHold]:
        thr, reason = self._decide()
        if reason is None or self._last_hold == (reason, thr):
            return []
        self._last_hold = (reason, thr)
        return [TurnHold(at_ms, reason, thr)]            # type: ignore[arg-type]

    # --- inputs ------------------------------------------------------------------------------

    def on_vad(self, ev: VadStart | VadPause | VadResume) -> list[TurnHold | TurnEnd]:
        if isinstance(ev, VadStart):
            if self._state in ("idle", "ended"):
                self._begin(ev.speech_start_ms, ev.at_ms)
            return []
        if isinstance(ev, VadResume):
            if self._state == "paused":
                self._burst = (self._pause_start + ev.pause_ms, ev.at_ms)    # end set at the next pause
                self._state, self._pause_start, self._last_hold = "speaking", None, None
                self._sticky_burst = False
            elif self._state in ("idle", "ended"):
                self._begin(ev.at_ms, ev.at_ms)
            return []
        if isinstance(ev, VadPause) and self._state == "speaking":
            self._state, self._pause_start = "paused", ev.silence_start_ms
            self._eos, self._speech_ms = ev.silence_start_ms, ev.speech_ms
            if self._burst is not None:
                self._burst = (self._burst[0], ev.silence_start_ms)
            if ev.speech_ms < self.cfg.min_speech_ms and not self._text():
                self._state = "idle"                    # a click or a breath, not a turn
                return []
            return list(self._hold(ev.at_ms))
        return []

    def _begin(self, speech_start: int, now: int) -> None:
        if self._state == "ended":
            self._ignore_before = self._eos + 50       # late finals of the turn that just ended
        self._clear_text()
        self._state, self._speech_start, self._eos = "speaking", speech_start, now
        self._pause_start, self._last_hold, self._speech_ms, self._burst = None, None, 0, None
        self._sticky_burst = False

    def on_partial(self, ev: SttPartial) -> list[TurnHold | TurnEnd]:
        if self._state in ("idle", "ended"):
            return []
        self._partial, self._covered = ev.text.strip(), False
        self._partial_word_ms = ev.at_ms if ev.last_word_ms is None else ev.last_word_ms
        self._last_word_ms = max(self._last_word_ms, self._partial_word_ms)
        return list(self._hold(ev.at_ms)) if self._state == "paused" else []

    def on_final(self, ev: SttFinal) -> list[TurnHold | TurnEnd]:
        if self._state in ("idle", "ended") or ev.end_ms <= self._ignore_before:
            return []
        if ev.text.strip():
            self._finals.append(ev.text.strip())
        if ev.confidence is not None:
            self._confs.append(ev.confidence)
        self._last_word_ms = max(self._last_word_ms, ev.end_ms if ev.last_word_ms is None else ev.last_word_ms)
        self._partial, self._covered = "", True
        if self._state != "paused":
            return []
        out: list[TurnHold | TurnEnd] = list(self._hold(ev.at_ms))
        thr = self._decide()[0]
        if ev.at_ms - self._pause_start >= thr and self._text():
            return out + self._end("final_after_silence", ev.at_ms)
        return out

    def on_tick(self, now_ms: int) -> list[TurnHold | TurnEnd]:
        """Called every frame (20 ms); fires TurnEnd when due."""
        if self._state == "speaking":
            if now_ms - self._speech_start >= self.cfg.max_utterance_ms:
                self._eos = now_ms
                return self._end("max_length", now_ms)
            return []
        if self._state != "paused":
            return []
        out = list(self._hold(now_ms))
        thr = self._decide()[0]
        silence = now_ms - self._pause_start
        if silence < thr:
            return out
        if self._covered or self.ends_on_partial():
            return out + (self._end("silence", now_ms) if self._text() else self._drop())
        waited = silence - thr
        if self._text():
            # the identify stream's final carries the language scores and, live, the words a lagging partial left
            # out (lang-G: "klockan sju" was lost): wait longer for it on the first utterance
            final_wait = self.stt.final_wait_ms if self.partial_ends else max(self.stt.final_wait_ms,
                                                                              IDENTIFY_FINAL_WAIT_MS)
            return out + (self._end("silence", now_ms) if waited >= final_wait else [])
        return out + (self._drop() if waited >= max(self.stt.final_wait_ms, EMPTY_GRACE_MS) else [])

    def _drop(self) -> list:
        self._state = "idle"
        self._clear_text()
        return []

    def _end(self, reason: str, now_ms: int) -> list[TurnEnd]:
        text = self._text()
        if not text:
            return self._drop()
        conf = sum(self._confs) / len(self._confs) if self._confs else None
        end = TurnEnd(now_ms, text, self.language, self._speech_start, self._eos, reason, conf)   # type: ignore[arg-type]
        self._state = "ended"
        self._clear_text()
        return [end]

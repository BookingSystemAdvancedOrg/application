"""BackchannelController: when a short acknowledgement fits. Owner: builder 'converse'.

Pure (no I/O, injected RNG). Consumes events, returns BackchannelRequests;
the async shell (speak.speaker.Speaker) forwards them to the mixer.

Fires only when ALL hold (BackchannelConfig):
  * the call is older than min_call_ms and the orchestrator state is LISTENING
    (the caller holds the turn; never in GREETING, THINKING or SPEAKING);
  * the caller has spoken at least min_turn_speech_ms in this turn;
  * the caller is in a pause (VadPause) that has lasted pause_min_ms and
    not yet pause_max_ms (on_tick decides inside the window; a VadResume
    cancels), and the TurnDetector has not ended the turn; a TurnHold in
    this pause makes it a better moment (hesitation), not a requirement;
  * the recogniser has caught up: a partial arrived after the pause
    began, so the tail below is the caller's real last words (partials
    trail the audio by 100 ms and come every 480 ms; at a short comma pause
    the tail is still a word or two behind, and that pause gets nothing);
  * a meaningful clause since the last backchannel: at least
    min_words_since_last new words in the partials, and the tail is not a
    number (turn.classify_tail != "number") when no_number_tail;
  * not within cooldown_ms of the last one, at most max_per_turn per turn;
  * no assistant audio playing or queued, and the assistant stopped more
    than after_assistant_ms ago;
  * no read-back awaiting the caller's yes (AssistantContext
    .awaiting_confirmation) when no_during_confirmation;
  * a token exists for the caller's language (tokens.TOKENS).
At most one request per pause. Each request is counted for metrics
(time to first backchannel from VadStart).

It must end before the caller speaks again (an "mm" over the caller is
the one thing that sounds worse than none). The controller learns this
caller's pauses: every pause the caller resumed from (at least
PAUSE_SAMPLE_MIN_MS; shorter gaps, inside words or at a comma, could
host no clip anyway) is kept, and the
expected pause is the median of the last ones plus a prior
(PAUSE_PRIOR_MS). A token is only chosen when its clip (duration from
`duration(language, token)`) ends FIT_MARGIN_MS before that expected
pause is over, so a caller with short pauses gets none, and a shorter
token wins when time is tight.

What makes it sound like a person and not a metronome:
  * the moment inside the pause is drawn per pause (pause_min_ms plus up
    to 150 ms of jitter), so two backchannels never land on the same beat;
  * the first one of a turn always fires when everything fits, a second
    one only with SECOND_CHANCE (people acknowledge a long story once or
    twice, not on every clause);
  * the token follows the moment: in a hesitation ("för, ehm...", a
    dangling word, a TurnHold filler or dangling) only a continuer ("mm"),
    which says "go on"; after a complete clause any token, weighted to
    "mm"; never the same token twice in a row, recent tokens weigh less;
  * never after a question from the caller (they wait for an answer, an
    "mm" there sounds like not listening), never after a number (it could
    be heard as confirming it), never at a comma ("fyra, nej förlåt,":
    the caller is mid-clause and resumes within a few hundred ms), never
    while the caller confirms a read-back.
The assistant's own queue is the shell's business: Speaker calls on_tick
only while the mixer is idle, and the mixer re-checks at play time;
note_dropped() undoes the bookkeeping when the mixer refused.
"""

from __future__ import annotations

import random
import statistics
from collections import deque
from dataclasses import dataclass

from voice_gateway.backchannel import tokens as tk
from voice_gateway.config import BackchannelConfig
from voice_gateway.events import (AssistantContext, BackchannelRequest, CallState, LanguageDecided, PlaybackFinished,
                                  PlaybackFlushed, PlaybackStarted, SttPartial, StateChanged, TurnEnd, TurnHold,
                                  VadPause, VadResume, VadStart)

SECOND_CHANCE = 0.6         # probability that a second backchannel in one turn is used at all
JITTER_MS = 150             # extra wait drawn per pause on top of pause_min_ms
PAUSE_PRIOR_MS = 500        # expected caller pause before this caller showed any (a sentence boundary)
PAUSE_SAMPLE_MIN_MS = 300   # shorter resumed pauses (inside words, at commas) can host no clip: not learned
PAUSE_SAMPLES = 6           # the last this many resumed pauses count
FIT_MARGIN_MS = 30          # the clip ends at least this long before the expected resume
TICK_MS = 20                # the shell ticks every frame: the moment can land this much after fire_after_ms
HESITATION = frozenset({"filler", "dangling"})
_QUESTION_END = ("?", "？", "؟")
_COMMA_END = (",", "،", ";")   # the recogniser heard a clause go on: a comma pause is too short for a clip
_NUMBER_WORDS = frozenset(
    "noll ett en två tre fyra fem sex sju åtta nio tio elva tolv tretton fjorton femton sexton sjutton arton "
    "nitton tjugo trettio fyrtio femtio halv kvart zero oh one two three four five six seven eight nine ten "
    "eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty half "
    "quarter".split())
_FILLER_WORDS = frozenset("eh ehm öh öhm äh ähm hm hmm uh um er erm".split())
_DANGLING_WORDS = frozenset("för och till på i med att som men eller klockan kl vid om från for and to at on in "
                            "with that but or the about from of".split())


def tail_kind(text: str, language: str, asked=None) -> str:
    """turn.classify_tail when builder 'listen' has it, else a small local
    rule (digits and number words, fillers, dangling words)."""
    try:
        from voice_gateway.listen.turn import classify_tail
        return classify_tail(text, language, asked)
    except Exception:       # NotImplementedError while listen is being built, or any bug there
        pass
    words = [w.strip(".,;:!?\"'()").lower() for w in str(text or "").split()]
    words = [w for w in words if w]
    if not words:
        return "empty"
    last = words[-1]
    if any(c.isdigit() for c in last) or last in _NUMBER_WORDS:
        return "number"
    if last in _FILLER_WORDS:
        return "filler"
    if last in _DANGLING_WORDS:
        return "dangling"
    return "complete"


@dataclass
class _Pause:
    silence_start_ms: int
    speech_ms: int
    fire_after_ms: int      # earliest moment, as ms after silence_start_ms
    allowed: bool           # the random gate for a second backchannel
    done: bool = False


@dataclass(frozen=True)
class _Snapshot:
    last_ms: int | None
    last_token: str | None
    words_at_last: int
    first_ms: int | None


class BackchannelController:
    def __init__(self, cfg: BackchannelConfig, *, rng: random.Random | None = None, language: str = "sv",
                 available=None, duration=None) -> None:
        """`available(language, token) -> bool` (optional): only tokens with a
        ready clip are chosen (ClipCache.backchannel is not None).
        `duration(language, token) -> int | None` (optional): the clip's
        length in ms; without it a token is assumed to fit."""
        self.cfg, self.rng, self.language = cfg, rng or random.Random(), language
        self.available = available
        self.duration = duration
        self.pauses: deque[int] = deque(maxlen=PAUSE_SAMPLES)   # this caller's resumed pauses (ms)
        self.state: CallState | None = None
        self.awaiting_confirmation = False
        self.asked_question = None
        self.caller_speaking = False
        self.turn_start_ms: int | None = None
        self.pause: _Pause | None = None
        self.hold: str | None = None
        self._segments: dict[str, int] = {}
        self._words_at_last = 0
        self.tail_text = ""
        self.last_partial_ms: int | None = None
        self.last_ms: int | None = None
        self.last_token: str | None = None
        self.recent: deque[str] = deque(maxlen=3)
        self.count_this_turn = 0
        self._assistant_items: set[str] = set()
        self.assistant_end_ms: int | None = None
        self.own_playing = False
        self.first_ms: int | None = None        # this turn: ms from the turn's VadStart to the first request
        self.sent = 0                           # requests the mixer accepted (metrics)
        self.dropped = 0
        self._undo: _Snapshot | None = None

    # -- inputs -------------------------------------------------------------------------------
    def on_event(self, event) -> list[BackchannelRequest]:
        """Any bus event (VadStart, VadPause, VadResume, SttPartial, TurnHold,
        TurnEnd, PlaybackStarted, PlaybackFinished, StateChanged,
        AssistantContext, LanguageDecided); unknown types are ignored."""
        if isinstance(event, VadStart):
            self.caller_speaking, self.pause, self.hold = True, None, None
            if self.turn_start_ms is None:
                self.turn_start_ms = event.speech_start_ms
        elif isinstance(event, VadResume):
            if event.pause_ms >= PAUSE_SAMPLE_MIN_MS:
                self.pauses.append(event.pause_ms)
            self.caller_speaking, self.pause, self.hold = True, None, None
        elif isinstance(event, VadPause):
            self.caller_speaking = False
            if self.turn_start_ms is None:
                self.turn_start_ms = event.silence_start_ms - event.speech_ms
            allowed = self.count_this_turn == 0 or self.rng.random() < SECOND_CHANCE
            room = (self.expected_pause_ms() - FIT_MARGIN_MS - TICK_MS - self._shortest_ms()
                    - self.cfg.pause_min_ms)
            jitter = max(0, min(JITTER_MS, self.cfg.pause_max_ms - self.cfg.pause_min_ms - 100, room))
            self.pause = _Pause(event.silence_start_ms, event.speech_ms,
                                self.cfg.pause_min_ms + self.rng.randint(0, jitter), allowed)
        elif isinstance(event, SttPartial):
            self.last_partial_ms = event.at_ms
            words = len(event.text.split())
            self._segments[event.segment_id] = max(words, self._segments.get(event.segment_id, 0))
            self.tail_text = event.text
        elif isinstance(event, TurnHold):
            self.hold = event.reason
            return self._maybe_fire(event.at_ms)
        elif isinstance(event, TurnEnd):
            self._new_turn()
        elif isinstance(event, PlaybackStarted):
            if event.kind == "backchannel":
                self.own_playing = True
            else:
                self._assistant_items.add(event.item_id)
        elif isinstance(event, PlaybackFinished):
            if event.kind == "backchannel":
                self.own_playing = False
            else:
                self._assistant_items.discard(event.item_id)
                self.assistant_end_ms = event.at_ms
        elif isinstance(event, PlaybackFlushed):
            self._assistant_items.clear()
            self.own_playing = False
            self.assistant_end_ms = event.at_ms
        elif isinstance(event, StateChanged):
            self.state = event.new
            if event.new != CallState.LISTENING:
                self.pause = None
        elif isinstance(event, AssistantContext):
            self.awaiting_confirmation = event.awaiting_confirmation
            self.asked_question = event.asked_question
            self.language = event.language or self.language
        elif isinstance(event, LanguageDecided):
            self.language = event.current or self.language
        return []

    def on_tick(self, now_ms: int) -> list[BackchannelRequest]:
        return self._maybe_fire(now_ms)

    def note_dropped(self, req: BackchannelRequest) -> None:
        """The mixer refused `req` at play time: it does not count."""
        if self._undo is None:
            return
        snap, self._undo = self._undo, None
        self.last_ms, self.last_token, self._words_at_last, self.first_ms = (snap.last_ms, snap.last_token,
                                                                             snap.words_at_last, snap.first_ms)
        self.count_this_turn = max(0, self.count_this_turn - 1)
        if self.recent and self.recent[-1] == req.token:
            self.recent.pop()
        self.sent = max(0, self.sent - 1)
        self.dropped += 1

    # -- the decision -------------------------------------------------------------------------
    def expected_pause_ms(self) -> int:
        """How long this caller's pause is expected to last (see the module doc)."""
        return int(statistics.median([PAUSE_PRIOR_MS, *self.pauses]))

    def _clip_ms(self, token: str) -> int:
        if self.duration is None:
            return 0
        try:
            return int(self.duration(tk.base_language(self.language), token) or 0)
        except Exception:   # noqa: BLE001  (a missing clip is the availability check's business)
            return 0

    def _shortest_ms(self) -> int:
        lang = tk.base_language(self.language)
        rows = [t for t, _, _ in tk.TOKENS.get(lang, ())
                if self.available is None or self.available(lang, t)]
        return min((self._clip_ms(t) for t in rows), default=0)

    def _fits(self, elapsed_ms: int):
        """available() limited to tokens whose clip ends before the expected resume."""
        budget = self.expected_pause_ms() - FIT_MARGIN_MS - elapsed_ms

        def ok(language: str, token: str) -> bool:
            if self.available is not None and not self.available(language, token):
                return False
            return self._clip_ms(token) <= budget
        return ok

    @property
    def words(self) -> int:
        return sum(self._segments.values())

    def blocked(self, now_ms: int) -> str | None:
        """Why no backchannel may start now (None: it may). For tests and debugging."""
        cfg, p = self.cfg, self.pause
        if not cfg.enabled:
            return "disabled"
        if self.state != CallState.LISTENING:
            return "state"
        if now_ms < cfg.min_call_ms:
            return "call_start"
        if p is None or p.done or self.caller_speaking:
            return "no_pause"
        elapsed = now_ms - p.silence_start_ms
        if elapsed > cfg.pause_max_ms:
            return "pause_over"
        if elapsed < p.fire_after_ms:
            return "too_early"
        if not p.allowed:
            return "sparse"
        if p.speech_ms < cfg.min_turn_speech_ms:
            return "short_turn"
        if self.count_this_turn >= cfg.max_per_turn:
            return "max_per_turn"
        if self.last_ms is not None and now_ms - self.last_ms < cfg.cooldown_ms:
            return "cooldown"
        if self.words - self._words_at_last < cfg.min_words_since_last:
            return "few_words"
        if cfg.no_during_confirmation and self.awaiting_confirmation:
            return "confirmation"
        if self._assistant_items or self.own_playing:
            return "audio"
        if self.assistant_end_ms is not None and now_ms - self.assistant_end_ms < cfg.after_assistant_ms:
            return "after_assistant"
        if self.last_partial_ms is None or self.last_partial_ms < p.silence_start_ms:
            return "stale_text"     # the recogniser has not caught up with the pause: the tail is not known yet
        text = self.tail_text.rstrip()
        if text.endswith(_QUESTION_END):
            return "question"
        if text.endswith(_COMMA_END) and self.hold not in HESITATION:
            return "mid_clause"
        if cfg.no_number_tail and (self.hold == "number" or self._tail() == "number" or self._number_before_tail()):
            return "number"
        return None

    def _number_before_tail(self) -> bool:
        """The word before the last is a number ("för fyra, nej"): the newest
        partial word is the least stable one, and the caller may be correcting
        the number, so an "mm" could still be heard as confirming it."""
        words = self.tail_text.split()
        return len(words) > 1 and tail_kind(" ".join(words[:-1]), tk.base_language(self.language),
                                            self.asked_question) == "number"

    def _tail(self) -> str:
        return tail_kind(self.tail_text, tk.base_language(self.language), self.asked_question)

    def _maybe_fire(self, now_ms: int) -> list[BackchannelRequest]:
        p = self.pause
        why = self.blocked(now_ms)
        if why is not None:
            if p is not None and why in ("pause_over", "sparse", "short_turn", "max_per_turn", "few_words",
                                         "confirmation", "question", "number", "mid_clause"):
                p.done = True       # nothing in this pause can change that
            return []
        hesitation = self.hold in HESITATION or self._tail() in HESITATION
        token = tk.pick_token(self.language, self.rng, self.last_token,
                              style="continuer" if hesitation else None,
                              available=self._fits(now_ms - p.silence_start_ms), recent=tuple(self.recent))
        p.done = True               # at most one per pause, fired or not
        if token is None:
            return []
        self._undo = _Snapshot(self.last_ms, self.last_token, self._words_at_last, self.first_ms)
        if self.count_this_turn == 0 and self.turn_start_ms is not None:
            self.first_ms = now_ms - self.turn_start_ms
        self.last_ms, self.last_token = now_ms, token
        self.recent.append(token)
        self._words_at_last = self.words
        self.count_this_turn += 1
        self.sent += 1
        return [BackchannelRequest(now_ms, tk.base_language(self.language), token,
                                   "hesitation" if hesitation else "clause_pause")]

    def _new_turn(self) -> None:
        self.pause, self.hold, self.caller_speaking = None, None, False
        self.turn_start_ms, self.first_ms = None, None
        self._segments.clear()
        self._words_at_last, self.count_this_turn, self.tail_text = 0, 0, ""
        self._undo = None

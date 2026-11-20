"""Per-turn and per-call metrics from bus events. Owner: builder 'brain'.

MetricsRecorder hangs off EventBus.listen (synchronous, in publish order)
and fills TurnMetrics per caller turn and CallMetrics per call; when a
turn closes (the next caller turn ends, or the call ends) it emits EMF
lines via log.metric with the dimension `language` only. Never text,
never numbers, never ids in dimensions (log.event carries the hashed call
tag).

A "turn" is one caller utterance (opened by the VadStart that began it,
closed by its TurnEnd) and the assistant's reply to it. Barge-in numbers
belong to the turn whose REPLY was interrupted, not to the caller turn the
interruption opens.

Definitions (all ms on the call clock; TurnMetrics keeps absolute times,
the derived numbers are in `summary()`):
  eos_to_first_audio     TurnEnd.eos_ms -> first PlaybackStarted (kind assistant,
                         fixed or opener) after it; THE latency number (target p50
                         under 1500): what the caller hears first, the opener
                         ("Mm.", speak/openers.py) when one played
  eos_to_reply_audio     TurnEnd.eos_ms -> first assistant or fixed PlaybackStarted
                         (the reply itself, without the opener)
  opener_played          bool
  turn_end_to_first_audio TurnEnd.at_ms -> same (excludes the turn detector wait)
  turn_detect            TurnEnd.at_ms - TurnEnd.eos_ms (what endpointing costs)
  first_sentence         TurnEnd.at_ms -> first AgentSentence
  tts_first_audio        first AgentSentence -> first PlaybackStarted
  first_backchannel      VadStart.speech_start_ms -> first backchannel PlaybackStarted
                         in the turn (None when none played; stored as a duration)
  backchannel_overlaps   backchannel playing while VAD speech (caller) began;
                         target 0
  barge_in_stop          PlaybackFlushed.stop_ms (BargeIn -> line cleared);
                         target under 300
  barge_in_detect        BargeIn.at_ms - BargeIn.speech_start_ms
  filler_played          bool; rounds, tools, cut, error from AgentTurnDone
Per call: language detections (LanguageDecided with action != keep, by
reason), switches, first-utterance LID result, booking outcome
(AgentTurnDone.outcome last seen), barge-ins, turns, call ms, end reason.
`summary()` returns plain dicts for the harness assertions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from voice_gateway import log
from voice_gateway.events import (AgentSentence, AgentTurnDone, BargeIn, CallEnded, EventBus, LanguageAction,
                                  LanguageDecided, PlaybackFinished, PlaybackFlushed, PlaybackStarted, TurnEnd,
                                  TurnHold, VadStart)


@dataclass
class TurnMetrics:
    turn: int
    language: str = "sv"
    speech_start_ms: int | None = None
    eos_ms: int | None = None
    turn_end_ms: int | None = None
    first_sentence_ms: int | None = None
    first_audio_ms: int | None = None
    first_reply_ms: int | None = None
    opener_played: bool = False
    first_backchannel_ms: int | None = None
    backchannels: int = 0
    backchannel_overlaps: int = 0
    filler_played: bool = False
    holds: int = 0
    barge_in_detect_ms: int | None = None
    barge_in_stop_ms: int | None = None
    rounds: int = 0
    tools: tuple[str, ...] = ()
    cut: bool = False
    error: bool = False

    @property
    def eos_to_first_audio(self) -> int | None:
        if self.eos_ms is None or self.first_audio_ms is None:
            return None
        return self.first_audio_ms - self.eos_ms

    @property
    def eos_to_reply_audio(self) -> int | None:
        if self.eos_ms is None or self.first_reply_ms is None:
            return None
        return self.first_reply_ms - self.eos_ms

    @property
    def turn_end_to_first_audio(self) -> int | None:
        if self.turn_end_ms is None or self.first_audio_ms is None:
            return None
        return self.first_audio_ms - self.turn_end_ms

    @property
    def turn_detect(self) -> int | None:
        if self.turn_end_ms is None or self.eos_ms is None:
            return None
        return self.turn_end_ms - self.eos_ms

    @property
    def first_sentence(self) -> int | None:
        if self.turn_end_ms is None or self.first_sentence_ms is None:
            return None
        return self.first_sentence_ms - self.turn_end_ms

    @property
    def tts_first_audio(self) -> int | None:
        if self.first_sentence_ms is None or self.first_reply_ms is None:
            return None
        return self.first_reply_ms - self.first_sentence_ms

    def as_dict(self) -> dict:
        return {"turn": self.turn, "language": self.language, "eos_to_first_audio": self.eos_to_first_audio,
                "eos_to_reply_audio": self.eos_to_reply_audio, "opener_played": self.opener_played,
                "turn_end_to_first_audio": self.turn_end_to_first_audio, "turn_detect": self.turn_detect,
                "first_sentence": self.first_sentence, "tts_first_audio": self.tts_first_audio,
                "first_backchannel": self.first_backchannel_ms, "backchannels": self.backchannels,
                "backchannel_overlaps": self.backchannel_overlaps, "filler_played": self.filler_played,
                "holds": self.holds, "barge_in_detect": self.barge_in_detect_ms,
                "barge_in_stop": self.barge_in_stop_ms, "rounds": self.rounds, "tools": list(self.tools),
                "cut": self.cut, "error": self.error}


@dataclass
class CallMetrics:
    turns: list[TurnMetrics] = field(default_factory=list)
    language_detections: list[tuple[str, str, str]] = field(default_factory=list)   # (action, code, reason)
    language_switches: int = 0
    first_language: str | None = None
    outcome: str | None = None
    barge_ins: int = 0
    end_reason: str | None = None
    call_ms: int = 0


# EMF names (DESIGN.md 7) -> TurnMetrics field or property.
TURN_METRICS = (("EosToFirstAudio", "eos_to_first_audio"), ("EosToReplyAudio", "eos_to_reply_audio"),
                ("TurnDetectMs", "turn_detect"),
                ("FirstSentenceMs", "first_sentence"), ("TtsFirstAudioMs", "tts_first_audio"),
                ("FirstBackchannelMs", "first_backchannel_ms"), ("BargeInStopMs", "barge_in_stop_ms"),
                ("BargeInDetectMs", "barge_in_detect_ms"))
TURN_COUNTS = (("BackchannelOverlaps", "backchannel_overlaps"), ("Rounds", "rounds"))


def percentile(values: list[int], p: float) -> int | None:
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    k = max(0, min(len(values) - 1, int(round(p / 100 * (len(values) - 1)))))
    return values[k]


class MetricsRecorder:
    def __init__(self, bus: EventBus, *, emit: bool = True) -> None:
        self.call = CallMetrics()
        self.emit = emit
        self._open: TurnMetrics | None = None      # the caller turn being spoken (VadStart .. TurnEnd)
        self._reply: TurnMetrics | None = None     # the turn whose reply is being produced or played
        self._backchannel_playing = False
        self._emitted: set[int] = set()
        self._language = "sv"

    # -- events ------------------------------------------------------------------------------

    def on_event(self, event) -> None:
        handler = _HANDLERS.get(type(event))
        if handler:
            handler(self, event)

    def _new_turn(self) -> TurnMetrics:
        turn = TurnMetrics(len(self.call.turns) + 1, self._language)
        self.call.turns.append(turn)
        return turn

    def _vad_start(self, e: VadStart) -> None:
        if self._open is None:
            self._open = self._new_turn()
            self._open.speech_start_ms = e.speech_start_ms
        if self._backchannel_playing:
            self._open.backchannel_overlaps += 1

    def _hold(self, e: TurnHold) -> None:
        if self._open is not None:
            self._open.holds += 1

    def _turn_end(self, e: TurnEnd) -> None:
        turn = self._open or self._new_turn()
        if turn.speech_start_ms is None:
            turn.speech_start_ms = e.speech_start_ms
        turn.eos_ms, turn.turn_end_ms, turn.language = e.eos_ms, e.at_ms, e.language
        self._language = e.language
        if self._reply is not None and self._reply is not turn:
            self._close(self._reply)
        self._reply, self._open = turn, None

    def _sentence(self, e: AgentSentence) -> None:
        if self._reply is not None and self._reply.first_sentence_ms is None:
            self._reply.first_sentence_ms = e.at_ms

    def _started(self, e: PlaybackStarted) -> None:
        if e.kind == "backchannel":
            self._backchannel_playing = True
            turn = self._open
            if turn is not None:
                turn.backchannels += 1
                if turn.first_backchannel_ms is None and turn.speech_start_ms is not None:
                    turn.first_backchannel_ms = e.at_ms - turn.speech_start_ms
            return
        if e.kind == "filler":
            if self._reply is not None:
                self._reply.filler_played = True
            return
        if self._reply is None:
            return
        if self._reply.first_audio_ms is None:
            self._reply.first_audio_ms = e.at_ms
        if e.kind == "opener":
            self._reply.opener_played = True
        elif self._reply.first_reply_ms is None:
            self._reply.first_reply_ms = e.at_ms

    def _finished(self, e: PlaybackFinished) -> None:
        if e.kind == "backchannel":
            self._backchannel_playing = False

    def _barge_in(self, e: BargeIn) -> None:
        self.call.barge_ins += 1
        if self._reply is not None:
            self._reply.barge_in_detect_ms = e.at_ms - e.speech_start_ms

    def _flushed(self, e: PlaybackFlushed) -> None:
        self._backchannel_playing = False
        if self._reply is not None and self._reply.barge_in_stop_ms is None:
            self._reply.barge_in_stop_ms = e.stop_ms

    def _done(self, e: AgentTurnDone) -> None:
        turn = self._reply
        if turn is not None:
            turn.rounds, turn.tools, turn.cut, turn.error = e.rounds, tuple(e.tools), e.cut, e.error
        if e.outcome:
            self.call.outcome = e.outcome
        if e.language != self._language:
            self._language = e.language

    def _language_decided(self, e: LanguageDecided) -> None:
        if self.call.first_language is None:
            self.call.first_language = e.current
        if e.action != LanguageAction.KEEP:
            self.call.language_detections.append((e.action.value, e.candidate or e.current, e.reason))
            if self.emit:
                log.metric("LanguageDetected", 1, "Count", action=e.action.value, reason=e.reason)
        if e.action == LanguageAction.SWITCH:
            self.call.language_switches += 1
            if self.emit:
                log.metric("LanguageSwitch", 1, "Count", reason=e.reason)
        self._language = e.current

    def _ended(self, e: CallEnded) -> None:
        self.call.end_reason, self.call.call_ms = e.reason, e.at_ms
        for turn in self.call.turns:
            self._close(turn)
        if self.emit:
            log.metric("CallMs", e.at_ms)
            log.metric("BargeIns", self.call.barge_ins, "Count")
            log.metric("Outcome", 1, "Count", outcome=self.call.outcome or "none")
            if self.call.first_language:
                log.metric("FirstLanguage", 1, "Count", language=self.call.first_language)

    def _close(self, turn: TurnMetrics) -> None:
        if turn.turn in self._emitted or turn.turn_end_ms is None:
            return
        self._emitted.add(turn.turn)
        if not self.emit:
            return
        for name, attr in TURN_METRICS:
            value = getattr(turn, attr)
            if value is not None:
                log.metric(name, value, language=turn.language)
        for name, attr in TURN_COUNTS:
            log.metric(name, getattr(turn, attr), "Count", language=turn.language)
        log.metric("FillerPlayed", int(turn.filler_played), "Count", language=turn.language)
        log.metric("OpenerPlayed", int(turn.opener_played), "Count", language=turn.language)

    # -- result ------------------------------------------------------------------------------

    def summary(self) -> dict:
        turns = [t.as_dict() for t in self.call.turns]
        eos = [t["eos_to_first_audio"] for t in turns if t["eos_to_first_audio"] is not None]
        reply = [t["eos_to_reply_audio"] for t in turns if t["eos_to_reply_audio"] is not None]
        return {"turns": turns, "language_detections": [list(x) for x in self.call.language_detections],
                "language_switches": self.call.language_switches, "first_language": self.call.first_language,
                "outcome": self.call.outcome, "barge_ins": self.call.barge_ins, "end_reason": self.call.end_reason,
                "call_ms": self.call.call_ms, "eos_to_first_audio_p50": percentile(eos, 50),
                "eos_to_first_audio_p90": percentile(eos, 90),
                "eos_to_reply_audio_p50": percentile(reply, 50), "openers": sum(t["opener_played"] for t in turns),
                "backchannels": sum(t["backchannels"] for t in turns),
                "backchannel_overlaps": sum(t["backchannel_overlaps"] for t in turns),
                "barge_in_stop_max": max((t["barge_in_stop"] for t in turns if t["barge_in_stop"] is not None),
                                         default=None)}


_HANDLERS = {VadStart: MetricsRecorder._vad_start, TurnHold: MetricsRecorder._hold, TurnEnd: MetricsRecorder._turn_end,
             AgentSentence: MetricsRecorder._sentence, PlaybackStarted: MetricsRecorder._started,
             PlaybackFinished: MetricsRecorder._finished, BargeIn: MetricsRecorder._barge_in,
             PlaybackFlushed: MetricsRecorder._flushed, AgentTurnDone: MetricsRecorder._done,
             LanguageDecided: MetricsRecorder._language_decided, CallEnded: MetricsRecorder._ended}

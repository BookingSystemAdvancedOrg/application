"""Per-turn and per-call metrics from bus events. Owner: builder 'brain'.

MetricsRecorder hangs off EventBus.listen (synchronous, in publish order)
and fills TurnMetrics per caller turn and CallMetrics per call; at
TurnEnd+AgentTurnDone+first audio it emits EMF lines via log.metric with
the dimension `language` only. Never text, never numbers, never ids in
dimensions (log.event carries the hashed call tag).

Definitions (all ms on the call clock):
  eos_to_first_audio     TurnEnd.eos_ms -> first PlaybackStarted (kind assistant
                         or fixed) of the reply; THE latency number (target p50
                         under 1200, ASSESSMENT)
  turn_end_to_first_audio TurnEnd.at_ms -> same (excludes the turn detector wait)
  turn_detect            TurnEnd.at_ms - TurnEnd.eos_ms (what endpointing costs)
  first_sentence         TurnEnd.at_ms -> first AgentSentence
  tts_first_audio        first AgentSentence -> first PlaybackStarted
  first_backchannel      VadStart.speech_start_ms -> first backchannel PlaybackStarted
                         in the turn (None when none played)
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

from voice_gateway.events import EventBus


@dataclass
class TurnMetrics:
    turn: int
    language: str = "sv"
    speech_start_ms: int | None = None
    eos_ms: int | None = None
    turn_end_ms: int | None = None
    first_sentence_ms: int | None = None
    first_audio_ms: int | None = None
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
    def turn_end_to_first_audio(self) -> int | None:
        if self.turn_end_ms is None or self.first_audio_ms is None:
            return None
        return self.first_audio_ms - self.turn_end_ms


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


class MetricsRecorder:
    def __init__(self, bus: EventBus, *, emit: bool = True) -> None:
        self.call = CallMetrics()
        self.emit = emit

    def on_event(self, event) -> None:
        raise NotImplementedError

    def summary(self) -> dict:
        raise NotImplementedError

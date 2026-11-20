"""Events on the per-call bus (FROZEN: the contract between builders).

Every event is a frozen dataclass with `at_ms` on the call clock (clock.py).
Producers publish; any component may subscribe. Audio frames never travel
on the bus (they flow through direct async iterators, DESIGN.md 4).

Text fields (transcripts, sentences) live only in memory: never log them
(log.py drops them). Phone numbers never appear in any event.

Producer per event (who may publish it):
  listen/   VadStart, VadPause, VadResume, SttPartial, SttFinal, LanguageEvidence,
            LanguageDecided, TurnHold, TurnEnd, BargeIn
  speak/    PlaybackStarted, PlaybackFinished, PlaybackFlushed, BackchannelRequest(*)
  backchannel/  BackchannelRequest
  brain/    AgentSentence, ToolStarted, ToolFinished, AgentTurnDone
  orchestrator  StateChanged, AssistantContext, CallEnded
(*) only backchannel/ creates BackchannelRequest; the mixer consumes it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, Callable, Literal


# --- listen ----------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class VadStart:
    """Caller speech began (confirmed after VadConfig.start_confirm_ms).
    `speech_start_ms` is the onset (earlier than at_ms by the confirm time).
    `during_playback`: assistant audio was playing at the onset."""
    at_ms: int
    speech_start_ms: int
    prob: float
    during_playback: bool = False


@dataclass(frozen=True, slots=True)
class VadPause:
    """Silence onset inside caller speech, confirmed after
    VadConfig.pause_confirm_ms. `silence_start_ms` is the onset. The pause
    is not a turn end: TurnDetector decides that."""
    at_ms: int
    silence_start_ms: int
    speech_ms: int          # voiced ms since the VadStart of this caller turn


@dataclass(frozen=True, slots=True)
class VadResume:
    """Speech again after a VadPause (same caller turn)."""
    at_ms: int
    pause_ms: int


@dataclass(frozen=True, slots=True)
class SttPartial:
    """A Transcribe partial result. `stable_text` is the stabilised prefix
    (partial results stabilisation), `text` the whole current hypothesis."""
    at_ms: int
    segment_id: str
    text: str
    stable_text: str
    language: str           # our code (sv, en, ...), the stream's language
    stream_id: int          # which Transcribe stream produced it (a reopen makes a new one)
    last_word_ms: int | None = None     # audio time (call clock) the last word ends; at_ms is the result's end


@dataclass(frozen=True, slots=True)
class SttFinal:
    at_ms: int
    segment_id: str
    text: str
    language: str
    stream_id: int
    start_ms: int           # audio time of the segment on the call clock
    end_ms: int
    confidence: float | None = None             # mean word confidence, None if absent
    language_scores: tuple[tuple[str, float], ...] = ()   # identify-language streams only, our codes
    last_word_ms: int | None = None     # audio time the last word ends (end_ms also covers trailing audio)


@dataclass(frozen=True, slots=True)
class LanguageEvidence:
    """One piece of language evidence (MULTILINGUAL.md rules)."""
    at_ms: int
    source: Literal["transcribe_lid", "hint", "model_tag", "explicit", "asr_confidence"]
    code: str | None
    score: float | None = None
    speech_ms: int = 0
    content_words: int = 0


class LanguageAction(str, Enum):
    KEEP = "keep"
    SWITCH = "switch"
    ASK = "ask"                 # rule 5: ask once in the current language plus the candidate
    REPEAT = "repeat"           # rule 7: poor audio, ask to repeat in the current language
    UNSUPPORTED = "unsupported"  # rule 8: detected, but no voice (fa, bs, id, so, ...)
    CALLBACK = "callback"       # rule 7 third miss: offer a callback, topic language:unknown


@dataclass(frozen=True, slots=True)
class LanguageDecided:
    at_ms: int
    action: LanguageAction
    current: str                 # the call language after this decision
    candidate: str | None        # the language the decision is about (switch target, unsupported code)
    reason: str                  # "strong" | "accumulated" | "explicit" | "asked" | "first_utterance" | ...
    switches: int                # automatic switches so far on this call


@dataclass(frozen=True, slots=True)
class TurnHold:
    """The silence threshold was extended (hesitation, dangling word, number
    being spoken, open question). For metrics and the backchannel controller."""
    at_ms: int
    reason: Literal["filler", "dangling", "number", "open_question", "short_answer"]
    threshold_ms: int


@dataclass(frozen=True, slots=True)
class TurnEnd:
    """The caller's turn is over: the brain may answer `text`.
    `eos_ms` is the caller's last voiced ms (end of speech), the zero point
    of the end-of-speech-to-first-audio metric."""
    at_ms: int
    text: str
    language: str
    speech_start_ms: int
    eos_ms: int
    reason: Literal["silence", "max_length", "final_after_silence"]
    confidence: float | None = None
    language_decision: LanguageDecided | None = None


@dataclass(frozen=True, slots=True)
class BargeIn:
    """Caller speech over assistant playback that is neither echo nor a
    backchannel: playback must stop now."""
    at_ms: int
    speech_start_ms: int
    reason: Literal["duration", "words", "stop_word"]


# --- speak / backchannel ---------------------------------------------------------------------

PlaybackKind = Literal["assistant", "fixed", "filler", "backchannel"]


@dataclass(frozen=True, slots=True)
class BackchannelRequest:
    at_ms: int
    language: str
    token: str               # key into backchannel.tokens.TOKENS[language]
    reason: str = "clause_pause"


@dataclass(frozen=True, slots=True)
class PlaybackStarted:
    at_ms: int
    item_id: str
    kind: PlaybackKind
    language: str
    duration_ms: int         # audio length of the item (known after TTS)


@dataclass(frozen=True, slots=True)
class PlaybackFinished:
    at_ms: int
    item_id: str
    kind: PlaybackKind
    played_ms: int
    interrupted: bool


@dataclass(frozen=True, slots=True)
class PlaybackFlushed:
    """Barge-in flush done. `stop_ms` = at_ms - the BargeIn's at_ms."""
    at_ms: int
    dropped_ms: int
    stop_ms: int
    unplayed_item_ids: tuple[str, ...] = ()


# --- brain -----------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class AgentSentence:
    """One sentence of the agent's reply, ready for TTS, already through
    agent.spoken_text. `language` picks the voice (native voice guard).
    `kind` "fixed" = Booqy-authored copy (disclosure, error, still checking)."""
    at_ms: int
    turn: int
    index: int
    text: str
    language: str
    kind: Literal["assistant", "fixed"] = "assistant"
    preamble: bool = False   # written before a tool call ("one moment, I am checking")


@dataclass(frozen=True, slots=True)
class ToolStarted:
    at_ms: int
    turn: int
    name: str


@dataclass(frozen=True, slots=True)
class ToolFinished:
    at_ms: int
    turn: int
    name: str
    ok: bool | None
    ms: int


@dataclass(frozen=True, slots=True)
class ProviderAction:
    """An action the agent asked the line to do (session.call_turn actions)."""
    name: Literal["end_call", "transfer_to_number", "language_detection"]
    arguments: dict = field(default_factory=dict, hash=False, compare=False)


@dataclass(frozen=True, slots=True)
class AgentTurnDone:
    at_ms: int
    turn: int
    language: str            # the call language after the turn
    rounds: int
    cut: bool                # TURN_BUDGET_S ran out: the still-checking sentence was said
    error: bool
    actions: tuple[ProviderAction, ...] = ()
    outcome: str | None = None              # toolState outcome (booked, moved, callback, ...)
    awaiting_confirmation: bool = False     # a read-back is pending a clear yes
    asked_question: Literal["yes_no", "open", "number", None] = None
    latency_ms: int = 0
    tools: tuple[str, ...] = ()


# --- orchestrator ----------------------------------------------------------------------------

class CallState(str, Enum):
    CONNECTING = "connecting"
    GREETING = "greeting"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    TRANSFERRING = "transferring"
    ENDING = "ending"
    ENDED = "ended"


@dataclass(frozen=True, slots=True)
class StateChanged:
    at_ms: int
    old: CallState
    new: CallState
    reason: str = ""


@dataclass(frozen=True, slots=True)
class AssistantContext:
    """What the caller is answering. Set by the orchestrator after each
    AgentTurnDone; read by TurnDetector and BackchannelController."""
    at_ms: int
    asked_question: Literal["yes_no", "open", "number", None]
    awaiting_confirmation: bool
    language: str


@dataclass(frozen=True, slots=True)
class CallEnded:
    at_ms: int
    reason: str


Event = (VadStart | VadPause | VadResume | SttPartial | SttFinal | LanguageEvidence | LanguageDecided | TurnHold
         | TurnEnd | BargeIn | BackchannelRequest | PlaybackStarted | PlaybackFinished | PlaybackFlushed
         | AgentSentence | ToolStarted | ToolFinished | AgentTurnDone | StateChanged | AssistantContext | CallEnded)


# --- the bus ---------------------------------------------------------------------------------

class Subscription:
    def __init__(self, bus: "EventBus", types: tuple[type, ...], maxsize: int) -> None:
        self._bus, self.types = bus, types
        self.queue: asyncio.Queue = asyncio.Queue(maxsize)

    def __aiter__(self) -> AsyncIterator:
        return self._iter()

    async def _iter(self):
        while True:
            item = await self.queue.get()
            if item is _CLOSED:
                return
            yield item

    async def get(self):
        return await self.queue.get()

    def close(self) -> None:
        self._bus._subs.discard(self)
        self.queue.put_nowait(_CLOSED)


_CLOSED = object()


class EventBus:
    """In-process fan-out for one call. `publish` never blocks: a full
    subscriber queue drops its OLDEST item and counts it in `dropped` (a
    slow consumer must never stall audio). Synchronous `listen` handlers
    run inline, in publish order (pure cores hang off these)."""

    def __init__(self, *, maxsize: int = 512) -> None:
        self._subs: set[Subscription] = set()
        self._handlers: list[tuple[tuple[type, ...], Callable]] = []
        self._maxsize = maxsize
        self.dropped = 0
        self.history: list | None = None    # tests and the harness: set to [] to record every event

    def subscribe(self, *types: type) -> Subscription:
        sub = Subscription(self, types, self._maxsize)
        self._subs.add(sub)
        return sub

    def listen(self, handler: Callable, *types: type) -> None:
        self._handlers.append((types, handler))

    def publish(self, event) -> None:
        if self.history is not None:
            self.history.append(event)
        for types, handler in list(self._handlers):
            if not types or isinstance(event, types):
                handler(event)
        for sub in list(self._subs):
            if sub.types and not isinstance(event, sub.types):
                continue
            if sub.queue.full():
                sub.queue.get_nowait()
                self.dropped += 1
            sub.queue.put_nowait(event)

    def close(self) -> None:
        for sub in list(self._subs):
            sub.close()

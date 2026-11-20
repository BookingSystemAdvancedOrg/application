"""Streaming out of the unchanged agent loop. Owner: builder 'brain'.

How it streams WITHOUT editing shared/: agent.run_turn calls
`model.converse(...)` once per round (in a worker thread, agent._within).
StreamingAgentModel subclasses shared.agent_model_real.RealAgentModel and
overrides only `_once`: it calls converse_stream exactly as the parent,
wraps the event stream in a tee that reports every event to a StreamSink
BEFORE handing it on, and returns `read_stream(tee, deadline)`: the Turn
that run_turn sees is byte-for-byte what it sees today, so every guard
(tools.Executor read-back and clear-yes, real availability only, writes
never abandoned, deadlines, spoken_text cleaning, unsupported language
replacement) runs unchanged. Only the AUDIO starts earlier.

StreamSink callbacks run in the worker thread; StreamBridge moves them to
the call's event loop (loop.call_soon_threadsafe) and drops callbacks of
a round that is no longer current (a worker left running after the turn
deadline, agent._within, or a retried attempt: `round_restarted`).

TextStreamer (pure, event loop side) turns one round's text deltas into
AgentSentence events:
  * strips the leading `[xx]` tag of each text block (language.tag) and
    picks the voice language from it (native voice guard): a tag that
    languages.can_speak and the call allows streams in that language; an
    unknown or unspeakable tag holds the text until AgentTurnDone
    (BrainConfig.hold_unknown_tag) because session.call_turn may replace
    the reply with the fixed unsupported sentences;
  * speak.chunker.SentenceChunker for sentence borders;
  * every sentence through agent.spoken_text(sentence, code); a sentence
    matching agent._META (an English note to itself) is dropped;
  * reasoning deltas (thinking "low") are never text: ignored;
  * a round ending in max_tokens drops its unfinished last fragment
    (agent._whole_sentences semantics);
  * toolUse contentBlockStart -> ToolStarted; the next round's start (or
    the turn's end) -> ToolFinished.

Reconciliation at the end of the turn (AgentBrain): the reply returned by
session.call_turn is the truth. Its sentences that were not streamed yet
(the disclosure sentence prepended on a language switch, the error or
still-checking sentence, the unsupported sentences, a held reply) are
emitted in order as kind "fixed" or "assistant"; streamed sentences that
the final reply leaves out (a preamble before a lookup) were already
heard, which is fine: the history keeps the model's full text.

Prompt caching: `_once` adds Bedrock cachePoint blocks after the system
prompt and after the tool list when BrainConfig.prompt_cache and none
are present yet (verify Haiku 5.5 on Bedrock supports it; the Stage 1
change may add the same in shared, then this is a no-op).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from shared.agent_model_real import RealAgentModel

from voice_gateway.clock import Clock
from voice_gateway.config import BrainConfig, SpeakConfig
from voice_gateway.events import AgentSentence, EventBus, ToolFinished, ToolStarted


class StreamSink(Protocol):
    """Called from the worker thread, in stream order."""

    def round_started(self, round_id: int) -> None: ...

    def round_restarted(self, round_id: int) -> None: ...

    def text_delta(self, round_id: int, text: str) -> None: ...

    def tool_started(self, round_id: int, name: str) -> None: ...

    def round_stopped(self, round_id: int, stop_reason: str) -> None: ...


class StreamingAgentModel(RealAgentModel):
    """One per call (holds the sink). `converse` is inherited unchanged."""

    def __init__(self, sink: StreamSink, cfg: BrainConfig, client=None, model_id=None) -> None:
        super().__init__(client=client, model_id=model_id)
        self.sink, self.cfg = sink, cfg
        self._round = 0

    def _once(self, args, left, deadline):
        raise NotImplementedError


class StreamBridge:
    """StreamSink implementation: thread -> loop, stale rounds dropped,
    feeds TextStreamer and publishes on the bus."""

    def __init__(self, loop: asyncio.AbstractEventLoop, bus: EventBus, clock: Clock, cfg: BrainConfig,
                 speak: SpeakConfig) -> None:
        self.loop, self.bus, self.clock, self.cfg, self.speak = loop, bus, clock, cfg, speak

    def open_turn(self, turn: int, language: str, allowed: tuple[str, ...]) -> None:
        raise NotImplementedError

    def close_turn(self) -> list[AgentSentence]:
        """Ends the turn: every later callback is dropped; returns the
        sentences already emitted (for reconciliation)."""
        raise NotImplementedError

    def round_started(self, round_id: int) -> None:
        raise NotImplementedError

    def round_restarted(self, round_id: int) -> None:
        raise NotImplementedError

    def text_delta(self, round_id: int, text: str) -> None:
        raise NotImplementedError

    def tool_started(self, round_id: int, name: str) -> None:
        raise NotImplementedError

    def round_stopped(self, round_id: int, stop_reason: str) -> None:
        raise NotImplementedError


@dataclass
class TextStreamer:
    """Pure: one turn's deltas -> sentences (see the module docstring)."""
    speak: SpeakConfig
    call_language: str
    allowed: tuple[str, ...]
    turn: int = 0

    def start_block(self) -> None:
        raise NotImplementedError

    def feed(self, delta: str, at_ms: int) -> list[AgentSentence]:
        raise NotImplementedError

    def end_round(self, stop_reason: str, at_ms: int) -> list[AgentSentence]:
        raise NotImplementedError

    @property
    def held(self) -> bool:
        """Text is being held (unknown or unspeakable tag)."""
        raise NotImplementedError


def tool_events(turn: int, name: str, at_ms: int) -> ToolStarted:
    return ToolStarted(at_ms, turn, name)


def tool_finished(turn: int, name: str, started_ms: int, at_ms: int, ok: bool | None) -> ToolFinished:
    return ToolFinished(at_ms, turn, name, ok, at_ms - started_ms)

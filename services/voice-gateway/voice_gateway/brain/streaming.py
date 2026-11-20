"""Streaming out of the unchanged agent loop. Owner: builder 'brain'.

How it streams WITHOUT editing shared/: agent.run_turn calls
`model.converse(...)` once per round (in a worker thread, agent._within).
StreamingAgentModel subclasses shared.agent_model_real.RealAgentModel and
overrides only `_once` (plus `client_for`, which hands the parent's own
`_once` a client whose event stream is teed): every ConverseStream event is
reported to a StreamSink BEFORE the parent's `read_stream(...)` reads it,
so the Turn that run_turn sees is byte-for-byte what it sees today and
every guard (tools.Executor read-back and clear-yes, real availability
only, writes never abandoned, deadlines, spoken_text cleaning, the
unsupported language replacement) runs unchanged. Only the AUDIO starts
earlier.

StreamSink callbacks run in the worker thread; StreamBridge moves them to
the call's event loop (loop.call_soon_threadsafe) and drops callbacks of
a round that is no longer current (a worker left running after the turn
deadline, agent._within, or a retried attempt: `round_restarted`). The tee
itself stops reporting once the round's deadline passed, so text the
agent loop no longer waits for is never spoken.

TextStreamer (pure, event loop side) turns one turn's text deltas into
AgentSentence events:
  * strips the leading `[xx]` tag of each text block (language.tag) and
    picks the voice language from it (native voice guard): a tag that
    languages.can_speak and the call allows streams in that language; a
    known but unspeakable or not allowed tag holds the text until
    AgentTurnDone (BrainConfig.hold_unknown_tag) because session.call_turn
    may replace the reply with the fixed unsupported sentences;
  * a first sentence in a language the call has not disclosed in yet is
    preceded by copy.disclosure_short in that language (kind "fixed"):
    session.call_turn prepends the same sentence to the reply when it
    switches, so the reconciliation finds it already said;
  * sentence borders (., ?, ! and their CJK and Arabic forms, then a
    space), a short first chunk is joined with the next sentence
    (SpeakConfig.first_chunk_min_chars), a long clause is cut at its last
    comma (SpeakConfig.clause_split_chars);
  * every sentence through agent.spoken_text(sentence, code); a sentence
    that cleans to nothing (an English note to itself, agent._META) is
    dropped; a sentence identical to one already streamed in this turn is
    dropped (run_turn does the same);
  * reasoning deltas (thinking "low") never reach the sink (only text);
  * a round ending in max_tokens drops its unfinished last fragment
    (agent._whole_sentences semantics); a failed round drops it too;
  * toolUse contentBlockStart -> ToolStarted (end_call is not a lookup:
    no event, so no filler before a goodbye); the next round's start (or
    the turn's end) -> ToolFinished.

Reconciliation at the end of the turn (`reconcile`): the reply returned by
session.call_turn is the truth. Its sentences that were not streamed yet
(the disclosure sentence prepended on a language switch, the error or
still-checking sentence, the unsupported sentences, a held reply) are
emitted in order, as kind "fixed" when they are Booqy copy (spoken in the
copy's own language: each unsupported sentence in the voice of the language it is written in)
or "assistant" otherwise; streamed sentences that the final reply leaves
out (a preamble before a lookup) were already heard, which is fine: the
history keeps the model's full text.

Prompt caching: `_once` adds Bedrock cachePoint blocks after the system
prompt and after the tool list when BrainConfig.prompt_cache and none are
present yet (verify Haiku 5.5 on Bedrock supports it; the Stage 1 change
may add the same in shared, then this is a no-op).
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Protocol

from shared.agent_model import AgentModelError
from shared.agent_model_real import RealAgentModel
from shared.features.phone_agent import agent as pa_agent
from shared.features.phone_agent import calls as pa_calls
from shared.features.phone_agent import copy as pa_copy
from shared.features.phone_agent import language as pa_language

from voice_gateway import languages
from voice_gateway.clock import Clock
from voice_gateway.config import BrainConfig, SpeakConfig
from voice_gateway.events import AgentSentence, EventBus, ToolFinished, ToolStarted

# A note the model wrote to itself beside a lookup ("Friday is 2026-11-27.", 'Time 17:00 is "klockan fem".'): an ISO
# date, a clock time in digits or a quoted phrase is never something a person says. Dropped from the live stream; the
# reconciliation still says it if the final reply (the truth) holds the sentence.
_SELF_NOTE = re.compile(r'\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}:\d{2}\b|["\u201c\u201d]')


def is_self_note(text: str) -> bool:
    return bool(_SELF_NOTE.search(text or ""))


# A sentence that says a write is done ("Jag har bokat ett bord", "Your table is booked"). Streamed before the
# round ends, it could be followed by the create_reservation call itself, which the guard may still refuse (no
# fresh read-back, no clear yes, no availability): the caller would hear a booking that does not exist. Such a
# sentence, and the rest of its round, is never streamed; the reconciliation says it from the final reply (the
# truth) once the turn is done. A question is not a claim. A false match only costs latency.
_CLAIM = re.compile(
    r"\b(bokat|bokad|bokade|avbokat|avbokad|ombokat|ombokad|flyttat|flyttad|bekräftat|bekräftad|registrerat|"
    r"registrerad|booked|cancell?ed|confirmed|rescheduled|reserved|all set|gebucht|storniert|bestätigt|"
    r"verschoben|reserviert|réservé|réservée|confirmé|confirmée|annulé|annulée|reservado|reservada|confirmado|"
    r"confirmada|cancelado|cancelada|zarezerwowa\w*|potwierdzon\w*|anulowan\w*|rezervasyon\w* (yapıldı|"
    r"oluşturuldu|onaylandı)|iptal edildi)\b", re.I)


def is_settled_claim(text: str) -> bool:
    text = (text or "").strip()
    return bool(text) and not text.endswith(("?", "\uff1f", "\u061f")) and bool(_CLAIM.search(text))


NO_FILLER_TOOLS = frozenset({"end_call"})   # not a lookup: never a "one moment" before the goodbye


class StreamSink(Protocol):
    """Called from the worker thread, in stream order."""

    def round_started(self, round_id: int) -> None: ...

    def round_restarted(self, round_id: int) -> None: ...

    def text_delta(self, round_id: int, text: str) -> None: ...

    def tool_started(self, round_id: int, name: str) -> None: ...

    def round_stopped(self, round_id: int, stop_reason: str) -> None: ...


# --- the model ---------------------------------------------------------------------------------

class _TeeStream:
    """Iterates a ConverseStream event stream for read_stream and reports
    text deltas, tool starts and text block changes to the sink first.
    Nothing is reported once the round's deadline passed (read_stream then
    raises timeout; the agent loop stopped waiting)."""

    def __init__(self, stream, sink: StreamSink, round_id: int, deadline: float | None) -> None:
        self._stream, self._sink, self._round, self._deadline = stream, sink, round_id, deadline
        self._block = None

    def close(self):
        close = getattr(self._stream, "close", None)
        if close:
            close()

    def __iter__(self):
        for event in self._stream:
            if self._deadline is None or time.monotonic() <= self._deadline:
                self._report(event)
            yield event

    def _report(self, event) -> None:
        if "contentBlockStart" in event:
            start = (event["contentBlockStart"].get("start") or {}).get("toolUse")
            if start:
                self._block = event["contentBlockStart"].get("contentBlockIndex")
                self._sink.tool_started(self._round, str(start.get("name") or ""))
        elif "contentBlockDelta" in event:
            delta = event["contentBlockDelta"].get("delta") or {}
            if "text" in delta:
                index = event["contentBlockDelta"].get("contentBlockIndex")
                if index != self._block:
                    self._block = index
                    block = getattr(self._sink, "block_started", None)
                    if block:
                        block(self._round)
                self._sink.text_delta(self._round, delta["text"])


class _TeeClient:
    """The Bedrock client the parent's `_once` gets: converse_stream's event
    stream is teed; everything else is the real client."""

    def __init__(self, real, sink: StreamSink, round_id: int, deadline: float | None) -> None:
        self._real, self._sink, self._round, self._deadline = real, sink, round_id, deadline

    def converse_stream(self, **kwargs):
        answer = self._real.converse_stream(**kwargs)
        return {**answer, "stream": _TeeStream(answer.get("stream") or [], self._sink, self._round, self._deadline)}

    def __getattr__(self, name):
        return getattr(self._real, name)


def with_cache_points(args: dict) -> dict:
    """Bedrock prompt caching: a cachePoint after the system prompt and
    after the tool list, unless one is there already. A copy; `args` is
    not changed (the parent retries with the same dict)."""
    out = dict(args)
    system = list(out.get("system") or [])
    if system and not any("cachePoint" in block for block in system):
        out["system"] = system + [{"cachePoint": {"type": "default"}}]
    tools = out.get("toolConfig")
    if tools and tools.get("tools") and not any("cachePoint" in t for t in tools["tools"]):
        out["toolConfig"] = {**tools, "tools": list(tools["tools"]) + [{"cachePoint": {"type": "default"}}]}
    return out


class StreamingAgentModel(RealAgentModel):
    """One per call (holds the sink). `converse` is inherited unchanged."""

    def __init__(self, sink: StreamSink, cfg: BrainConfig, client=None, model_id=None) -> None:
        super().__init__(client=client, model_id=model_id)
        self.sink, self.cfg = sink, cfg
        self._round = 0
        self._failed = False
        self._lock = threading.Lock()
        self._local = threading.local()

    def client_for(self, seconds):
        real = super().client_for(seconds)
        current = getattr(self._local, "round", None)
        return _TeeClient(real, self.sink, *current) if current else real

    def _once(self, args, left, deadline):
        with self._lock:
            self._round += 1
            round_id, restarted = self._round, self._failed
        (self.sink.round_restarted if restarted else self.sink.round_started)(round_id)
        self._local.round = (round_id, deadline)
        try:
            turn = super()._once(with_cache_points(args) if self.cfg.prompt_cache else args, left, deadline)
        except AgentModelError:
            self._failed = True
            self.sink.round_stopped(round_id, "error")
            raise
        finally:
            self._local.round = None
        self._failed = False
        self.sink.round_stopped(round_id, turn.stop_reason or "end_turn")
        return turn


# --- sentences -----------------------------------------------------------------------------------

_BORDER = re.compile(r"(?<=[.?!。？！؟።])[\"')\]]*\s+|\n\s*\n")
_SPLIT = re.compile(r"(?<=[.?!。？！؟።])\s+")
_TAG_MAX = 12          # "[xx-YYYY] " at most: a block that starts otherwise has no tag


def split_sentences(text: str) -> list[str]:
    """Whole sentences of a finished text (a time like 19.30 is not a border)."""
    return [s.strip() for s in _SPLIT.split(" ".join(str(text or "").split())) if s.strip()]


def _norm(text: str) -> str:
    return pa_calls.normalized_text(text)


def _fixed_index() -> dict[str, tuple[str, ...]]:
    """Every sentence of Booqy's fixed copy -> the languages it is written in."""
    found: dict[str, list[str]] = {}
    tables = (pa_copy.DISCLOSURE_SHORT, pa_copy.ERROR, pa_copy.STILL_CHECKING, pa_copy.MAX_LENGTH,
              pa_copy.UNSUPPORTED_LANGUAGE, pa_copy.UNSUPPORTED_CALLBACK, pa_copy.TRANSFER_UNANSWERED,
              pa_copy.TRANSFERRING, pa_copy.STILL_THERE, pa_copy.ASK_LANGUAGE, pa_copy.REFUSED,
              pa_copy.DEFAULT_EXTRA)
    for table in tables:
        for lang, text in table.items():
            for sentence in split_sentences(text):
                found.setdefault(_norm(sentence), [])
                if lang not in found[_norm(sentence)]:
                    found[_norm(sentence)].append(lang)
    return {k: tuple(v) for k, v in found.items()}


FIXED = _fixed_index()


def fixed_language(sentence: str, prefer: str | None = None) -> str | None:
    """The language of a sentence of Booqy's fixed copy (None: not copy).
    A sentence two languages share (no, da) takes `prefer` when it is one."""
    langs = FIXED.get(_norm(sentence))
    if not langs:
        return None
    if prefer in langs:
        return prefer
    return next((x for x in ("sv", "en") if x in langs), langs[0])


def reconcile(final_reply: str, streamed: list[AgentSentence], *, turn: int, language: str, at_ms: int,
              start_index: int = 0) -> list[AgentSentence]:
    """The sentences of the authoritative reply not streamed yet, in order.
    Booqy copy is kind "fixed" in the copy's own language; the rest is
    "assistant" in `language` (the reply's language)."""
    # One running text, not one entry per chunk: a streamed chunk may hold two short sentences, and a long
    # sentence may have been streamed as two chunks split at a comma (both are the same words in the final).
    said = " ".join(_norm(s.text) for s in streamed)
    out, index = [], start_index
    for sentence in split_sentences(final_reply):
        key = _norm(sentence)
        if not key or f" {key} " in f" {said} ":
            continue
        said += " " + key
        lang = fixed_language(sentence, language)
        out.append(AgentSentence(at_ms, turn, index, sentence, lang or language, "fixed" if lang else "assistant"))
        index += 1
    return out


@dataclass
class TextStreamer:
    """Pure: one turn's deltas -> sentences (see the module docstring)."""
    speak: SpeakConfig
    call_language: str
    allowed: tuple[str, ...]
    turn: int = 0
    disclosed: tuple[str, ...] = ()
    hold_unknown_tag: bool = True
    emitted: list[AgentSentence] = field(default_factory=list)
    _head: str = ""
    _tag_pending: bool = True
    _buf: str = ""
    _lang: str = ""
    _held: bool = False
    _held_text: str = ""
    _seen: set = field(default_factory=set)
    _preamble: bool = False
    _claim_hold: bool = False             # a done claim was seen in this round: the rest waits for the reply

    def __post_init__(self) -> None:
        self._lang = self.call_language
        self.disclosed = tuple(self.disclosed) or (self.call_language,)

    # -- blocks and rounds -------------------------------------------------------------------

    def start_block(self, at_ms: int = 0) -> list[AgentSentence]:
        """A new text block (a round start, or text after a tool block): the
        previous block's tail is a sentence of its own; the tag is read again."""
        out = self._flush(at_ms)
        self._tag_pending, self._head = True, ""
        return out

    def restart_round(self) -> None:
        """A failed attempt is retried: its unspoken tail is dropped."""
        self._buf, self._head, self._tag_pending = "", "", True
        self._claim_hold = False

    def tool_started(self, at_ms: int) -> list[AgentSentence]:
        """Text before a lookup is a preamble ("one moment, I am checking")."""
        self._preamble = True
        out = self._flush(at_ms)
        self._preamble = False
        self._tag_pending, self._head = True, ""
        self._claim_hold = False            # a claim before a tool call is dropped: the final reply says what is true
        return out

    def end_round(self, stop_reason: str, at_ms: int) -> list[AgentSentence]:
        if stop_reason in ("max_tokens", "error"):
            out = self._emit_ready(at_ms, final=False)
            self._buf = ""                      # the unfinished fragment is never spoken
        else:
            out = self._flush(at_ms)
        self._tag_pending, self._head = True, ""
        self._claim_hold = False
        return out

    # -- text --------------------------------------------------------------------------------

    def feed(self, delta: str, at_ms: int) -> list[AgentSentence]:
        if not delta:
            return []
        if self._tag_pending:
            self._head += delta
            head = self._head.lstrip()
            if not head:
                return []
            if head.startswith("[") and "]" not in head and len(head) < _TAG_MAX:
                return []                      # the tag is still arriving
            self._tag_pending = False
            code, delta = pa_language.tag(head)
            self._head = ""
            self._set_block_language(code)
        if self._held:
            self._held_text += delta
            return []
        self._buf += delta
        return self._emit_ready(at_ms, final=False)

    @property
    def held(self) -> bool:
        """Text is being held (unknown or unspeakable tag)."""
        return self._held

    @property
    def held_text(self) -> str:
        return self._held_text

    # -- internals ---------------------------------------------------------------------------

    def _set_block_language(self, code: str | None) -> None:
        if code is None:
            self._lang = self.call_language
            return
        if languages.can_speak(code) and code in self.allowed:
            self._lang = code
        elif self.hold_unknown_tag:
            self._held, self._lang = True, code
        else:
            self._lang = self.call_language

    def _flush(self, at_ms: int) -> list[AgentSentence]:
        return self._emit_ready(at_ms, final=True)

    def _emit_ready(self, at_ms: int, *, final: bool) -> list[AgentSentence]:
        out = []
        while True:
            piece = self._next_piece(final)
            if piece is None:
                break
            out.extend(self._sentence(piece, at_ms))
        return out

    def _next_piece(self, final: bool) -> str | None:
        buf = self._buf
        if not buf.strip():
            if final:
                self._buf = ""
            return None
        first = not self.emitted
        start = 0
        while True:
            match = _BORDER.search(buf, start)
            if not match:
                break
            piece = buf[:match.start() + len(match.group(0).rstrip())]
            if first and len(piece.strip()) < self.speak.first_chunk_min_chars:
                start = match.end()            # a short first chunk waits for the next sentence
                continue
            self._buf = buf[match.end():]
            return piece
        if final:
            self._buf = ""
            return buf
        if len(buf) > self.speak.clause_split_chars:
            cut = buf.rfind(", ", self.speak.first_chunk_min_chars)
            if cut > 0:
                self._buf = buf[cut + 2:]
                return buf[:cut + 1]
        return None

    def _sentence(self, raw: str, at_ms: int) -> list[AgentSentence]:
        text = pa_agent.spoken_text(raw, self._lang)
        key = _norm(text)
        if not key or key in self._seen or is_self_note(text):
            return []
        if self._claim_hold or is_settled_claim(text):
            self._claim_hold = True             # said by the reconciliation if the final reply keeps it
            return []
        out = []
        if self._lang not in self.disclosed:
            self.disclosed = (*self.disclosed, self._lang)
            short = pa_copy.disclosure_short(self._lang)
            if short and _norm(short) not in self._seen:
                self._seen.add(_norm(short))
                out.append(self._make(short, "fixed", at_ms))
        self._seen.add(key)
        out.append(self._make(text, "assistant", at_ms))
        return out

    def _make(self, text: str, kind: str, at_ms: int) -> AgentSentence:
        sentence = AgentSentence(at_ms, self.turn, len(self.emitted), text, self._lang, kind,
                                 self._preamble and kind == "assistant")
        self.emitted.append(sentence)
        return sentence


# --- the bridge --------------------------------------------------------------------------------

class StreamBridge:
    """StreamSink implementation: thread -> loop, stale rounds dropped,
    feeds TextStreamer and publishes on the bus. Items (AgentSentence,
    ToolStarted, ToolFinished) are also put on `items` for the turn's
    iterator (AgentBrain.turn)."""

    def __init__(self, loop: asyncio.AbstractEventLoop, bus: EventBus, clock: Clock, cfg: BrainConfig,
                 speak: SpeakConfig) -> None:
        self.loop, self.bus, self.clock, self.cfg, self.speak = loop, bus, clock, cfg, speak
        self.items: asyncio.Queue = asyncio.Queue()
        self._turn: int | None = None
        self._round: int | None = None
        self._streamer: TextStreamer | None = None
        self._open_tools: list[tuple[str, int]] = []

    # -- turn (event loop side) ------------------------------------------------------------

    def open_turn(self, turn: int, language: str, allowed: tuple[str, ...], *,
                  disclosed: tuple[str, ...] = ()) -> None:
        self._turn, self._round = turn, None
        self._open_tools = []
        self._streamer = TextStreamer(self.speak, language, tuple(allowed), turn, tuple(disclosed),
                                      self.cfg.hold_unknown_tag)

    def close_turn(self) -> list[AgentSentence]:
        """Ends the turn: every later callback is dropped; returns the
        sentences already emitted (for reconciliation). Text still held
        (an unspeakable tag) is not emitted: the reconciliation decides."""
        streamer = self._streamer
        if streamer is not None and not streamer.held:
            self._put(streamer.end_round("end_turn", self.clock.now_ms()))
        self._finish_tools(None)
        self._turn, self._round, self._streamer = None, None, None
        return list(streamer.emitted) if streamer else []

    @property
    def streamer(self) -> TextStreamer | None:
        return self._streamer

    # -- StreamSink (worker thread side) ---------------------------------------------------

    def round_started(self, round_id: int) -> None:
        self._hop(self._on_round, round_id, False)

    def round_restarted(self, round_id: int) -> None:
        self._hop(self._on_round, round_id, True)

    def block_started(self, round_id: int) -> None:
        self._hop(self._on_block, round_id)

    def text_delta(self, round_id: int, text: str) -> None:
        self._hop(self._on_text, round_id, text)

    def tool_started(self, round_id: int, name: str) -> None:
        self._hop(self._on_tool, round_id, name)

    def round_stopped(self, round_id: int, stop_reason: str) -> None:
        self._hop(self._on_stop, round_id, stop_reason)

    def _hop(self, fn, *args) -> None:
        turn = self._turn
        try:
            self.loop.call_soon_threadsafe(fn, turn, *args)
        except RuntimeError:
            pass                                # the loop is closed: the call is over

    # -- event loop side -------------------------------------------------------------------

    def _current(self, turn, round_id) -> bool:
        return self._streamer is not None and turn == self._turn and round_id == self._round

    def _on_round(self, turn, round_id, restarted) -> None:
        if self._streamer is None or turn != self._turn:
            return
        self._round = round_id
        now = self.clock.now_ms()
        if restarted:
            self._streamer.restart_round()
        else:
            self._finish_tools(None)
            self._put(self._streamer.start_block(now))

    def _on_block(self, turn, round_id) -> None:
        if self._current(turn, round_id):
            self._put(self._streamer.start_block(self.clock.now_ms()))

    def _on_text(self, turn, round_id, text) -> None:
        if self._current(turn, round_id):
            self._put(self._streamer.feed(text, self.clock.now_ms()))

    def _on_tool(self, turn, round_id, name) -> None:
        if not self._current(turn, round_id):
            return
        now = self.clock.now_ms()
        self._put(self._streamer.tool_started(now))
        if name and name not in NO_FILLER_TOOLS:
            self._open_tools.append((name, now))
            self._put([ToolStarted(now, self._turn, name)])

    def _on_stop(self, turn, round_id, stop_reason) -> None:
        if self._current(turn, round_id):
            self._put(self._streamer.end_round(stop_reason, self.clock.now_ms()))

    def _finish_tools(self, ok) -> None:
        now = self.clock.now_ms()
        tools, self._open_tools = self._open_tools, []
        self._put([ToolFinished(now, self._turn or 0, name, ok, now - started) for name, started in tools])

    def _put(self, items) -> None:
        for item in items:
            self.bus.publish(item)
            self.items.put_nowait(item)


def tool_events(turn: int, name: str, at_ms: int) -> ToolStarted:
    return ToolStarted(at_ms, turn, name)


def tool_finished(turn: int, name: str, started_ms: int, at_ms: int, ok: bool | None) -> ToolFinished:
    return ToolFinished(at_ms, turn, name, ok, at_ms - started_ms)


__all__ = ["StreamSink", "StreamingAgentModel", "StreamBridge", "TextStreamer", "reconcile", "split_sentences",
           "fixed_language", "with_cache_points", "NO_FILLER_TOOLS"]

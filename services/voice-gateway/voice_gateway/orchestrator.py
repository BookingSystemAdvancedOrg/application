"""CallOrchestrator: the call state machine. Owner: builder 'brain'.

States (events.CallState) and transitions:

  CONNECTING --accept, brain.start--> GREETING (refused: say the refusal,
      ENDING; not a Booqy number: hang up at once)
  GREETING  --greeting played--> LISTENING (brain.greeting_played)
            --BargeIn before the disclosure sentence ended--> flush,
              brain.greeting_interrupted, its short sentence is said
              before the next reply (art. 50), LISTENING; the evidence
              (brain.greeting_played) is recorded once that short
              sentence played
            --BargeIn after the disclosure sentence--> flush,
              brain.greeting_played, LISTENING
            --TurnEnd (the caller talked through the greeting)--> the
              greeting counts as played when its disclosure sentence did,
              THINKING
  LISTENING --TurnEnd--> THINKING (language decision applied first:
              SWITCH -> brain.set_language + speaker language, the short
              disclosure in the new language is said at once;
              ASK/REPEAT/UNSUPPORTED/CALLBACK -> handled by the brain turn
              with the shared copy, DESIGN.md 6.4)
            --no speech for still_there_after_ms--> say STILL_THERE (twice,
              then ENDING "still_there"; copy has no separate goodbye
              sentence, so the line is hung up after the second wait)
  THINKING  --first AgentSentence queued and playing--> SPEAKING
            --caller speaks again within continuation_grace_ms of the
              TurnEnd and before any audio--> the reply is NOT played;
              when the turn finishes, brain.mark_not_heard([]) and the new
              utterance is the next turn (nothing is lost, nothing is said
              twice). A TurnEnd while a turn still runs waits for it (one
              turn at a time on the call row).
            --BargeIn (over a filler)--> as the continuation above
  SPEAKING  --AgentTurnDone and the queue drained--> LISTENING, with
              AssistantContext published (turn detector and backchannels)
            --BargeIn--> speaker.flush (target under 300 ms), unplayed or
              partly played sentences -> brain.mark_not_heard(heard),
              LISTENING (the caller's speech continues as the next turn);
              the interrupted reply's provider actions are dropped (the
              caller did not hear the goodbye or the transfer notice)
            --action end_call--> ENDING after the goodbye drained
            --action transfer_to_number--> TRANSFERRING after the
              TRANSFERRING sentence drained
  TRANSFERRING --adapter.transfer answered--> ENDED (brain.finish transferred)
               --not answered--> say TRANSFER_UNANSWERED, LISTENING
  ENDING    --drained--> adapter.hangup, brain.finish, ENDED
  any       --caller hung up (adapter.closed, caller audio ended)-->
              brain.finish("hangup") after a running turn saved, ENDED
  any       --listener, speaker or brain error--> fixed error sentence, ENDING

Tasks per call: Listener.run, Speaker.run, a 100 ms ticker (still-there
timer, hang-up check), the brain turn consumer, and this loop, which owns
every state change. Bus events reach the loop through a synchronous
EventBus.listen handler that only enqueues (the bus never blocks), so the
loop sees events in publish order. Every task ends when the call ends; a
failing task ends the call with the error sentence (never silence) and is
logged with the call tag only.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field

from voice_gateway import languages, log
from voice_gateway.audio.adapter import CallInfo, TelephonyAdapter, TransferOutcome
from voice_gateway.backchannel.clips import ClipCache
from voice_gateway.brain.streaming import fixed_language
from voice_gateway.clock import Clock
from voice_gateway.config import GatewayConfig
from voice_gateway.events import (AgentSentence, AgentTurnDone, AssistantContext, BargeIn, CallEnded, CallState,
                                  EventBus, LanguageAction, PlaybackFinished, PlaybackStarted, ProviderAction,
                                  StateChanged, SttFinal, SttPartial, TurnEnd, VadPause, VadStart)
from voice_gateway.listen.transcribe import SttFactory
from voice_gateway.metrics import CallMetrics, MetricsRecorder
from voice_gateway.speak.openers import OpenerPicker
from voice_gateway.speak.tts import Tts

TICK_MS = 100
DRAIN_TIMEOUT_MS = 30000
FIXED_INDEX = 900                  # item index of the gateway's own sentences (the brain's count from 0)
TURN_SAVE_WAIT_MS = 12000          # a running turn may finish its writes before the call ends (agent budget 8 s)
FINISH_WAIT_MS = 30000             # brain.finish (call row, metering) after the line is down
TURN_MAX_MS = 25000                # a turn still running this long after the caller's turn end is stuck: error, end
CALL_MAX_MS = (900 + 60) * 1000    # hard cap above the largest maxCallSeconds (900): no line, stream or task outlives it
OPENER_DELAY_MS = 120              # the opener waits this long after the turn end: a caller who goes on gets none


@dataclass
class Components:
    """Everything a call needs that tests or the harness may replace."""
    stt: SttFactory
    tts: Tts
    clips: ClipCache
    clock: Clock
    brain_factory: object                  # (cfg, bus, clock) -> AgentBrain
    rng_seed: int | None = None
    record_events: bool = False            # bus.history on (harness)
    speaker_factory: object | None = None  # (cfg, adapter, tts, clips, bus, clock, language, rng_seed) -> Speaker
    listener_factory: object | None = None  # (cfg, adapter, stt, bus, clock, playback, caller_e164, names) -> Listener
    emit_metrics: bool = True              # EMF lines through log.metric


@dataclass
class CallSummary:
    call_id: str | None
    end_reason: str
    metrics: CallMetrics
    states: list[tuple[int, CallState]] = field(default_factory=list)
    events: list | None = None             # bus.history when record_events
    metrics_summary: dict = field(default_factory=dict)


@dataclass
class _Turn:
    """One caller turn and the reply to it."""
    gen: int
    end: TurnEnd
    task: asyncio.Task | None = None
    items: dict = field(default_factory=dict)        # item id -> sentence text, in say order
    played: set = field(default_factory=set)         # item ids that finished uninterrupted
    audio: bool = False                              # reply audio started
    done: AgentTurnDone | None = None
    interrupted: bool = False                        # barge-in: the rest is never said
    suppressed: bool = False                         # the caller kept talking before any audio
    heard: list | None = None                        # what was heard when interrupted
    played_until: int | None = None                  # when the reply's last played item ended on the line
    started_ms: int = 0                              # when the brain turn started (the watchdog's zero)

    @property
    def dropped(self) -> bool:
        return self.interrupted or self.suppressed


class _Msg:
    __slots__ = ("kind", "payload")

    def __init__(self, kind: str, payload=None) -> None:
        self.kind, self.payload = kind, payload


class CallOrchestrator:
    def __init__(self, cfg: GatewayConfig, adapter: TelephonyAdapter, components: Components) -> None:
        self.cfg, self.adapter, self.components = cfg, adapter, components
        self.bus = EventBus()
        self.state = CallState.CONNECTING
        self.clock: Clock = components.clock
        self.metrics = MetricsRecorder(self.bus, emit=components.emit_metrics)
        self.brain = None
        self.speaker = None
        self.listener = None
        self.start = None
        self.tag: str | None = None
        self._states: list[tuple[int, CallState]] = []
        self._inbox: asyncio.Queue | None = None
        self._turn: _Turn | None = None
        self._gen = 0
        self._pending: TurnEnd | None = None
        self._prefix: tuple[str, str] | None = None       # (text, language): said before the next reply
        self._prefix_item: str | None = None
        self._greeting_items: list[str] = []
        self._disclosure_item: str | None = None
        self._disclosure_heard = False
        self._greeting_done = False
        self._quiet_since = 0
        self._caller_active = False
        self._last_vad = 0
        self._still_there = 0
        self._language = cfg.language.start_language
        self._ending: str | None = None
        self._tasks: list[asyncio.Task] = []
        self._helpers: set[asyncio.Task] = set()
        self._fixed_seq = 0
        self._openers = OpenerPicker(random.Random(components.rng_seed), cfg.speak.opener_chance)
        self._ctx: AssistantContext | None = None

    # -- public ------------------------------------------------------------------------------

    async def run(self) -> CallSummary:
        if self.components.record_events:
            self.bus.history = []
        self._inbox = asyncio.Queue()
        self.bus.listen(self.metrics.on_event)
        self.bus.listen(self._on_bus, TurnEnd, BargeIn, VadStart, VadPause, PlaybackStarted, PlaybackFinished)
        reason = "error"
        try:
            info = await self.adapter.accept()
            self.tag = log.call_tag(info.call_ref)
            self.brain = self.components.brain_factory(self.cfg, self.bus, self.clock)
            self.start = await self.brain.start(info)
            if self.start.call_id is None:
                await self.adapter.hangup("not_a_booqy_number")
                reason = "not_a_booqy_number"
            else:
                reason = await self._call(info)
        except Exception as exc:   # noqa: BLE001  (logged with the tag only; the line is hung up)
            log.event("call_failed", level="error", call=self.tag, error=type(exc).__name__)
            reason = "error"
            await self._safe(self.adapter.hangup("error"))
            if self.brain is not None:
                await self._safe(self.brain.finish("error"))
        finally:
            for task in list(self._helpers):
                task.cancel()
        self._set(CallState.ENDED, reason)
        self.bus.publish(CallEnded(self.clock.now_ms(), reason))
        log.event("call_ended", call=self.tag, reason=reason, turns=len(self.metrics.call.turns))
        self.bus.close()
        return CallSummary(self.start.call_id if self.start else None, reason, self.metrics.call, list(self._states),
                           self.bus.history, self.metrics.summary())

    # -- the call ------------------------------------------------------------------------------

    def _make_speaker(self):
        c = self.components
        if c.speaker_factory is not None:
            return c.speaker_factory(self.cfg, self.adapter, c.tts, c.clips, self.bus, self.clock,
                                     self.start.language, c.rng_seed)
        from voice_gateway.speak.speaker import Speaker
        return Speaker(self.cfg, self.adapter, c.tts, c.clips, self.bus, self.clock, language=self.start.language,
                       rng_seed=c.rng_seed)

    def _make_listener(self, info: CallInfo):
        c = self.components
        playback = getattr(self.speaker, "mixer", None)
        if c.listener_factory is not None:
            return c.listener_factory(self.cfg, self.adapter, c.stt, self.bus, self.clock, playback, info.caller,
                                      self.start.names)
        from voice_gateway.listen.pipeline import Listener
        return Listener(self.cfg, self.adapter, c.stt, self.bus, self.clock, playback, caller_e164=info.caller,
                        names=self.start.names)

    async def _call(self, info: CallInfo) -> str:
        self._language = self.start.language
        self._call_started_ms = self.clock.now_ms()
        self.speaker = self._make_speaker()
        self.listener = self._make_listener(info)
        self.bus.listen(self._on_heard, SttPartial, SttFinal)      # after the listener's own handler: it has the words
        warm = getattr(self.components.tts, "warm", None)
        if warm is not None and self.cfg.brain.warm_connections:
            self._spawn(self._safe(warm(self._language)))
        self._tasks = [asyncio.create_task(self.speaker.run(), name="speaker"),
                       asyncio.create_task(self.listener.run(), name="listener"),
                       asyncio.create_task(self._ticker(), name="ticker")]
        for task in self._tasks:
            task.add_done_callback(lambda t: self._post("task_done", t))
        try:
            self._greet()
            if self.start.refused:
                return await self._end("refused", drain=True)
            return await self._loop()
        finally:
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _loop(self) -> str:
        while True:
            msg = await self._inbox.get()
            try:
                reason = await self._handle(msg)
            except Exception as exc:   # noqa: BLE001  (never silence: the error sentence, then the end)
                log.event("call_error", level="error", call=self.tag, state=self.state.value,
                          error=type(exc).__name__)
                reason = await self._fail()
            if reason:
                return reason

    async def _handle(self, msg: _Msg) -> str | None:
        kind, p = msg.kind, msg.payload
        if kind == "bus":
            return await self._on_event(p)
        if kind == "tick":
            return await self._on_tick()
        if kind == "turn_item":
            return await self._on_turn_item(*p)
        if kind == "turn_failed":
            turn, _ = p
            if turn is self._turn:
                log.event("turn_failed", level="error", call=self.tag, gen=turn.gen)
                return await self._fail()
            return None
        if kind == "drained":
            return await self._on_drained(p)
        if kind == "greeting_drained":
            return await self._greeting_finished()
        if kind == "task_done":
            return await self._on_task_done(p)
        return None

    # -- greeting ------------------------------------------------------------------------------

    def _greet(self) -> None:
        start = self.start
        self._set(CallState.ENDING if start.refused else CallState.GREETING, "refused" if start.refused else "")
        sentences = start.greeting_sentences or ((start.greeting,) if start.greeting else ())
        for i, sentence in enumerate(sentences):
            item = self.speaker.say(sentence, start.language, kind="fixed", turn=0, index=i)
            self._greeting_items.append(item)
            if i == start.disclosure_until or (start.disclosure_until < 0 and i == len(sentences) - 1):
                self._disclosure_item = item
        if not start.refused:
            self._spawn(self._drain_then("greeting_drained"))

    async def _greeting_finished(self) -> str | None:
        if self._greeting_done or self.state != CallState.GREETING:
            return None
        self._greeting_done = True
        await self.brain.greeting_played()
        self._listen("greeting_played")
        return None

    async def _greeting_barge_in(self, e: BargeIn) -> None:
        self._greeting_done = True
        await self.speaker.flush(e.at_ms)
        if self._disclosure_heard:
            await self.brain.greeting_played()
        else:
            said = await self.brain.greeting_interrupted()
            if said:
                self._prefix = (said, self._language)
        self.listener.turn_taken()
        self._listen("barge_in_greeting")
        self._caller_active = True

    # -- events --------------------------------------------------------------------------------

    def _on_bus(self, event) -> None:
        self._post("bus", event)

    def _on_heard(self, event) -> None:
        """Speculative availability (brain/prefetch.py): the caller's words so far, while they still speak."""
        prefetch = getattr(self.brain, "prefetch", None)
        heard = getattr(self.listener, "heard_so_far", None)
        if prefetch is None or heard is None or self.state in (CallState.ENDING, CallState.ENDED):
            return
        try:
            text = heard()
            if text:
                prefetch(text, self._language)
        except Exception as exc:   # noqa: BLE001  (a prefetch is only a head start)
            log.event("prefetch_failed", level="warning", call=self.tag, error=type(exc).__name__)

    def _post(self, kind: str, payload=None) -> None:
        if self._inbox is not None:
            self._inbox.put_nowait(_Msg(kind, payload))

    async def _on_event(self, e) -> str | None:
        if isinstance(e, PlaybackFinished):
            self._on_played(e)
            return None
        if isinstance(e, PlaybackStarted):
            turn = self._turn
            if turn is not None and e.item_id in turn.items and not turn.dropped:
                turn.audio = True
                if self.state == CallState.THINKING:
                    self._set(CallState.SPEAKING)
            return None
        if isinstance(e, VadStart):
            return await self._on_vad_start(e)
        if isinstance(e, VadPause):
            self._last_vad = e.at_ms
            return None
        if isinstance(e, BargeIn):
            return await self._on_barge_in(e)
        if isinstance(e, TurnEnd):
            return await self._on_turn_end(e)
        return None

    def _on_played(self, e: PlaybackFinished) -> None:
        if e.item_id == self._disclosure_item and not e.interrupted:
            self._disclosure_heard = True
        if e.item_id == self._prefix_item:
            self._prefix_item = None
            if not e.interrupted and self._greeting_done:
                self._spawn(self.brain.greeting_played(spoken=self._prefix_text))
        turn = self._turn
        if turn is not None and e.item_id in turn.items and not e.interrupted:
            turn.played.add(e.item_id)
            turn.played_until = max(turn.played_until or 0, e.at_ms)
        if not self.speaker_busy():
            self._quiet_since = max(self._quiet_since, e.at_ms)

    async def _on_vad_start(self, e: VadStart) -> str | None:
        self._caller_active, self._last_vad, self._still_there = True, e.at_ms, 0
        turn = self._turn
        if self.state == CallState.THINKING and turn is not None and not turn.audio and not turn.dropped \
                and e.speech_start_ms - turn.end.at_ms <= self.cfg.call.continuation_grace_ms:
            turn.suppressed = True
            if turn.items or self.speaker_busy():      # the reply, or the opener that went before it
                await self.speaker.flush(e.at_ms)
            log.event("reply_suppressed", call=self.tag, gen=turn.gen)
            self._listen("caller_continued")
            if turn.done is not None:
                await self._settle_dropped(turn)
        return None

    async def _on_barge_in(self, e: BargeIn) -> str | None:
        if not self.cfg.barge_in.enabled:
            return None
        if self.state == CallState.GREETING:
            await self._greeting_barge_in(e)
            return None
        turn = self._turn
        if self.state not in (CallState.SPEAKING, CallState.THINKING) or turn is None or turn.dropped:
            if self.state == CallState.LISTENING and self.speaker_busy():
                await self.speaker.flush(e.at_ms)      # STILL_THERE or TRANSFER_UNANSWERED talked over
                self.listener.turn_taken()
            return None
        await self.speaker.flush(e.at_ms)
        turn.interrupted = True
        turn.heard = [text for item, text in turn.items.items() if item in turn.played]
        self.listener.turn_taken()
        self._caller_active = True
        log.event("barge_in", call=self.tag, gen=turn.gen, heard=len(turn.heard), said=len(turn.items))
        self._listen("barge_in")
        if turn.done is not None:
            await self._settle_dropped(turn)
        return None

    async def _on_turn_end(self, e: TurnEnd) -> str | None:
        self._caller_active, self._still_there = False, 0
        if self.state in (CallState.ENDING, CallState.ENDED, CallState.TRANSFERRING):
            return None
        if self.state == CallState.GREETING:
            self._greeting_done = True
            if self._disclosure_heard:
                await self.brain.greeting_played()
        if self._turn is not None:
            self._pending = e             # one turn at a time: this one starts when the running one is done
            self.listener.turn_taken()
            return None
        await self._start_turn(e)
        return None

    # -- the reply -----------------------------------------------------------------------------

    async def _start_turn(self, e: TurnEnd) -> None:
        self.listener.turn_taken()
        await self._overlap_guard(e)
        decision = e.language_decision
        if decision is not None and decision.action == LanguageAction.SWITCH and decision.candidate:
            said = await self.brain.set_language(decision.candidate, e.at_ms, decision.reason)
            self._language = self.brain.language
            self.speaker.set_language(self._language)
            if said:
                self._say(said, decision.candidate, kind="fixed")
        self._gen += 1
        turn = _Turn(self._gen, e, started_ms=self.clock.now_ms())
        self._turn = turn
        self._set(CallState.THINKING, "turn_end")
        self._opener(turn)
        turn.task = asyncio.create_task(self._consume(turn, e.text, e.language or self._language,
                                                      languages.to_transcribe(e.language or self._language)))

    def _opener(self, turn: _Turn) -> None:
        """A short opener right at the turn end (speak/openers.py), never on a language switch (the short
        disclosure comes first), never when the caller's words point to another language, never while
        something else waits to be said first. It plays OPENER_DELAY_MS after the turn end unless the caller
        went on meanwhile or the reply is already there."""
        e = turn.end
        play = getattr(self.speaker, "opener", None)
        if play is None or not self.cfg.speak.opener or self._prefix or self.speaker_busy():
            return
        decision = e.language_decision
        if decision is not None and decision.action != LanguageAction.KEEP:
            return
        if not languages_same(e.language or self._language, self._language) \
                or getattr(self.speaker, "other_language_hint", False):
            return
        planned = getattr(self.brain, "lookup_planned", None)
        lookup = bool(planned(e.text, self._language)) if planned else False
        ctx = self._ctx
        key = self._openers.pick(e.text, self._language, lookup=lookup,
                                 awaiting_confirmation=bool(ctx and ctx.awaiting_confirmation),
                                 asked=bool(ctx and ctx.asked_question),
                                 available=getattr(self.components.clips, "has_opener", None))
        if key:
            self._spawn(self._play_opener(turn, play, key, lookup))

    async def _play_opener(self, turn: _Turn, play, key: str, lookup: bool) -> None:
        await self.clock.sleep_ms(OPENER_DELAY_MS)
        voiced = getattr(self.listener, "caller_voiced", None)
        if turn is not self._turn or turn.dropped or turn.audio or turn.items or self.speaker_busy() \
                or self._caller_active or (voiced is not None and voiced()):
            return
        if play(self._language, key, lookup=lookup):
            log.event("opener", call=self.tag, key=key, lookup=lookup)

    async def _consume(self, turn: _Turn, text: str, language: str, locale: str | None) -> None:
        try:
            async for item in self.brain.turn(text, language=language, asr_locale=locale):
                self._post("turn_item", (turn, item))
        except asyncio.CancelledError:
            raise
        except Exception as exc:   # noqa: BLE001
            self._post("turn_failed", (turn, exc))

    async def _on_turn_item(self, turn: _Turn, item) -> str | None:
        if isinstance(item, AgentSentence):
            if turn.dropped or turn is not self._turn:
                return None
            if self._prefix:
                text, lang = self._prefix
                self._prefix_text = text
                self._prefix_item = self._say(text, lang, kind="fixed")
                turn.items[self._prefix_item] = text
                self._prefix = None
            item_id = self.speaker.say(item.text, item.language, kind=item.kind, turn=item.turn, index=item.index)
            turn.items[item_id] = item.text
            return None
        if isinstance(item, AgentTurnDone):
            turn.done = item
            if turn.dropped:
                return await self._settle_dropped(turn)
            # The brain switched language: the caller is heard in it from now, not only after the reply played
            # (live 2026-11-20 a German "Ja, das stimmt" said over a 28 s German reply was transcribed on the
            # Swedish stream as "Ja, det stä..."). Speaker and listener follow at once; _on_drained finds it done.
            await self._after_language(item)
            if not turn.items and self._prefix:
                text, lang = self._prefix
                self._prefix_text = text
                self._prefix_item = self._say(text, lang, kind="fixed")
                self._prefix = None
            self._spawn(self._drain_then("drained", turn))
        return None

    async def _settle_dropped(self, turn: _Turn) -> None:
        """A reply the caller did not hear (all or part): the not-heard rule,
        then the caller's waiting turn, if any."""
        if turn is not self._turn:
            return None
        await self.brain.mark_not_heard(turn.heard or [])
        self._turn = None
        await self._after_language(turn.done)
        if self._pending is not None:
            pending, self._pending = self._pending, None
            await self._start_turn(pending)
        return None

    async def _on_drained(self, turn: _Turn) -> str | None:
        if turn is not self._turn or turn.dropped or turn.done is None:
            return None
        done = turn.done
        await self._after_language(done)
        actions = {a.name: a for a in done.actions}
        if "end_call" in actions:
            reason = "max_length" if actions["end_call"].arguments.get("reason") == "max_length" else "agent_end"
            return await self._end(reason, drain=True)
        if "transfer_to_number" in actions:
            reason = await self._transfer(actions["transfer_to_number"])
            if reason:
                return reason
        self._turn = None
        self._last_reply = turn
        ctx = AssistantContext(self.clock.now_ms(), done.asked_question, done.awaiting_confirmation, self._language,
                               question_topic(turn.items.values()) if done.asked_question else None)
        self._ctx = ctx
        self.bus.publish(ctx)
        self.listener.set_context(ctx)
        if self._pending is not None:
            pending, self._pending = self._pending, None
            await self._start_turn(pending)
            return None
        self._listen("reply_done")
        return None

    async def _overlap_guard(self, e: TurnEnd) -> None:
        """A caller turn that began while the previous reply (a read-back
        waiting for the yes) was still playing is not an answer to it: a
        "ja" said over the date or the time must not book. That reply's
        read-backs count as not heard (brain.mark_not_heard), so the model
        reads back again and needs a later clear yes. The caller may answer
        over the last overlap_tolerance_ms of the question."""
        last, self._last_reply = self._last_reply, None
        if last is None or last.dropped or last.done is None or not last.done.awaiting_confirmation:
            return
        if last.played_until is None or e.speech_start_ms >= last.played_until - self.cfg.call.overlap_tolerance_ms:
            return
        log.event("answer_overlapped", call=self.tag, gen=last.gen)
        await self.brain.mark_not_heard([text for item, text in last.items.items() if item in last.played])

    async def _after_language(self, done: AgentTurnDone | None) -> None:
        if done is None or not done.language or done.language == self._language:
            return
        self._language = done.language
        self.speaker.set_language(done.language)
        await self.listener.follow_language(done.language, self.clock.now_ms())

    async def _transfer(self, action: ProviderAction) -> str | None:
        message = action.arguments.get("client_message") or self.brain.fixed("transferring", self._language)
        self._say(message, fixed_language(message, self._language) or self._language, kind="fixed")
        await self.speaker.drain(DRAIN_TIMEOUT_MS)
        self._set(CallState.TRANSFERRING, "transfer")
        to = action.arguments.get("transfer_number")
        try:
            outcome = await self.adapter.transfer(to, self.cfg.call.transfer_timeout_s) if to \
                else TransferOutcome(False, "no_number")
        except Exception as exc:   # noqa: BLE001
            log.event("transfer_failed", level="warning", call=self.tag, error=type(exc).__name__)
            outcome = TransferOutcome(False, "error")
        said = await self.brain.transfer_result(outcome.answered)
        log.event("transfer", call=self.tag, answered=outcome.answered, reason=outcome.reason)
        if outcome.answered:
            return await self._end("transferred", drain=False, hangup=False)
        if said:
            self._say(said, fixed_language(said, self._language) or self._language, kind="fixed")
        return None

    # -- still there, hang-up, errors ----------------------------------------------------------

    async def _ticker(self) -> None:
        while True:
            await self.clock.sleep_ms(TICK_MS)
            self._post("tick")

    async def _on_tick(self) -> str | None:
        if self.adapter.closed:
            return await self._hung_up()
        now = self.clock.now_ms()
        if self._ending:
            return None
        if now - self._call_started_ms > CALL_MAX_MS:             # noise that never ends a turn cannot keep it open
            log.event("call_cap", call=self.tag)
            if self._turn is not None:
                self._turn.interrupted = True
            text, lang = self._fixed("max_length")
            self._say(text, lang, kind="fixed")
            return await self._end("max_length", drain=True)
        turn = self._turn
        if turn is not None and turn.done is None and now - turn.started_ms > TURN_MAX_MS:
            log.event("turn_stuck", level="error", call=self.tag, gen=turn.gen)
            return await self._fail()
        if self._caller_active and now - self._last_vad > self.cfg.turn.max_utterance_ms + self.cfg.turn.max_silence_ms:
            self._caller_active = False               # a VadStart that never became a turn (noise)
        if self.state != CallState.LISTENING or self._caller_active or self._turn is not None:
            return None
        if self.speaker_busy():
            self._quiet_since = now
            return None
        if now - self._quiet_since < self.cfg.call.still_there_after_ms:
            return None
        if self._still_there >= self.cfg.call.hangup_after_still_there:
            return await self._end("still_there", drain=True)
        self._still_there += 1
        text, lang = self._fixed("still_there")
        self._say(text, lang, kind="fixed")
        self._quiet_since = now
        log.event("still_there", call=self.tag, count=self._still_there)
        return None

    async def _on_task_done(self, task: asyncio.Task) -> str | None:
        if task.cancelled() or self._ending:
            return None
        exc = task.exception()
        if task.get_name() == "listener" and exc is None:
            return await self._hung_up()             # the caller's audio ended
        if exc is not None:
            log.event("task_failed", level="error", call=self.tag, task=task.get_name(), error=type(exc).__name__)
            if task.get_name() == "speaker":
                return await self._end("error", drain=False)
            return await self._fail()
        return None

    async def _fail(self) -> str:
        text, lang = self._fixed("error")
        if self._turn is not None:
            self._turn.interrupted = True            # nothing more of the failed reply is said
        try:
            self._say(text, lang, kind="fixed")
        except Exception:   # noqa: BLE001
            return await self._end("error", drain=False)
        return await self._end("error", drain=True)

    async def _hung_up(self) -> str:
        return await self._end("hangup", drain=False, hangup=False)

    async def _end(self, reason: str, *, drain: bool, hangup: bool = True) -> str:
        if self._ending:
            return self._ending
        self._ending = reason
        self._set(CallState.ENDING, reason)
        if drain and not self.adapter.closed:
            await self._safe(self.speaker.drain(DRAIN_TIMEOUT_MS))
        turn = self._turn
        if turn is not None and turn.task is not None and not turn.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(turn.task), TURN_SAVE_WAIT_MS / 1000)
            except Exception:   # noqa: BLE001  (the brain saves what it can; finish still runs)
                pass
        if hangup and not self.adapter.closed:
            await self._safe(self.adapter.hangup(reason))
        finish = "error" if reason == "error" else reason
        # bounded: a worker thread that never returns holds the brain's lock, and the call must still end
        await self._safe(asyncio.wait_for(self.brain.finish(finish), FINISH_WAIT_MS / 1000))
        return reason

    # -- helpers -------------------------------------------------------------------------------

    def _fixed(self, key: str) -> tuple[str, str]:
        text = self.brain.fixed(key, self._language)
        return text, fixed_language(text, self._language) or self._language

    def _say(self, text: str, language: str, *, kind: str = "fixed") -> str:
        """A sentence of the gateway's own (disclosure, still there, transfer, error). Its item id never collides
        with the brain's sentences of the same turn ("t<gen>-s<index>"): these count from FIXED_INDEX."""
        self._fixed_seq += 1
        return self.speaker.say(text, language, kind=kind, turn=self._gen, index=FIXED_INDEX + self._fixed_seq)

    def _listen(self, reason: str) -> None:
        self._quiet_since = self.clock.now_ms()
        self._set(CallState.LISTENING, reason)

    def speaker_busy(self) -> bool:
        try:
            return bool(self.speaker.busy)
        except Exception:   # noqa: BLE001
            return False

    def _set(self, new: CallState, reason: str = "") -> None:
        if new == self.state:
            return
        old, self.state = self.state, new
        now = self.clock.now_ms()
        self._states.append((now, new))
        self.bus.publish(StateChanged(now, old, new, reason))
        log.event("state", call=self.tag, old=old.value, new=new.value, reason=reason)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._helpers.add(task)
        task.add_done_callback(self._helpers.discard)
        return task

    async def _drain_then(self, kind: str, payload=None) -> None:
        await self.speaker.drain(DRAIN_TIMEOUT_MS)
        self._post(kind, payload)

    @staticmethod
    async def _safe(awaitable) -> None:
        try:
            await awaitable
        except Exception as exc:   # noqa: BLE001
            log.event("cleanup_failed", level="warning", error=type(exc).__name__)

    _prefix_text: str | None = None
    _last_reply: _Turn | None = None
    _call_started_ms: int = 0


def languages_same(a: str | None, b: str | None) -> bool:
    return bool(a and b) and a.split("-")[0].lower() == b.split("-")[0].lower()


_TOPICS = (("party", ("hur många", "how many", "wie viele", "antal", "personer blir", "number of guests")),
           ("time", ("vilken tid", "vilket klockslag", "what time", "which time", "um wie viel uhr", "när på")),
           ("date", ("vilken dag", "vilket datum", "which day", "what day", "which date", "welchen tag")),
           ("name", ("vilket namn", "ditt namn", "namn ska", "what name", "your name", "which name", "ihr name")))


def question_topic(sentences) -> str | None:
    """What the reply's last question asks for (listen/numbers.py reads a bare number by it)."""
    texts = [str(t) for t in sentences if str(t).strip()]
    last = texts[-1].lower() if texts else ""
    for topic, words in _TOPICS:
        if any(w in last for w in words):
            return topic
    return None

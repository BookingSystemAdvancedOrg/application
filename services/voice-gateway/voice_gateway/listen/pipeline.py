"""Listener: the async shell of everything in listen/. Owner: builder 'listen'.

    adapter.caller_frames()
        -> RingBuffer.push
        -> VadSegmenter.on_frame(playing=mixer.assistant_playing)   => VadStart/Pause/Resume
        -> Transcriber.feed                                          => SttPartial/SttFinal
        -> BargeInDetector.on_frame / on_partial                     => BargeIn
        -> TurnDetector.on_vad / on_partial / on_final / on_tick    => TurnHold/TurnEnd
    at TurnEnd: LanguageTracker.observe (with a probe when wants_probe)
        => LanguageEvidence + LanguageDecided; a SWITCH reopens the live
        stream in the new language with replay from the utterance start,
        and the TurnEnd carries the re-transcribed text of the new stream
        (waits at most 2.5 s for it, else the original text) plus the
        decision. When the text was already recognised in the new language
        (the identify-language stream of the first utterance, or the LID
        probe), nothing is replayed or waited for: the live stream just
        follows. Every event goes on the bus; the orchestrator consumes
        TurnEnd and BargeIn.

Turn ends are enriched in a task (probe, language decision, reopen) while
the frame loop keeps running, so barge-in and VAD never wait for them; two
safety rules guard the orchestrator: a TurnEnd made only of backchannel
words while the assistant is still speaking is swallowed (a "mm" mid
sentence is not an answer), and a real turn that began over the assistant
but never produced a BargeIn gets one just before its TurnEnd.

`playback` is the mixer's view (speak.mixer.OutputMixer implements
PlaybackView): whether assistant audio (not a backchannel) is on the
line, since when, and its level history for the echo gate.
"""

import asyncio
import dataclasses

from voice_gateway import languages, log
from voice_gateway.audio.adapter import TelephonyAdapter
from voice_gateway.audio.buffers import RingBuffer
from voice_gateway.audio.frames import Frame, rms_dbfs
from voice_gateway.clock import Clock
from voice_gateway.config import GatewayConfig
from voice_gateway.events import (AssistantContext, BargeIn, EventBus, LanguageAction, SttFinal, SttPartial, TurnEnd,
                                  TurnHold, VadStart)
from voice_gateway.listen.barge_in import BargeInDetector, is_backchannel
from voice_gateway.listen.language_id import LanguageTracker, UtteranceInfo, candidates_for, explicit_targets
from voice_gateway.listen.transcribe import SttFactory, SttUnavailable, Transcriber
from voice_gateway.listen.turn import TurnDetector
from voice_gateway.listen.vad import VadModel, VadSegmenter, make_vad

from typing import Protocol

NEW_STREAM_WAIT_MS = 2500        # how long a TurnEnd waits for the re-transcription after a switch
# Transcribe accepts replayed audio faster than real time but answers in real time (measured live 2026-11-20: a
# 5.0 s utterance fed at 3x or 6x gave its identify-language final 5.1 to 5.6 s after the feed began, plus 0.8 s to
# open the stream). A probe over a longer utterance cannot finish inside its 2.5 s budget and only delays the reply:
# it runs for short utterances only.
PROBE_MAX_UTTERANCE_MS = 1000


class PlaybackView(Protocol):
    @property
    def assistant_playing(self) -> bool: ...

    @property
    def assistant_item_started_ms(self) -> int | None: ...

    def playback_dbfs_at(self, at_ms: int) -> float: ...


def same_language(a: str | None, b: str | None) -> bool:
    """sv and sv-SE are the same language here (base codes compared)."""
    return bool(a and b) and a.split("-")[0].lower() == b.split("-")[0].lower()


class Listener:
    def __init__(self, cfg: GatewayConfig, adapter: TelephonyAdapter, stt: SttFactory, bus: EventBus, clock: Clock,
                 playback: PlaybackView, *, caller_e164: str | None, names: tuple[str, ...] = (),
                 vad_model: VadModel | None = None) -> None:
        self.cfg, self.adapter, self.stt, self.bus, self.clock = cfg, adapter, stt, bus, clock
        self.playback = playback
        self.caller_e164 = caller_e164
        start = cfg.language.start_language
        self.ring = RingBuffer(cfg.audio.ring_seconds * 1000, cfg.audio.frame_ms)
        self.vad = VadSegmenter(cfg.vad, vad_model or make_vad(cfg.vad))
        self.transcriber = Transcriber(stt, self.ring, cfg.stt, bus, clock)
        self.turn = TurnDetector(cfg.turn, cfg.stt, language=start)
        self.barge = BargeInDetector(cfg.barge_in, playback_dbfs_at=playback.playback_dbfs_at)
        self.tracker = LanguageTracker(cfg.language, spoken=languages.SPOKEN, names=names, current=start,
                                       caller_e164=caller_e164)
        self._first = True
        self._item_started: int | None = None
        self._turn_lid: tuple[tuple[str, float], ...] = ()
        self._turn_asr_lang: str | None = None
        self._turn_started_playing = False
        self._turn_barged = False
        self._turn_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._collect: dict | None = None
        self._failure: Exception | None = None      # SttUnavailable from a background task: run() raises it
        bus.listen(self._on_stt, SttPartial, SttFinal)

    # --- run -------------------------------------------------------------------------------------

    async def run(self) -> None:
        """Until the caller's audio ends. Opens the identify-language stream
        first (LanguageConfig.identify_first_utterance), then fixed streams."""
        lang = self.tracker.current
        warm = getattr(self.stt, "warm", None)
        if warm is not None:                                  # spare connections for a reopen or a probe
            warm_task = asyncio.create_task(warm(1))
            self._tasks.add(warm_task)
            warm_task.add_done_callback(self._tasks.discard)
        cands = candidates_for(self.caller_e164, self.cfg.language, current=lang)
        if self.cfg.language.identify_first_utterance and len(cands) >= 2:
            await self.transcriber.open_identify(cands, preferred=lang)
        else:
            await self.transcriber.open(lang)
        try:
            async for frame in self.adapter.caller_frames():
                await self._on_frame(frame)
        finally:
            for task in list(self._tasks):
                task.cancel()
            await self.transcriber.close()

    async def _on_frame(self, frame: Frame) -> None:
        if self._failure is not None:
            raise self._failure
        now = frame.end_ms
        self.ring.push(frame)
        playing = self.playback.assistant_playing
        started = self.playback.assistant_item_started_ms
        if started != self._item_started:
            self._item_started = started
            self.barge.reset()
        for ev in self.vad.on_frame(frame, playing=playing):
            if isinstance(ev, VadStart):
                self._turn_started_playing, self._turn_barged = ev.during_playback, False
            self.bus.publish(ev)
            self._emit(self.turn.on_vad(ev))
        await self.transcriber.feed(frame)
        barge = self.barge.on_frame(now, speech_prob=self.vad.last_prob,
                                    caller_dbfs=rms_dbfs(frame.pcm) if playing else -120.0,
                                    speaking=self.vad.speaking, assistant_playing=playing, item_started_ms=started)
        if barge:
            self._publish_barge(barge)
        self._emit(self.turn.on_tick(now))

    def _publish_barge(self, barge: BargeIn) -> None:
        self._turn_barged = True
        self.bus.publish(barge)

    def _emit(self, outs) -> None:
        for out in outs:
            if isinstance(out, TurnHold):
                self.bus.publish(out)
            elif isinstance(out, TurnEnd):
                speech_ms = self.vad.speech_ms
                task = asyncio.create_task(self._finish_turn(out, speech_ms))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    # --- speech to text events -------------------------------------------------------------------

    def _on_stt(self, ev) -> None:
        if isinstance(ev, SttPartial):
            barge = self.barge.on_partial(ev, assistant_playing=self.playback.assistant_playing)
            if barge:
                self._publish_barge(barge)
            self._emit(self.turn.on_partial(ev))
            return
        if ev.language_scores:
            self._turn_lid = ev.language_scores
        self._turn_asr_lang = ev.language
        col = self._collect
        if col is not None and ev.stream_id == col["sid"] and ev.text.strip():
            col["finals"].append(ev)
            if ev.end_ms >= col["eos"] - 400:
                col["event"].set()
        self._emit(self.turn.on_final(ev))

    # --- the end of a turn -----------------------------------------------------------------------

    async def _finish_turn(self, end: TurnEnd, speech_ms: int) -> None:
        async with self._turn_lock:
            try:
                await self._finish(end, speech_ms)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if isinstance(exc, SttUnavailable):
                    self._failure = exc
                    return
                log.event("turn_finish_failed", level="error", error=type(exc).__name__)
                self.bus.publish(end)                        # the caller must still be answered

    async def _finish(self, end: TurnEnd, speech_ms: int) -> None:
        lid, asr_lang = self._turn_lid, self._turn_asr_lang or self.tracker.current
        self._turn_lid = ()
        playing = self.playback.assistant_playing
        if self._turn_started_playing and not self._turn_barged:
            if playing and is_backchannel(end.text, asr_lang):
                log.event("turn_swallowed", reason="backchannel_over_assistant")
                self.turn_taken()
                return
            self._publish_barge(BargeIn(end.at_ms, end.speech_start_ms, "words"))
        utt = UtteranceInfo(end.at_ms, end.text, speech_ms, asr_lang, end.confidence, tuple(lid), self._first)
        cands = None if self.transcriber.identifying else self.tracker.wants_probe(utt)
        if cands and getattr(self.transcriber.factory, "answers_in_real_time", False) \
                and end.eos_ms - end.speech_start_ms > PROBE_MAX_UTTERANCE_MS:
            log.event("probe_skipped", reason="too_long", speech_ms=end.eos_ms - end.speech_start_ms)
            cands = None
        probe_text: tuple[str, str] | None = None
        if cands:
            ev = await self.transcriber.probe(cands, replay_from_ms=end.speech_start_ms)
            if ev is not None:
                self.bus.publish(ev)
                utt = dataclasses.replace(utt, lid_scores=self.transcriber.last_probe_scores)
                probe_text = self.transcriber.last_probe_text
                if probe_text and explicit_targets(probe_text[1]) and not explicit_targets(utt.text):
                    # the live stream heard the request in the wrong language ("vi kan ta det på svenska" on
                    # en-GB came back as nonsense, live 2026-11-20): the probe's words carry the request (rule 1)
                    utt = dataclasses.replace(utt, text=probe_text[1])
        for evidence in self.tracker.evidence(utt):
            self.bus.publish(evidence)
        decision = self.tracker.observe(utt)
        self.bus.publish(decision)
        self._first = False
        text = end.text
        if decision.action == LanguageAction.SWITCH and probe_text and same_language(probe_text[0], decision.current):
            asr_lang, text = decision.current, probe_text[1]      # the probe already heard it in the new language
        if decision.action == LanguageAction.SWITCH and not same_language(asr_lang, decision.current):
            self.turn.set_language(decision.current)
            text = await self._retranscribe(decision.current, end) or text
        elif decision.action == LanguageAction.SWITCH:
            # the text was already recognised in the new language (the identify-language stream of the first
            # utterance, or the probe): no replay to wait for, the live stream follows from here
            self.turn.set_language(decision.current)
            if self.transcriber.language != decision.current or self.transcriber.identifying:
                await self.transcriber.reopen(decision.current, self.transcriber.fed_end_ms)
        elif self.transcriber.identifying:
            await self.transcriber.reopen(self.tracker.current, self.transcriber.fed_end_ms)
        self.bus.publish(dataclasses.replace(end, text=text, language=decision.current, language_decision=decision))

    async def _retranscribe(self, language: str, end: TurnEnd) -> str | None:
        """Reopens the live stream in `language` with replay and waits (a bounded
        time) for its text of the utterance."""
        event = asyncio.Event()
        self._collect = {"sid": None, "finals": [], "event": event, "eos": end.eos_ms}
        try:
            sid = await self.transcriber.reopen(language, end.speech_start_ms)
            self._collect["sid"] = sid
            waiter = asyncio.ensure_future(event.wait())
            timer = asyncio.ensure_future(self.clock.sleep_ms(NEW_STREAM_WAIT_MS))
            await asyncio.wait({waiter, timer}, return_when=asyncio.FIRST_COMPLETED)
            waiter.cancel()
            timer.cancel()
            finals = list(self._collect["finals"])
        finally:
            self._collect = None
        return " ".join(f.text.strip() for f in finals).strip() or None

    # --- the orchestrator's calls ----------------------------------------------------------------

    def set_context(self, ctx: AssistantContext) -> None:
        self.turn.set_context(ctx)
        self.turn.set_language(ctx.language)

    async def follow_language(self, code: str, at_ms: int) -> None:
        """The brain switched language: tracker.follow + reopen the live stream."""
        decided = self.tracker.follow(code, at_ms)
        if decided is not None:
            self.bus.publish(decided)
        self.turn.set_language(code)
        if self.transcriber.language != code or self.transcriber.identifying:
            await self.transcriber.reopen(code, self.transcriber.fed_end_ms)

    def turn_taken(self) -> None:
        """The orchestrator acted on a TurnEnd or a BargeIn: reset VAD turn
        state, turn detector and barge-in detector."""
        self.vad.end_turn()
        self.turn.reset()
        self.barge.reset()
        self._turn_started_playing = self._turn_barged = False

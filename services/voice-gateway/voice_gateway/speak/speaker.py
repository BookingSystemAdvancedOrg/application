"""Speaker: the async shell of speak/ and backchannel/ that the
orchestrator drives. Owner: builder 'converse'.

Owns one SpeechQueue, one OutputMixer, one BackchannelController, one
FillerPolicy and the ClipCache reference. Subscribes to the bus for the
controller's and the filler policy's inputs, runs a 20 ms tick for both,
and forwards their requests to the mixer.

Bus handlers only record (they run inline in publish, also inside the
mixer's own publishes); every decision is taken in the tick, so nothing
re-enters the mixer from a publish. A backchannel the mixer accepted is
published on the bus (metrics: first backchannel, overlaps); a refused
one is handed back to the controller (note_dropped) and does not count.
"""

from __future__ import annotations

import asyncio
import random
from typing import Literal

from voice_gateway.audio.adapter import TelephonyAdapter
from voice_gateway.backchannel.clips import ClipCache
from voice_gateway.backchannel.controller import BackchannelController
from voice_gateway.clock import Clock
from voice_gateway.config import GatewayConfig
from voice_gateway.events import (AgentTurnDone, AssistantContext, BackchannelRequest, EventBus, LanguageDecided,
                                  LanguageEvidence,
                                  PlaybackFinished, PlaybackFlushed, PlaybackStarted, SttPartial, StateChanged,
                                  ToolFinished, ToolStarted, TurnEnd, TurnHold, VadPause, VadResume, VadStart)
from voice_gateway.speak.fillers import FillerPolicy
from voice_gateway.speak.mixer import OutputMixer
from voice_gateway.speak.queue import SpeechItem, SpeechQueue
from voice_gateway.speak.tts import Tts

_CONTROLLER_EVENTS = (VadStart, VadPause, VadResume, SttPartial, TurnHold, TurnEnd, PlaybackStarted,
                      PlaybackFinished, PlaybackFlushed, StateChanged, AssistantContext, LanguageDecided)


class Speaker:
    def __init__(self, cfg: GatewayConfig, adapter: TelephonyAdapter, tts: Tts, clips: ClipCache, bus: EventBus,
                 clock: Clock, *, language: str = "sv", rng_seed: int | None = None) -> None:
        self.cfg, self.adapter, self.tts, self.clips, self.bus, self.clock = cfg, adapter, tts, clips, bus, clock
        self.language = language
        self.queue = SpeechQueue(tts, cfg.speak, clock)
        self.mixer: OutputMixer = OutputMixer(cfg, adapter, self.queue, clips, bus, clock)
        self.backchannels = BackchannelController(cfg.backchannel, rng=random.Random(rng_seed), language=language,
                                                  available=clips.has_backchannel, duration=self._clip_ms)
        self.fillers = FillerPolicy(cfg.speak)
        self._requests: list[BackchannelRequest] = []
        # The caller's words point to another language than the call's (text hint before the TurnEnd): no filler
        # this turn. Live 2026-11-20 an English caller after the Swedish greeting heard a Swedish "one moment"
        # before the reply switched to English; silence is better than a filler in the wrong language.
        self._other_hint = False
        self._no_filler = False
        bus.listen(self._on_event)

    def _clip_ms(self, language: str, token: str) -> int | None:
        clip = self.clips.backchannel(language, token)
        return clip.duration_ms if clip is not None else None

    def _on_event(self, ev) -> None:
        if isinstance(ev, _CONTROLLER_EVENTS):
            self._requests += self.backchannels.on_event(ev)
        if isinstance(ev, ToolStarted):
            self.fillers.on_tool_started(ev)
        elif isinstance(ev, ToolFinished):
            self.fillers.on_tool_finished(ev.at_ms)
        elif isinstance(ev, AgentTurnDone):
            self.fillers.turn_done()
        elif isinstance(ev, LanguageEvidence) and ev.source == "hint":
            self._other_hint = bool(ev.code) and ev.code.split("-")[0] != self.language.split("-")[0]
        elif isinstance(ev, TurnEnd):
            self.fillers.new_turn()
            self._no_filler, self._other_hint = self._other_hint and ev.language == self.language, False
        elif isinstance(ev, PlaybackStarted) and ev.kind in ("assistant", "fixed"):
            self.fillers.on_assistant_audio(ev.at_ms)

    async def run(self) -> None:
        mixer = asyncio.get_running_loop().create_task(self.mixer.run())
        try:
            while not self.adapter.closed:
                self._tick(self.clock.now_ms())
                if mixer.done():
                    mixer.result()          # surfaces a mixer failure to the call's task group
                    return
                await self.clock.sleep_ms(self.cfg.audio.frame_ms)
        finally:
            mixer.cancel()

    def _tick(self, now: int) -> None:
        requests, self._requests = self._requests, []
        if not self.mixer.busy:
            requests += self.backchannels.on_tick(now)
        for req in requests:
            if self.mixer.request_backchannel(req):
                self.bus.publish(req)
            else:
                self.backchannels.note_dropped(req)
        index = None if self._no_filler else self.fillers.due(now, self.language, assistant_busy=self.mixer.busy)
        if index is not None and not self.mixer.request_filler(self.language, index):
            self.fillers.refused(self.language)

    def say(self, text: str, language: str, *, kind: Literal["assistant", "fixed"] = "assistant", turn: int = 0,
            index: int = 0) -> str:
        """Queues one sentence; returns its item id."""
        item_id = f"t{turn}-s{index}"
        self.queue.enqueue(SpeechItem(item_id, text, language, kind, turn))
        self.fillers.on_assistant_audio(self.clock.now_ms())
        return item_id

    def opener(self, language: str, key: str, *, lookup: bool = False) -> bool:
        """Plays the opener `key` now if the line is idle (True when it plays). A "check" opener says the lookup
        is being done: no filler repeats it this turn; any other counts as speech for the filler's wait."""
        if not self.mixer.request_opener(language, key):
            return False
        now = self.clock.now_ms()
        if lookup:
            self.fillers.played_this_turn = True
        self.fillers.on_assistant_audio(now)
        return True

    async def flush(self, barge_in_at_ms: int) -> PlaybackFlushed:
        return await self.mixer.flush(barge_in_at_ms)

    async def drain(self, timeout_ms: int = 30000) -> None:
        await self.mixer.drain(timeout_ms)

    @property
    def other_language_hint(self) -> bool:
        """The caller's words of the turn that just ended point to another language than the call's."""
        return self._no_filler

    def set_language(self, language: str) -> None:
        self.language = language
        self.backchannels.language = language

    @property
    def busy(self) -> bool:
        return self.mixer.busy

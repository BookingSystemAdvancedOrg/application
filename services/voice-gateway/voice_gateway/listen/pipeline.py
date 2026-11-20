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
        decision. Every event goes on the bus; the orchestrator consumes
        TurnEnd and BargeIn.

`playback` is the mixer's view (speak.mixer.OutputMixer implements
PlaybackView): whether assistant audio (not a backchannel) is on the
line, since when, and its level history for the echo gate.
"""

from __future__ import annotations

from typing import Protocol

from voice_gateway.audio.adapter import TelephonyAdapter
from voice_gateway.clock import Clock
from voice_gateway.config import GatewayConfig
from voice_gateway.events import AssistantContext, EventBus
from voice_gateway.listen.transcribe import SttFactory


class PlaybackView(Protocol):
    @property
    def assistant_playing(self) -> bool: ...

    @property
    def assistant_item_started_ms(self) -> int | None: ...

    def playback_dbfs_at(self, at_ms: int) -> float: ...


class Listener:
    def __init__(self, cfg: GatewayConfig, adapter: TelephonyAdapter, stt: SttFactory, bus: EventBus, clock: Clock,
                 playback: PlaybackView, *, caller_e164: str | None, names: tuple[str, ...] = ()) -> None:
        self.cfg, self.adapter, self.stt, self.bus, self.clock = cfg, adapter, stt, bus, clock
        self.playback = playback

    async def run(self) -> None:
        """Until the caller's audio ends. Opens the identify-language stream
        first (LanguageConfig.identify_first_utterance), then fixed streams."""
        raise NotImplementedError

    def set_context(self, ctx: AssistantContext) -> None:
        raise NotImplementedError

    async def follow_language(self, code: str, at_ms: int) -> None:
        """The brain switched language: tracker.follow + reopen the live stream."""
        raise NotImplementedError

    def turn_taken(self) -> None:
        """The orchestrator acted on a TurnEnd or a BargeIn: reset VAD turn
        state, turn detector and barge-in detector."""
        raise NotImplementedError

"""Speaker: the async shell of speak/ and backchannel/ that the
orchestrator drives. Owner: builder 'converse'.

Owns one SpeechQueue, one OutputMixer, one BackchannelController, one
FillerPolicy and the ClipCache reference. Subscribes to the bus for the
controller's and the filler policy's inputs, runs a 20 ms tick for both,
and forwards their requests to the mixer.
"""

from __future__ import annotations

from typing import Literal

from voice_gateway.audio.adapter import TelephonyAdapter
from voice_gateway.backchannel.clips import ClipCache
from voice_gateway.clock import Clock
from voice_gateway.config import GatewayConfig
from voice_gateway.events import EventBus, PlaybackFlushed
from voice_gateway.speak.mixer import OutputMixer
from voice_gateway.speak.tts import Tts


class Speaker:
    def __init__(self, cfg: GatewayConfig, adapter: TelephonyAdapter, tts: Tts, clips: ClipCache, bus: EventBus,
                 clock: Clock, *, language: str = "sv", rng_seed: int | None = None) -> None:
        self.cfg, self.adapter, self.tts, self.clips, self.bus, self.clock = cfg, adapter, tts, clips, bus, clock
        self.mixer: OutputMixer

    async def run(self) -> None:
        raise NotImplementedError

    def say(self, text: str, language: str, *, kind: Literal["assistant", "fixed"] = "assistant", turn: int = 0,
            index: int = 0) -> str:
        """Queues one sentence; returns its item id."""
        raise NotImplementedError

    async def flush(self, barge_in_at_ms: int) -> PlaybackFlushed:
        raise NotImplementedError

    async def drain(self, timeout_ms: int = 30000) -> None:
        raise NotImplementedError

    def set_language(self, language: str) -> None:
        raise NotImplementedError

    @property
    def busy(self) -> bool:
        raise NotImplementedError

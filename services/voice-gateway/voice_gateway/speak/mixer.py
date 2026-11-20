"""OutputMixer: the only writer to adapter.play(). Owner: builder 'converse'.

Three sources, one line, never overlapping:
  assistant  SpeechQueue.ready() audio (assistant sentences and fixed copy)
  filler     a ClipCache filler (FillerPolicy said so)
  backchannel a ClipCache backchannel (BackchannelRequest)
Rules:
  * at any moment at most one item plays; items are never mixed sample-wise;
  * a BackchannelRequest is dropped (not delayed) when anything else plays
    or is queued, or when the caller is speaking (the controller already
    checks, the mixer re-checks at play time);
  * assistant audio arriving while a backchannel plays waits at most
    SpeakConfig.backchannel_max_wait_ms, then the backchannel is cut at a
    frame border (the agent never waits on an "mm");
  * a filler is dropped when assistant audio is ready before it starts;
    once started it plays to its end (it is short), then assistant audio;
  * pacing: frames go to the adapter through audio.buffers.PlayoutBuffer
    at the clock's pace, AudioConfig.playout_lead_ms ahead;
  * flush(barge_in_at_ms): adapter.clear(), PlayoutBuffer.clear(),
    SpeechQueue.cancel(); publishes PlaybackFlushed with stop_ms and the
    unplayed item ids, and PlaybackFinished(interrupted=True) for the
    current item; must finish within 50 ms of being called;
  * publishes PlaybackStarted / PlaybackFinished per item and keeps a
    level history (dBFS per 20 ms of what was sent) for the echo gate.
Implements listen.pipeline.PlaybackView.
"""

from __future__ import annotations

from voice_gateway.audio.adapter import TelephonyAdapter
from voice_gateway.backchannel.clips import ClipCache
from voice_gateway.clock import Clock
from voice_gateway.config import GatewayConfig
from voice_gateway.events import BackchannelRequest, EventBus, PlaybackFlushed
from voice_gateway.speak.queue import SpeechQueue


class OutputMixer:
    def __init__(self, cfg: GatewayConfig, adapter: TelephonyAdapter, queue: SpeechQueue, clips: ClipCache,
                 bus: EventBus, clock: Clock) -> None:
        self.cfg, self.adapter, self.queue, self.clips, self.bus, self.clock = cfg, adapter, queue, clips, bus, clock

    async def run(self) -> None:
        """Plays until the call ends (cancel the task to stop)."""
        raise NotImplementedError

    def request_backchannel(self, req: BackchannelRequest) -> bool:
        """True when it will play now."""
        raise NotImplementedError

    def request_filler(self, language: str, index: int) -> bool:
        raise NotImplementedError

    async def flush(self, barge_in_at_ms: int) -> PlaybackFlushed:
        raise NotImplementedError

    async def drain(self, timeout_ms: int = 30000) -> None:
        """Waits until everything queued has played (goodbye before hang-up)."""
        raise NotImplementedError

    # -- listen.pipeline.PlaybackView ------------------------------------------------------
    @property
    def assistant_playing(self) -> bool:
        raise NotImplementedError

    @property
    def assistant_item_started_ms(self) -> int | None:
        raise NotImplementedError

    def playback_dbfs_at(self, at_ms: int) -> float:
        raise NotImplementedError

    @property
    def busy(self) -> bool:
        """Anything playing or queued (assistant, filler or backchannel)."""
        raise NotImplementedError

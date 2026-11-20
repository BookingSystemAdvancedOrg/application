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
    an opener (speak/openers.py, at the caller's turn end) is the same;
  * pacing: frames go to the adapter through audio.buffers.PlayoutBuffer
    at the clock's pace, AudioConfig.playout_lead_ms ahead;
  * flush(barge_in_at_ms): adapter.clear(), PlayoutBuffer.clear(),
    SpeechQueue.cancel(); publishes PlaybackFlushed with stop_ms and the
    unplayed item ids, and PlaybackFinished(interrupted=True) for the
    current item; must finish within 50 ms of being called;
  * publishes PlaybackStarted / PlaybackFinished per item and keeps a
    level history (dBFS per 20 ms of what was sent) for the echo gate.
Implements listen.pipeline.PlaybackView.

Implementation notes:
  * pacing is done here with the same rule as PlayoutBuffer (a planned
    play time per frame, frames released while play time < now + lead),
    kept private so the mixer does not wait on builder 'listen'; the line
    adapter queues frames back to back, so the planned time equals what
    LocalAdapter schedules;
  * a feeder task pulls SpeechQueue.ready() with back pressure (at most one
    complete sentence staged beyond the one playing), so the queue's
    synth-ahead window stays relative to playback; audio for an idle line
    is sent at once (no wait for the 20 ms tick: first audio counts);
  * PlaybackStarted.at_ms is the planned play time of the item's first
    frame (what the caller hears, up to playout_lead_ms after sending);
    duration_ms is the audio known at that moment (a streamed sentence:
    the chunks received so far). PlaybackFinished comes when the last
    frame's play time has passed;
  * unplayed_item_ids of a flush list assistant and fixed items only (the
    not-heard rule); backchannel and filler ids look like "bc:sv:mm#3".
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field

from voice_gateway import log
from voice_gateway.audio.adapter import TelephonyAdapter
from voice_gateway.audio.frames import Frame, rms_dbfs
from voice_gateway.backchannel.clips import ClipCache
from voice_gateway.clock import Clock
from voice_gateway.config import GatewayConfig
from voice_gateway.events import (BackchannelRequest, EventBus, PlaybackFinished, PlaybackFlushed, PlaybackStarted,
                                  TurnEnd, VadPause, VadResume, VadStart)
from voice_gateway.speak.queue import ReadyAudio, SpeechQueue

_FRAME_BYTES = 640
_LEVEL_HISTORY = 200            # 4 s of 20 ms levels


@dataclass
class _Item:
    item_id: str
    kind: str                   # assistant | fixed | filler | backchannel
    language: str
    frames: deque = field(default_factory=deque)
    fed_all: bool = False       # the last chunk arrived
    fed_ms: int = 0
    start_ms: int | None = None  # planned play time of the first frame
    sent_ms: int = 0
    started_published: bool = False

    def add(self, pcm: bytes) -> None:
        for i in range(0, len(pcm) - len(pcm) % 2, _FRAME_BYTES):
            chunk = pcm[i:i + _FRAME_BYTES]
            if len(chunk) < _FRAME_BYTES:
                chunk = chunk + bytes(_FRAME_BYTES - len(chunk))
            self.frames.append(chunk)
            self.fed_ms += 20

    @property
    def end_ms(self) -> int | None:
        return None if self.start_ms is None else self.start_ms + self.sent_ms

    @property
    def speech(self) -> bool:
        return self.kind in ("assistant", "fixed")


class OutputMixer:
    def __init__(self, cfg: GatewayConfig, adapter: TelephonyAdapter, queue: SpeechQueue, clips: ClipCache,
                 bus: EventBus, clock: Clock) -> None:
        self.cfg, self.adapter, self.queue, self.clips, self.bus, self.clock = cfg, adapter, queue, clips, bus, clock
        self.frame_ms = cfg.audio.frame_ms
        self.lead_ms = cfg.audio.playout_lead_ms
        self._current: _Item | None = None
        self._staged: deque[_Item] = deque()          # assistant items after the current one
        self._on_line: list[_Item] = []               # fully sent, still playing
        self._next_play_ms = 0
        self._levels: deque[tuple[int, float]] = deque(maxlen=_LEVEL_HISTORY)
        self._gen = 0
        self._room = asyncio.Event()
        self._holding: ReadyAudio | None = None       # the feeder's chunk waiting for room
        self._assistant_wait_since: int | None = None
        self._caller_speaking = False
        self._seq = 0
        self.overlaps = 0                              # must stay 0 (tests, metrics)
        self.cut_backchannels = 0
        self._pump_lock = asyncio.Lock()
        bus.listen(self._on_caller, VadStart, VadResume, VadPause, TurnEnd)

    def _on_caller(self, ev) -> None:
        self._caller_speaking = isinstance(ev, (VadStart, VadResume))

    # -- the loop -----------------------------------------------------------------------------
    async def run(self) -> None:
        """Plays until the call ends (cancel the task to stop)."""
        feeder = asyncio.get_running_loop().create_task(self._feeder())
        try:
            while not self.adapter.closed:
                await self._pump()
                await self.clock.sleep_ms(self.frame_ms)
        finally:
            feeder.cancel()

    async def _feeder(self) -> None:
        async for ra in self.queue.ready():
            gen = self._gen
            self._holding = ra
            while not self._has_room() and gen == self._gen:
                self._room.clear()
                await self._room.wait()
            self._holding = None
            if gen != self._gen:
                continue            # flushed while waiting: the flush reported it
            self._add_assistant(ra)
            if self._current is None and not self.adapter.closed:
                await self._pump()  # an idle line starts at once, not on the next tick

    def _has_room(self) -> bool:
        return sum(1 for p in self._staged if p.fed_all) < 1

    def _add_assistant(self, ra: ReadyAudio) -> None:
        last = self._staged[-1] if self._staged else self._current
        if last is not None and last.item_id == ra.item.item_id and last.speech and not last.fed_all:
            target = last
        else:
            target = _Item(ra.item.item_id, ra.item.kind, ra.item.language)
            self._staged.append(target)
        target.add(ra.pcm)
        if ra.last:
            target.fed_all = True

    def _assistant_ready(self) -> bool:
        return bool(self._staged and self._staged[0].frames)

    async def _pump(self) -> None:
        async with self._pump_lock:
            await self._pump_unlocked()

    async def _pump_unlocked(self) -> None:
        now = self.clock.now_ms()
        self._publish_finished(now)
        if self._next_play_ms < now:
            self._next_play_ms = now
        while self._next_play_ms < now + self.lead_ms:
            item = self._select(now)
            if item is None:
                break
            pcm = item.frames.popleft()
            at = self._next_play_ms
            if item.start_ms is None:
                item.start_ms = at
            if self._on_line and any(p.end_ms is not None and p.end_ms > at for p in self._on_line if p is not item):
                self.overlaps += 1      # never happens: frames are planned back to back
            gen = self._gen
            await self.adapter.play(Frame(pcm, at))
            if gen != self._gen:
                return              # flushed while the line took the frame: flush() owns the state now
            item.sent_ms += self.frame_ms
            self._levels.append((at, rms_dbfs(pcm)))
            self._next_play_ms = at + self.frame_ms
            if not item.started_published:
                item.started_published = True
                self.bus.publish(PlaybackStarted(at, item.item_id, item.kind, item.language,  # type: ignore[arg-type]
                                                 item.fed_ms))
            if not item.frames and item.fed_all:
                self._on_line.append(item)
                self._current = None
        self._publish_finished(now)

    def _select(self, now: int) -> _Item | None:
        cur = self._current
        if cur is not None and cur.kind == "backchannel" and self._assistant_ready():
            if self._assistant_wait_since is None:
                self._assistant_wait_since = now
            if now - self._assistant_wait_since >= self.cfg.speak.backchannel_max_wait_ms:
                self._finish(cur, interrupted=True, at=self._next_play_ms)
                self.cut_backchannels += 1
                self._current = cur = None
        if cur is not None:
            if cur.frames:
                return cur
            if not cur.fed_all:
                return None         # a streamed sentence waits for its next chunk
            self._current = cur = None
        self._assistant_wait_since = None
        while self._staged:
            nxt = self._staged[0]
            if not nxt.frames:
                if nxt.fed_all:     # an item with no audio at all: skip it silently
                    self._staged.popleft()
                    self._room.set()
                    continue
                return None         # first chunk not here yet
            self._current = self._staged.popleft()
            self._room.set()
            return self._current
        return None

    def _publish_finished(self, now: int) -> None:
        for item in list(self._on_line):
            if item.end_ms is not None and item.end_ms <= now:
                self._on_line.remove(item)
                self._finish(item, interrupted=False, at=item.end_ms)

    def _finish(self, item: _Item, *, interrupted: bool, at: int, played_ms: int | None = None) -> None:
        if not item.started_published:
            return
        played = item.sent_ms if played_ms is None else played_ms
        self.bus.publish(PlaybackFinished(at, item.item_id, item.kind,  # type: ignore[arg-type]
                                          max(0, played), interrupted))

    # -- requests -----------------------------------------------------------------------------
    def request_backchannel(self, req: BackchannelRequest) -> bool:
        """True when it will play now."""
        if self.busy or self._caller_speaking or self.adapter.closed:
            return False
        clip = self.clips.backchannel(req.language, req.token)
        if clip is None:
            return False
        self._seq += 1
        item = _Item(f"bc:{req.language}:{req.token}#{self._seq}", "backchannel", req.language)
        item.add(clip.pcm)
        item.fed_all = True
        self._current = item
        return True

    def request_filler(self, language: str, index: int) -> bool:
        if self.busy or self.adapter.closed:
            return False
        clip = self.clips.filler(language, index)
        if clip is None:
            return False
        self._seq += 1
        item = _Item(f"filler:{language}:{index}#{self._seq}", "filler", language)
        item.add(clip.pcm)
        item.fed_all = True
        self._current = item
        return True

    def request_opener(self, language: str, key: str) -> bool:
        """A short opener at the caller's turn end (speak/openers.py): plays at once when the line is idle; the
        reply's audio follows it (like a filler, it is short and plays to its end)."""
        if self.busy or self.adapter.closed:
            return False
        clip = self.clips.opener(language, key)
        if clip is None:
            return False
        self._seq += 1
        item = _Item(f"opener:{language}:{key}#{self._seq}", "opener", language)
        item.add(clip.pcm)
        item.fed_all = True
        self._current = item
        return True

    async def flush(self, barge_in_at_ms: int) -> PlaybackFlushed:
        self._gen += 1
        now = self.clock.now_ms()
        dropped = await self.adapter.clear()
        now = max(now, self.clock.now_ms())
        unplayed: list[str] = [i.item_id for i in self.queue.cancel()]
        if self._holding is not None:
            unplayed.append(self._holding.item.item_id)
            self._holding = None
        for item in [*self._on_line, *([self._current] if self._current else []), *self._staged]:
            if not item.started_published or item.start_ms is None:
                if item.speech:
                    unplayed.append(item.item_id)
                continue
            heard = max(0, min(item.sent_ms, now - item.start_ms))
            complete = item.fed_all and not item.frames and heard >= item.sent_ms
            if not complete and item.speech:
                unplayed.append(item.item_id)
            self._finish(item, interrupted=not complete, at=now, played_ms=heard)
        self._on_line.clear()
        self._staged.clear()
        self._current = None
        self._assistant_wait_since = None
        self._next_play_ms = now
        self._room.set()
        ids = tuple(dict.fromkeys(unplayed))
        ev = PlaybackFlushed(now, dropped, max(0, now - barge_in_at_ms), ids)
        self.bus.publish(ev)
        log.event("playback_flushed", dropped_ms=dropped, stop_ms=ev.stop_ms, unplayed=len(ids))
        return ev

    async def drain(self, timeout_ms: int = 30000) -> None:
        """Waits until everything queued has played (goodbye before hang-up)."""
        deadline = self.clock.now_ms() + timeout_ms
        while self.busy and not self.adapter.closed:
            if self.clock.now_ms() >= deadline:
                log.event("drain_timeout", level="warning")
                return
            await self.clock.sleep_ms(self.frame_ms)

    # -- listen.pipeline.PlaybackView ------------------------------------------------------
    def _audible(self) -> list[_Item]:
        items = list(self._on_line)
        if self._current is not None and self._current.start_ms is not None:
            items.append(self._current)
        return items

    @property
    def assistant_playing(self) -> bool:
        return any(p.kind != "backchannel" for p in self._audible())

    @property
    def assistant_item_started_ms(self) -> int | None:
        starts = [p.start_ms for p in self._audible() if p.kind != "backchannel" and p.start_ms is not None]
        return min(starts) if starts else None

    def playback_dbfs_at(self, at_ms: int) -> float:
        for ts, level in reversed(self._levels):
            if ts <= at_ms < ts + self.frame_ms:
                return level
            if ts + self.frame_ms <= at_ms:
                break
        return -120.0

    @property
    def busy(self) -> bool:
        """Anything playing or queued (assistant, filler or backchannel)."""
        return bool(self._current or self._staged or self._on_line or self.queue.pending or self._holding)

    @property
    def assistant_busy(self) -> bool:
        """Assistant speech (not a backchannel) playing or queued."""
        on_line = [*self._on_line, *([self._current] if self._current else [])]
        return bool(self.queue.pending or self._staged or self._holding
                    or any(p.kind != "backchannel" for p in on_line))

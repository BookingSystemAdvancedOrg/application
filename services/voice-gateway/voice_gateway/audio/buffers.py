"""Jitter-safe buffers. Owner: builder 'listen'.

RingBuffer: the last AudioConfig.ring_seconds of caller audio, by call
clock time, for the Transcribe reopen with replay (language switch, LID
probe) and for the VAD pre-roll.

InboundJitterBuffer: for transports that may reorder or drop (a WebSocket
adapter): frames are released in timestamp order after `delay_ms`; a
missing frame is replaced by silence (never by a stall), late frames
are dropped and counted.

PlayoutBuffer: assistant audio to the adapter. Holds frames with their
planned play time, releases them `lead_ms` ahead of real time, and
`clear()` drops everything not yet released (barge-in). `played_until_ms`
is what the caller has heard so far (released minus the adapter's lead),
used for the not-heard decision and the echo reference.
"""

from __future__ import annotations

from collections import deque

from voice_gateway.audio.frames import CANONICAL_RATE, Frame, silence


class RingBuffer:
    def __init__(self, capacity_ms: int, frame_ms: int = 20) -> None:
        self.capacity_ms, self.frame_ms = capacity_ms, frame_ms
        self._frames: deque[Frame] = deque(maxlen=max(1, capacity_ms // frame_ms))

    def push(self, frame: Frame) -> None:
        self._frames.append(frame)

    def since(self, ts_ms: int) -> list[Frame]:
        """Frames whose end is after ts_ms, oldest first (may start earlier
        than ts_ms when the buffer does not reach that far back)."""
        return [f for f in self._frames if f.end_ms > ts_ms]

    def last(self, ms: int) -> list[Frame]:
        n = max(0, ms // self.frame_ms)
        if n == 0:
            return []
        frames = self._frames
        return list(frames)[-n:] if n < len(frames) else list(frames)

    @property
    def end_ms(self) -> int | None:
        return self._frames[-1].end_ms if self._frames else None

    @property
    def start_ms(self) -> int | None:
        return self._frames[0].ts_ms if self._frames else None

    def __len__(self) -> int:
        return len(self._frames)


class InboundJitterBuffer:
    def __init__(self, delay_ms: int, frame_ms: int = 20, sample_rate: int = CANONICAL_RATE) -> None:
        self.delay_ms, self.frame_ms, self.sample_rate = delay_ms, frame_ms, sample_rate
        self.late = 0
        self.concealed = 0
        self._base: int | None = None           # ts of frame index 0
        self._next = 0                          # next frame index to release
        self._pending: dict[int, Frame] = {}

    def _index(self, ts_ms: int) -> int:
        return round((ts_ms - self._base) / self.frame_ms)

    def push(self, frame: Frame) -> None:
        if self._base is None:
            self._base = frame.ts_ms
        idx = self._index(frame.ts_ms)
        if idx < self._next:
            self.late += 1
            return
        self._pending.setdefault(idx, frame)

    def pop_due(self, now_ms: int) -> list[Frame]:
        """Every frame due at now_ms, in order, gaps filled with silence."""
        if self._base is None:
            return []
        out: list[Frame] = []
        while self._base + self._next * self.frame_ms + self.delay_ms <= now_ms:
            ts = self._base + self._next * self.frame_ms
            frame = self._pending.pop(self._next, None)
            if frame is None:
                frame = silence(ts, self.sample_rate, self.frame_ms)
                self.concealed += 1
            out.append(frame)
            self._next += 1
        return out


class PlayoutBuffer:
    def __init__(self, lead_ms: int, frame_ms: int = 20) -> None:
        self.lead_ms, self.frame_ms = lead_ms, frame_ms
        self._queue: deque[tuple[Frame, str]] = deque()
        self._queued_ms = 0
        self._play_end = 0                      # line time when everything released has been played
        self._last_now = 0
        self._released: deque[tuple[int, int, str]] = deque()   # (start, end, item) still audible or recent

    def push(self, frame: Frame, item_id: str) -> None:
        """Queues a frame of item `item_id` right after the previous one."""
        self._queue.append((frame, item_id))
        self._queued_ms += frame.duration_ms

    def pop_due(self, now_ms: int) -> list[tuple[Frame, str]]:
        """Frames to hand to the adapter now (play time <= now + lead)."""
        self._last_now = now_ms
        out: list[tuple[Frame, str]] = []
        if self._queue and self._play_end < now_ms:
            self._play_end = now_ms             # idle or underrun: a new stream starts now
        while self._queue and self._play_end <= now_ms + self.lead_ms:
            frame, item = self._queue.popleft()
            dur = frame.duration_ms
            self._queued_ms -= dur
            self._released.append((self._play_end, self._play_end + dur, item))
            self._play_end += dur
            out.append((frame, item))
        while self._released and self._released[0][1] <= now_ms - 60000:
            self._released.popleft()
        return out

    def clear(self) -> tuple[int, tuple[str, ...]]:
        """Drops everything not released: (dropped ms, item ids with unplayed
        audio). The ids include items whose released audio the line has not
        played yet (the adapter's own clear() drops that part)."""
        dropped = self._queued_ms
        ids: list[str] = []
        for start, end, item in self._released:
            if end > self._last_now and item not in ids:
                ids.append(item)
        for _, item in self._queue:
            if item not in ids:
                ids.append(item)
        self._queue.clear()
        self._queued_ms = 0
        self._play_end = min(self._play_end, self._last_now)
        self._released = deque(r for r in self._released if r[1] <= self._last_now)
        return dropped, tuple(ids)

    def played_until_ms(self, now_ms: int) -> int:
        return min(now_ms, self._play_end)

    def item_played_ms(self, item_id: str, now_ms: int) -> int:
        """Milliseconds of `item_id` the caller has heard by now_ms."""
        return sum(max(0, min(end, now_ms) - start) for start, end, item in self._released if item == item_id)

    @property
    def queued_ms(self) -> int:
        return self._queued_ms

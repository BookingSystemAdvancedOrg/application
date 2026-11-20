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

from voice_gateway.audio.frames import Frame


class RingBuffer:
    def __init__(self, capacity_ms: int, frame_ms: int = 20) -> None:
        self.capacity_ms, self.frame_ms = capacity_ms, frame_ms
        self._frames: deque[Frame] = deque(maxlen=max(1, capacity_ms // frame_ms))

    def push(self, frame: Frame) -> None:
        raise NotImplementedError

    def since(self, ts_ms: int) -> list[Frame]:
        """Frames whose end is after ts_ms, oldest first (may start earlier
        than ts_ms when the buffer does not reach that far back)."""
        raise NotImplementedError

    def last(self, ms: int) -> list[Frame]:
        raise NotImplementedError

    @property
    def end_ms(self) -> int | None:
        raise NotImplementedError


class InboundJitterBuffer:
    def __init__(self, delay_ms: int, frame_ms: int = 20, sample_rate: int = 16000) -> None:
        self.delay_ms, self.frame_ms, self.sample_rate = delay_ms, frame_ms, sample_rate
        self.late = 0
        self.concealed = 0

    def push(self, frame: Frame) -> None:
        raise NotImplementedError

    def pop_due(self, now_ms: int) -> list[Frame]:
        """Every frame due at now_ms, in order, gaps filled with silence."""
        raise NotImplementedError


class PlayoutBuffer:
    def __init__(self, lead_ms: int, frame_ms: int = 20) -> None:
        self.lead_ms, self.frame_ms = lead_ms, frame_ms

    def push(self, frame: Frame, item_id: str) -> None:
        """Queues a frame of item `item_id` right after the previous one."""
        raise NotImplementedError

    def pop_due(self, now_ms: int) -> list[tuple[Frame, str]]:
        """Frames to hand to the adapter now (play time <= now + lead)."""
        raise NotImplementedError

    def clear(self) -> tuple[int, tuple[str, ...]]:
        """Drops everything not released: (dropped ms, item ids with unplayed audio)."""
        raise NotImplementedError

    def played_until_ms(self, now_ms: int) -> int:
        raise NotImplementedError

    @property
    def queued_ms(self) -> int:
        raise NotImplementedError

"""The call clock (FROZEN: shared by every builder, see DESIGN.md 3).

Every timestamp in the gateway is integer milliseconds on one call clock
(`Clock.now_ms()`), starting near 0 when the call is accepted. Pure cores
take `now_ms` as an argument; async shells take a `Clock`, so the replay
harness can run a whole call on `FakeClock` (deterministic, faster than
real time) or on `RealClock` (real-time pacing).
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int:
        """Milliseconds since the clock started."""

    async def sleep_ms(self, ms: int) -> None:
        """Waits `ms` on this clock (0 or less: yields once)."""

    async def sleep_until(self, at_ms: int) -> None:
        """Waits until now_ms() >= at_ms."""


class RealClock:
    """Monotonic wall clock, zero at construction."""

    def __init__(self) -> None:
        self._t0 = time.monotonic()

    def now_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    async def sleep_ms(self, ms: int) -> None:
        await asyncio.sleep(max(0, ms) / 1000)

    async def sleep_until(self, at_ms: int) -> None:
        await self.sleep_ms(at_ms - self.now_ms())


class FakeClock:
    """Virtual time for tests. Sleepers wake in deadline order when the test
    (or `run_until`) advances the clock; nothing waits in real time.

    `autoadvance=True`: whenever every task is blocked on this clock the
    clock jumps to the next deadline (call `drive()` as a background task),
    so a full call replays as fast as the CPU allows while keeping the
    exact timing of a real call."""

    def __init__(self, start_ms: int = 0) -> None:
        self._now = start_ms
        self._sleepers: list[tuple[int, int, asyncio.Future]] = []
        self._seq = itertools.count()

    def now_ms(self) -> int:
        return self._now

    async def sleep_ms(self, ms: int) -> None:
        await self.sleep_until(self._now + max(0, ms))

    async def sleep_until(self, at_ms: int) -> None:
        if at_ms <= self._now:
            await asyncio.sleep(0)
            return
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (at_ms, next(self._seq), fut))
        await fut

    def advance(self, ms: int) -> None:
        """Moves time forward by `ms` and wakes every sleeper that is due."""
        self.set(self._now + ms)

    def set(self, at_ms: int) -> None:
        self._now = max(self._now, at_ms)
        while self._sleepers and self._sleepers[0][0] <= self._now:
            _, _, fut = heapq.heappop(self._sleepers)
            if not fut.done():
                fut.set_result(None)

    def next_deadline(self) -> int | None:
        while self._sleepers and self._sleepers[0][2].done():
            heapq.heappop(self._sleepers)
        return self._sleepers[0][0] if self._sleepers else None

    async def drive(self, idle_rounds: int = 5) -> None:
        """Background task: when the loop is idle (every task sleeps on this
        clock), jump to the next deadline. Cancel it to stop."""
        while True:
            for _ in range(idle_rounds):
                await asyncio.sleep(0)
            nxt = self.next_deadline()
            if nxt is not None:
                self.set(nxt)

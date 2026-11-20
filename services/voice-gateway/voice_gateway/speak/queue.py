"""SpeechQueue: ordered assistant speech with synth-ahead. Owner: builder 'converse'.

enqueue(SpeechItem) in the order the brain produces sentences; at most
SpeakConfig.synth_ahead items are synthesised ahead of playback (TTS
latency overlaps playback of the previous sentence); the first item of a
turn is streamed (Tts.stream) so its audio starts with Polly's first
bytes. `ready()` yields (item, pcm chunks) strictly in enqueue order to
the mixer. cancel() (barge-in) drops every item not fully played, stops
pending TTS calls and returns the items whose audio the caller did not
hear completely (the orchestrator uses them for the not-heard rule).
A TTS failure of one item: retried tts_retry times, then the item is
replaced by the fixed error sentence of copy.ERROR in its language once
per turn (never silence, never another language).

Details the mixer relies on:
  * a streamed item yields its chunks with last=False and ends with an
    empty ReadyAudio(last=True); a synthesised item is one chunk, last=True;
  * an item that has no audio at all (no voice for its language, or the
    error sentence was already said this turn) yields one empty last chunk,
    so the order never stalls;
  * cancel() returns the items not yet fully handed to the mixer; the
    mixer adds the one it was playing.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import AsyncIterator, Literal

from voice_gateway import log
from voice_gateway.clock import Clock
from voice_gateway.config import SpeakConfig
from voice_gateway.speak.tts import NoVoice, Tts, TtsError

_END = object()
_CANCEL = object()


@dataclass(frozen=True)
class SpeechItem:
    item_id: str                    # "t{turn}-s{index}"
    text: str
    language: str
    kind: Literal["assistant", "fixed"] = "assistant"
    turn: int = 0


@dataclass(frozen=True)
class ReadyAudio:
    item: SpeechItem
    pcm: bytes                      # a chunk (whole frames); several chunks per item when streamed
    last: bool                      # the final chunk of the item


class _Entry:
    __slots__ = ("item", "stream", "q", "task", "cancelled", "enqueued_ms")

    def __init__(self, item: SpeechItem, stream: bool, enqueued_ms: int) -> None:
        self.item, self.stream, self.enqueued_ms = item, stream, enqueued_ms
        self.q: asyncio.Queue = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.cancelled = False


def error_sentence(language: str) -> str | None:
    """copy.ERROR in `language` only (never another language)."""
    from shared.features.phone_agent import copy as pa_copy
    return pa_copy.ERROR.get(str(language or "").split("-")[0])


class SpeechQueue:
    def __init__(self, tts: Tts, cfg: SpeakConfig, clock: Clock) -> None:
        self.tts, self.cfg, self.clock = tts, cfg, clock
        self._entries: deque[_Entry] = deque()
        self._new = asyncio.Event()
        self._last_turn: int | None = None
        self._error_turns: set[int] = set()
        self.failures = 0

    # -- producer side ------------------------------------------------------------------------
    def enqueue(self, item: SpeechItem) -> None:
        stream = item.turn != self._last_turn
        self._last_turn = item.turn
        self._entries.append(_Entry(item, stream, self.clock.now_ms()))
        self._schedule()
        self._new.set()

    def new_turn(self) -> None:
        """The next item streams even when it has the same turn number (a
        reply after a barge-in, a fixed sentence before the brain's)."""
        self._last_turn = None

    def _schedule(self) -> None:
        loop = asyncio.get_running_loop()
        for i, entry in enumerate(self._entries):
            if i > max(0, self.cfg.synth_ahead):
                break
            if entry.task is None:
                entry.task = loop.create_task(self._produce(entry))

    async def _produce(self, entry: _Entry) -> None:
        item, attempts = entry.item, 1 + max(0, self.cfg.tts_retry)
        try:
            if entry.stream:
                attempts -= 1
                got = False
                try:
                    async for chunk in self.tts.stream(item.text, item.language):
                        if chunk:
                            got = True
                            entry.q.put_nowait((chunk, False))
                    if got:
                        entry.q.put_nowait((b"", True))
                        return
                except (NoVoice, asyncio.CancelledError):
                    raise
                except Exception as exc:
                    if got:                 # the caller heard a part: end it there
                        log.event("tts_stream_cut", level="warning", language=item.language,
                                  error=type(exc).__name__)
                        entry.q.put_nowait((b"", True))
                        return
            error: Exception | None = None
            for _ in range(max(1, attempts)):
                try:
                    audio = await self.tts.synthesize(item.text, item.language)
                    entry.q.put_nowait((audio.pcm, True))
                    return
                except (NoVoice, asyncio.CancelledError):
                    raise
                except Exception as exc:
                    error = exc
            raise TtsError(type(error).__name__ if error else "failed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failures += 1
            log.event("tts_failed", level="error", language=item.language, kind=item.kind,
                      error=type(exc).__name__)
            entry.q.put_nowait((await self._replacement(item, exc), True))

    async def _replacement(self, item: SpeechItem, exc: Exception) -> bytes:
        """copy.ERROR in the item's language, once per turn; b"" otherwise."""
        if isinstance(exc, NoVoice) or item.turn in self._error_turns:
            return b""
        text = error_sentence(item.language)
        if not text:
            return b""
        self._error_turns.add(item.turn)
        for _ in range(1 + max(0, self.cfg.tts_retry)):
            try:
                return (await self.tts.synthesize(text, item.language)).pcm
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
        return b""

    # -- consumer side ------------------------------------------------------------------------
    async def ready(self) -> AsyncIterator[ReadyAudio]:
        while True:
            while not self._entries:
                self._new.clear()
                await self._new.wait()
            entry = self._entries[0]
            if entry.task is None:
                self._schedule()
            while True:
                got = await entry.q.get()
                if got is _CANCEL or entry.cancelled:
                    break
                pcm, last = got
                if last:                    # handed over in full: no longer pending
                    if self._entries and self._entries[0] is entry:
                        self._entries.popleft()
                    self._schedule()
                yield ReadyAudio(entry.item, pcm, last)
                if last or entry.cancelled:
                    break

    def cancel(self) -> list[SpeechItem]:
        items = []
        for entry in self._entries:
            items.append(entry.item)
            entry.cancelled = True
            if entry.task is not None and not entry.task.done():
                entry.task.cancel()
            entry.q.put_nowait(_CANCEL)
        self._entries.clear()
        self._last_turn = None
        return items

    @property
    def pending(self) -> int:
        """Items enqueued and not yet fully handed to the mixer."""
        return len(self._entries)

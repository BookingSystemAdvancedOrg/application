"""LocalAdapter: the development and test line. Owner: builder 'listen'.

Two transports, one class each, both implementing adapter.TelephonyAdapter:

LocalAdapter (in-process, used by the replay harness and unit tests):
  * the caller side is driven by the test through `CallerSide`:
    `say(pcm16)` queues caller audio (16 kHz PCM16) to start at the next
    frame, `silence_ms(ms)` waits, `hang_up()`. When nothing is queued the
    caller sends silence frames, so the gateway always sees a continuous
    20 ms stream, like a real line;
  * optional line simulation: `wire_format=TELEPHONY_MULAW_8K` runs every
    frame through mu-law 8 kHz and back (the harness uses it to make Polly
    audio sound like a phone line), `echo_db` mixes the assistant audio back
    into the caller side with `echo_delay_ms` (tests the echo gate),
    `noise_dbfs` adds noise;
  * `paced=True` releases caller frames at the clock's pace (RealClock:
    real time; FakeClock: virtual time, fast); assistant frames are
    "played" at the clock's pace too, so clear() drops what is not yet due;
  * records everything: `timeline` (list of LineEvent) and both tracks as
    a stereo WAV (left caller, right assistant) via `save(path)`;
  * transfer() and hangup() are recorded; transfer answers per
    `transfer_answers` (default True).

WebSocketAdapter (manual tests from a browser or a softphone bridge):
  a server on localhost speaking a small JSON protocol, one connection per
  call: {"event": "start", "callRef", "dialed", "caller", "format":
  "mulaw8k"|"pcm16k"}, {"event": "media", "seq", "payload": base64},
  {"event": "stop"}; the gateway sends {"event": "media", "payload"},
  {"event": "clear"}, {"event": "hangup"}, {"event": "transfer", "to"}.
  Inbound frames pass InboundJitterBuffer. Uses the `websockets` package
  (imported lazily). Binds 127.0.0.1 only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Literal

from voice_gateway.audio.adapter import CallInfo, TransferOutcome
from voice_gateway.audio.frames import PCM16_16K, AudioFormat, Frame
from voice_gateway.clock import Clock


@dataclass(frozen=True)
class LineEvent:
    at_ms: int
    kind: Literal["caller_audio_start", "caller_audio_end", "assistant_frame", "clear", "hangup", "transfer",
                  "caller_hangup"]
    detail: dict = field(default_factory=dict)


class CallerSide:
    """The test's handle on the caller (LocalAdapter.caller)."""

    async def say(self, pcm16: bytes, *, label: str = "") -> None:
        """Queues caller audio; returns when it has been fully sent."""
        raise NotImplementedError

    def say_nowait(self, pcm16: bytes, *, label: str = "") -> None:
        raise NotImplementedError

    async def silence_ms(self, ms: int) -> None:
        raise NotImplementedError

    async def wait_assistant_audio(self, min_ms: int = 1, timeout_ms: int = 15000) -> int:
        """Waits until the assistant has played at least min_ms in its
        current utterance; returns the clock time (for barge-in scenarios)."""
        raise NotImplementedError

    async def wait_assistant_quiet(self, quiet_ms: int = 700, timeout_ms: int = 30000) -> int:
        """Waits until the assistant has been silent for quiet_ms."""
        raise NotImplementedError

    async def hang_up(self) -> None:
        raise NotImplementedError


class LocalAdapter:
    def __init__(self, clock: Clock, *, call_ref: str = "local-1", dialed: str = "+46000000000",
                 caller: str | None = "+46700000000", wire_format: AudioFormat = PCM16_16K,
                 paced: bool = True, echo_db: float | None = None, echo_delay_ms: int = 120,
                 noise_dbfs: float | None = None, transfer_answers: bool = True) -> None:
        self.clock = clock
        self.info = CallInfo(call_ref, dialed, caller, wire_format)
        self.timeline: list[LineEvent] = []
        self.caller = CallerSide()

    async def accept(self) -> CallInfo:
        raise NotImplementedError

    def caller_frames(self) -> AsyncIterator[Frame]:
        raise NotImplementedError

    async def play(self, frame: Frame) -> None:
        raise NotImplementedError

    async def clear(self) -> int:
        raise NotImplementedError

    async def hangup(self, reason: str) -> None:
        raise NotImplementedError

    async def transfer(self, to_e164: str, timeout_s: int) -> TransferOutcome:
        raise NotImplementedError

    @property
    def closed(self) -> bool:
        raise NotImplementedError

    def assistant_pcm(self) -> bytes:
        """Everything the caller actually heard (cleared audio excluded)."""
        raise NotImplementedError

    def save(self, path: str) -> None:
        """Stereo WAV: left the caller, right the assistant."""
        raise NotImplementedError


class WebSocketAdapter:
    """See the module docstring. `serve()` yields one adapter per call."""

    def __init__(self, clock: Clock, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.clock, self.host, self.port = clock, host, port

    async def serve(self) -> AsyncIterator["WebSocketAdapter"]:
        raise NotImplementedError

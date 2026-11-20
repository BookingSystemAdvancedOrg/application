"""The telephony adapter port (FROZEN: the orchestrator, the mixer and the
harness all code against it). Owner of implementations: builder 'listen'.

An adapter is one call on one line. It converts the line's wire format to
canonical frames (frames.py) and back, and executes line actions. The real
AWS phone adapter comes later (DESIGN.md 10); LocalAdapter (local.py) is
the test and development line.

Timing contract:
  * caller_frames() yields one Frame per 20 ms of caller audio, stamped on
    the call clock, with no gaps (the adapter conceals loss with silence);
    it ends when the caller hangs up or hangup() was called;
  * play() accepts canonical frames; the adapter plays them in order and
    never more than AudioConfig.playout_lead_ms ahead of real time is
    expected to be buffered on the line (the mixer paces);
  * clear() drops every frame not yet played on the line and returns the
    milliseconds dropped; after it returns, nothing of the old audio plays.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol, runtime_checkable

from voice_gateway.audio.frames import AudioFormat, Frame


@dataclass(frozen=True)
class CallInfo:
    """`call_ref`: the line's id for the call (unique per call), prefixed by
    the brain with BrainConfig.call_ref_prefix. Phone numbers are E.164;
    `caller` is None for a hidden number. Never log the numbers."""
    call_ref: str
    dialed: str
    caller: str | None
    wire_format: AudioFormat


@dataclass(frozen=True)
class TransferOutcome:
    answered: bool
    reason: str = ""


@runtime_checkable
class TelephonyAdapter(Protocol):
    async def accept(self) -> CallInfo:
        """Waits for the call and answers it (audio flows after this)."""

    def caller_frames(self) -> AsyncIterator[Frame]:
        """Caller audio, canonical 20 ms frames, until the call ends."""

    async def play(self, frame: Frame) -> None:
        """Queues one canonical frame of assistant audio on the line."""

    async def clear(self) -> int:
        """Barge-in: drops queued, unplayed assistant audio; returns ms dropped."""

    async def hangup(self, reason: str) -> None:
        """Ends the call from our side (after the goodbye played)."""

    async def transfer(self, to_e164: str, timeout_s: int) -> TransferOutcome:
        """Connects the caller to staff. Returns when answered or given up."""

    @property
    def closed(self) -> bool:
        """True once the call is over (either side hung up)."""

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
    a stereo WAV (left caller, right assistant) via `save(path)`; both
    tracks are aligned on the call clock (silence where nothing played);
  * `label_at(ts_ms)` names the `say(label=)` audio playing at a call
    time (FakeSttFactory uses it to map caller audio to scripted text);
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

import asyncio
import base64
import json
from collections import deque
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal

from voice_gateway import log
from voice_gateway.audio import frames as fr
from voice_gateway.audio.adapter import CallInfo, TransferOutcome
from voice_gateway.audio.buffers import InboundJitterBuffer
from voice_gateway.audio.frames import PCM16_16K, AudioFormat, Frame
from voice_gateway.audio.wav import interleave, write_wav
from voice_gateway.clock import Clock

_BYTES_PER_MS = fr.CANONICAL_RATE // 1000 * fr.SAMPLE_BYTES        # 32


@dataclass(frozen=True)
class LineEvent:
    at_ms: int
    kind: Literal["caller_audio_start", "caller_audio_end", "assistant_frame", "clear", "hangup", "transfer",
                  "caller_hangup"]
    detail: dict = field(default_factory=dict)


class _Line:
    """Wire simulation: canonical PCM -> wire format -> canonical PCM."""

    def __init__(self, wire: AudioFormat) -> None:
        self.wire = wire
        self.active = wire != PCM16_16K
        self._down = self._up = None
        if self.active and wire.sample_rate != fr.CANONICAL_RATE:
            from voice_gateway.audio.resample import Resampler
            self._down = Resampler(fr.CANONICAL_RATE, wire.sample_rate)
            self._up = Resampler(wire.sample_rate, fr.CANONICAL_RATE)

    def roundtrip(self, pcm: bytes, ts_ms: int) -> bytes:
        if not self.active:
            return pcm
        frame = Frame(pcm, ts_ms)
        payload = fr.from_canonical(frame, self.wire, self._down)
        return fr.to_canonical(payload, self.wire, ts_ms, self._up).pcm


@dataclass
class _Segment:
    kind: Literal["audio", "silence", "hangup"]
    data: bytes = b""
    ms: int = 0
    label: str = ""
    done: asyncio.Future | None = None
    offset: int = 0                 # bytes of `data` (audio) or ms (silence) already sent


class CallerSide:
    """The test's handle on the caller (LocalAdapter.caller)."""

    def __init__(self, adapter: "LocalAdapter") -> None:
        self._a = adapter

    def _push(self, seg: _Segment) -> asyncio.Future:
        seg.done = asyncio.get_running_loop().create_future()
        if self._a._closed:
            seg.done.set_result(None)
        else:
            self._a._segments.append(seg)
            self._a._wake.set()
        return seg.done

    async def say(self, pcm16: bytes, *, label: str = "") -> None:
        """Queues caller audio; returns when it has been fully sent."""
        await self._push(_Segment("audio", data=pcm16, label=label))

    def say_nowait(self, pcm16: bytes, *, label: str = "") -> None:
        self._push(_Segment("audio", data=pcm16, label=label))

    async def silence_ms(self, ms: int) -> None:
        await self._push(_Segment("silence", ms=ms))

    async def wait_assistant_audio(self, min_ms: int = 1, timeout_ms: int = 15000) -> int:
        """Waits until the assistant has played at least min_ms in its
        current utterance; returns the clock time (for barge-in scenarios)."""
        a = self._a
        deadline = a.clock.now_ms() + timeout_ms
        while True:
            now = a.clock.now_ms()
            played = a._utterance_played_ms(now)
            if played >= min_ms:
                return now
            if a._closed or now >= deadline:
                raise TimeoutError(f"assistant audio {min_ms} ms not reached")
            wait = (min_ms - played) if a._playing(now) else 20
            await a.clock.sleep_ms(max(1, min(wait, deadline - now)))

    async def wait_assistant_quiet(self, quiet_ms: int = 700, timeout_ms: int = 30000) -> int:
        """Waits until the assistant has been silent for quiet_ms."""
        a = self._a
        deadline = a.clock.now_ms() + timeout_ms
        while True:
            now = a.clock.now_ms()
            if now >= a._line_end + quiet_ms:
                return now
            if a._closed or now >= deadline:
                raise TimeoutError(f"assistant not quiet for {quiet_ms} ms")
            await a.clock.sleep_ms(max(1, min(a._line_end + quiet_ms - now, deadline - now, 100)))

    async def hang_up(self) -> None:
        await self._push(_Segment("hangup"))


class LocalAdapter:
    def __init__(self, clock: Clock, *, call_ref: str = "local-1", dialed: str = "+46000000000",
                 caller: str | None = "+46700000000", wire_format: AudioFormat = PCM16_16K,
                 paced: bool = True, echo_db: float | None = None, echo_delay_ms: int = 120,
                 noise_dbfs: float | None = None, transfer_answers: bool = True, seed: int = 7) -> None:
        self.clock = clock
        self.info = CallInfo(call_ref, dialed, caller, wire_format)
        self.timeline: list[LineEvent] = []
        self.caller = CallerSide(self)
        self.paced, self.echo_db, self.echo_delay_ms = paced, echo_db, echo_delay_ms
        self.noise_dbfs, self.transfer_answers, self.seed = noise_dbfs, transfer_answers, seed
        self.hangup_reason: str | None = None
        self.transfers: list[tuple[str, int]] = []
        self._in_line, self._out_line = _Line(wire_format), _Line(wire_format)
        self._segments: deque[_Segment] = deque()
        self._cur: _Segment | None = None
        self._wake = asyncio.Event()
        self._closed = False
        self._call_start: int | None = None
        self._caller_track = bytearray()
        self._track = bytearray()               # what the caller heard, by call clock
        self._line_end = 0                      # call time when the scheduled assistant audio ends
        self._run_start: int | None = None      # start of the current contiguous assistant utterance
        self._labels: list[list] = []           # [start_ms, end_ms | None, label]
        self._rng = None
        self._pending_hangup = False

    # --- line ------------------------------------------------------------------------------

    async def accept(self) -> CallInfo:
        if self._call_start is None:
            self._call_start = self.clock.now_ms()
        return self.info

    @property
    def closed(self) -> bool:
        return self._closed

    def _close(self, kind: str | None = None, **detail) -> None:
        if kind and not self._closed:
            self.timeline.append(LineEvent(self.clock.now_ms(), kind, detail))   # type: ignore[arg-type]
        self._closed = True
        self._wake.set()
        for seg in [self._cur, *self._segments]:
            if seg is not None and seg.done is not None and not seg.done.done():
                seg.done.set_result(None)

    async def hangup(self, reason: str) -> None:
        self.hangup_reason = reason
        self._close("hangup", reason=reason)

    async def transfer(self, to_e164: str, timeout_s: int) -> TransferOutcome:
        self.transfers.append((to_e164, timeout_s))
        self.timeline.append(LineEvent(self.clock.now_ms(), "transfer", {"timeout_s": timeout_s}))
        await self.clock.sleep_ms(0)
        return TransferOutcome(self.transfer_answers, "" if self.transfer_answers else "no_answer")

    # --- assistant side --------------------------------------------------------------------

    async def play(self, frame: Frame) -> None:
        if self._closed:
            return
        now = self.clock.now_ms()
        start = max(self._line_end, now)
        if self._run_start is None or start > self._line_end:
            self._run_start = start
        pcm = self._out_line.roundtrip(frame.pcm, start)
        dur = len(pcm) // _BYTES_PER_MS
        offset = start * _BYTES_PER_MS
        if len(self._track) < offset + len(pcm):
            self._track.extend(bytes(offset + len(pcm) - len(self._track)))
        self._track[offset:offset + len(pcm)] = pcm
        self._line_end = start + dur
        self.timeline.append(LineEvent(start, "assistant_frame", {"ms": dur}))

    async def clear(self) -> int:
        now = self.clock.now_ms()
        dropped = max(0, self._line_end - now)
        if dropped:
            a = now * _BYTES_PER_MS
            self._track[a:a + dropped * _BYTES_PER_MS] = bytes(min(dropped * _BYTES_PER_MS, len(self._track) - a))
            self._line_end = now
        self._run_start = None
        self.timeline.append(LineEvent(now, "clear", {"dropped_ms": dropped}))
        return dropped

    def _playing(self, now: int) -> bool:
        return self._run_start is not None and self._run_start <= now < self._line_end

    def _utterance_played_ms(self, now: int) -> int:
        return now - self._run_start if self._playing(now) else 0

    def assistant_pcm(self) -> bytes:
        """Everything the caller actually heard (cleared audio excluded),
        aligned on the call clock (silence where nothing played)."""
        return bytes(self._track)

    def label_at(self, ts_ms: int) -> str | None:
        """The `say(label=)` of the caller audio playing at call time ts_ms."""
        for start, end, label in reversed(self._labels):
            if start <= ts_ms and (end is None or ts_ms < end):
                return label or None
        return None

    def save(self, path: str) -> None:
        """Stereo WAV: left the caller, right the assistant."""
        write_wav(path, interleave(bytes(self._caller_track), bytes(self._track)), fr.CANONICAL_RATE, 2)

    # --- caller side -----------------------------------------------------------------------

    def _noise(self, n_bytes: int) -> bytes:
        import numpy as np
        if self._rng is None:
            self._rng = np.random.default_rng(self.seed)
        sigma = 32768.0 * 10 ** (self.noise_dbfs / 20)
        x = self._rng.normal(0.0, sigma, n_bytes // 2)
        return np.clip(np.rint(x), -32768, 32767).astype("<i2").tobytes()

    def _mix(self, a: bytes, b: bytes) -> bytes:
        import numpy as np
        x = np.frombuffer(a, "<i2").astype(np.int32) + np.frombuffer(b, "<i2").astype(np.int32)
        return np.clip(x, -32768, 32767).astype("<i2").tobytes()

    def _echo(self, ts_ms: int, n_bytes: int) -> bytes:
        a = (ts_ms - self.echo_delay_ms) * _BYTES_PER_MS
        if a < 0:
            return bytes(n_bytes)
        chunk = bytes(self._track[a:a + n_bytes]).ljust(n_bytes, b"\0")
        return fr.gain(chunk, self.echo_db)

    def _take(self, ts_ms: int, size: int) -> tuple[bytes, list[_Segment]]:
        """The next `size` bytes of caller speech (zeros when idle) and the
        segments that finish inside this frame."""
        out = bytearray()
        finished: list[_Segment] = []
        while len(out) < size:
            if self._cur is None:
                if not self._segments:
                    out.extend(bytes(size - len(out)))
                    break
                self._cur = self._segments.popleft()
                if self._cur.kind == "audio":
                    self.timeline.append(LineEvent(ts_ms, "caller_audio_start", {"label": self._cur.label}))
                    self._labels.append([ts_ms, None, self._cur.label])
            seg = self._cur
            if seg.kind == "hangup":
                finished.append(seg)
                self._cur = None
                out.extend(bytes(size - len(out)))
                self._pending_hangup = True
                break
            want = size - len(out)
            if seg.kind == "audio":
                chunk = seg.data[seg.offset:seg.offset + want]
                seg.offset += len(chunk)
                out.extend(chunk)
                done = seg.offset >= len(seg.data)
            else:
                chunk_bytes = min(want, seg.ms * _BYTES_PER_MS - seg.offset)
                seg.offset += chunk_bytes
                out.extend(bytes(chunk_bytes))
                done = seg.offset >= seg.ms * _BYTES_PER_MS
            if done:
                if seg.kind == "audio":
                    end_ts = ts_ms + len(out) // _BYTES_PER_MS
                    self.timeline.append(LineEvent(end_ts, "caller_audio_end", {"label": seg.label}))
                    self._labels[-1][1] = end_ts
                finished.append(seg)
                self._cur = None
        return bytes(out), finished

    async def caller_frames(self) -> AsyncIterator[Frame]:
        if self._call_start is None:
            self._call_start = self.clock.now_ms()
        start, i, size = self._call_start, 0, fr.frame_bytes()
        self._pending_hangup = False
        while not self._closed:
            ts = start + i * fr.FRAME_MS
            if self.paced:
                await self.clock.sleep_until(ts + fr.FRAME_MS)
            elif self._cur is None and not self._segments:
                self._wake.clear()
                await self._wake.wait()
                if self._closed:
                    break
            if self._closed:
                break
            pcm, finished = self._take(ts, size)
            if self.noise_dbfs is not None:
                pcm = self._mix(pcm, self._noise(size))
            if self.echo_db is not None:
                pcm = self._mix(pcm, self._echo(ts, size))
            pcm = self._in_line.roundtrip(pcm, ts)
            if len(self._caller_track) < ts * _BYTES_PER_MS:
                self._caller_track.extend(bytes(ts * _BYTES_PER_MS - len(self._caller_track)))
            self._caller_track.extend(pcm)
            i += 1
            yield Frame(pcm, ts)
            for seg in finished:
                if seg.done is not None and not seg.done.done():
                    seg.done.set_result(None)
            if self._pending_hangup:
                self._close("caller_hangup")
                break
            if not self.paced:
                await asyncio.sleep(0)
        self._close()


class WebSocketAdapter:
    """See the module docstring. `serve()` yields one adapter per call.

    Messages from the client: {"event": "start", "callRef", "dialed",
    "caller", "format": "mulaw8k" | "pcm16k"}, {"event": "media", "seq",
    "payload": base64 audio}, {"event": "stop"}. To the client: media,
    clear, hangup, transfer (see the module docstring). Manual tests only:
    never exposed beyond localhost, never logs numbers."""

    def __init__(self, clock: Clock, host: str = "127.0.0.1", port: int = 8765) -> None:
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("the WebSocket test adapter binds localhost only")
        self.clock, self.host, self.port = clock, host, port

    async def serve(self) -> AsyncIterator["WebSocketCall"]:
        from websockets.asyncio.server import serve
        queue: asyncio.Queue = asyncio.Queue()

        async def handler(ws) -> None:
            try:
                start = json.loads(await ws.recv())
            except Exception:
                return
            if start.get("event") != "start":
                return
            call = WebSocketCall(self.clock, ws, start)
            await queue.put(call)
            await call._pump()

        async with serve(handler, self.host, self.port):
            self.bound_port = self.port
            while True:
                yield await queue.get()


class WebSocketCall:
    """One WebSocket connection as a TelephonyAdapter."""

    def __init__(self, clock: Clock, ws, start: dict) -> None:
        wire = AudioFormat("mulaw", 8000) if start.get("format", "mulaw8k") == "mulaw8k" else PCM16_16K
        self.clock, self._ws = clock, ws
        self.info = CallInfo(str(start.get("callRef") or "ws-1"), str(start.get("dialed") or ""),
                             start.get("caller") or None, wire)
        self._jitter = InboundJitterBuffer(60)
        self._in_rs = self._out_rs = None
        if wire.sample_rate != fr.CANONICAL_RATE:
            from voice_gateway.audio.resample import Resampler
            self._in_rs = Resampler(wire.sample_rate, fr.CANONICAL_RATE)
            self._out_rs = Resampler(fr.CANONICAL_RATE, wire.sample_rate)
        self._closed = False
        self._t0: int | None = None

    async def accept(self) -> CallInfo:
        self._t0 = self.clock.now_ms()
        return self.info

    @property
    def closed(self) -> bool:
        return self._closed

    async def _pump(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                if msg.get("event") == "media":
                    base = self._t0 if self._t0 is not None else self.clock.now_ms()
                    ts = base + int(msg.get("seq", 0)) * fr.FRAME_MS
                    payload = base64.b64decode(msg.get("payload", ""))
                    self._jitter.push(fr.to_canonical(payload, self.info.wire_format, ts, self._in_rs))
                elif msg.get("event") == "stop":
                    break
        except Exception as exc:                      # a dropped socket ends the call, nothing more
            log.event("ws_adapter_error", error=type(exc).__name__)
        finally:
            self._closed = True

    async def caller_frames(self) -> AsyncIterator[Frame]:
        while True:
            await self.clock.sleep_ms(fr.FRAME_MS)
            for frame in self._jitter.pop_due(self.clock.now_ms()):
                yield frame
            if self._closed and not self._jitter._pending:
                return

    async def _send(self, msg: dict) -> None:
        try:
            await self._ws.send(json.dumps(msg))
        except Exception:
            self._closed = True

    async def play(self, frame: Frame) -> None:
        payload = fr.from_canonical(frame, self.info.wire_format, self._out_rs)
        await self._send({"event": "media", "payload": base64.b64encode(payload).decode("ascii")})

    async def clear(self) -> int:
        await self._send({"event": "clear"})
        return 0

    async def hangup(self, reason: str) -> None:
        await self._send({"event": "hangup", "reason": reason})
        self._closed = True
        try:
            await self._ws.close()
        except Exception:
            pass

    async def transfer(self, to_e164: str, timeout_s: int) -> TransferOutcome:
        await self._send({"event": "transfer", "to": to_e164})
        return TransferOutcome(True)

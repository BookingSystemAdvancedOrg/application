"""Amazon Transcribe streaming. Owner: builder 'listen'.

SttStream: one open Transcribe stream (one language, or identify-language
over up to 5 candidates). TranscribeStream implements it with the
`amazon-transcribe` SDK (Apache 2.0, imported lazily) in SttConfig.region,
16 kHz PCM, partial results stabilisation SttConfig.partial_stability,
optional custom vocabulary. Results come back as SttPartial / SttFinal
(events.py), with audio times mapped to the call clock via the stream's
`audio_origin_ms` (the call time of the first frame fed).

Transcriber: the manager the Listener uses. Keeps exactly one LIVE stream
and at most one PROBE stream:
  * open(language) / open_identify(candidates): starts the live stream;
  * reopen(language, replay_from_ms): opens a new live stream in another
    language, feeds RingBuffer audio from replay_from_ms (paced at
    SttConfig.replay_speed) then live audio, closes the old one; results
    of the old stream after the switch are dropped (stream_id);
  * probe(candidates, replay_from_ms): a short identify-language stream on
    the buffered utterance only, returns LanguageEvidence (source
    "transcribe_lid") within about 2 s, then closes (rule 5 / hint
    disagreement); never replaces the live stream itself;
  * keepalive: silence when nothing was sent for idle_keepalive_ms;
  * a stream error reopens once in the same language with replay; a
    second error publishes nothing more and raises SttUnavailable (the
    orchestrator says the fixed error sentence and offers a callback).

Details worth knowing:
  * the SDK (0.6.4) drops the service's per-result LanguageIdentification
    list; `_install_lid_patch` keeps it (as `Result.language_identification`)
    so identify-language scores reach SttFinal.language_scores;
  * credentials: the CRT default chain (Fargate task role); with AWS_PROFILE
    set (a developer machine, SSO) boto3's resolver is used instead;
  * one amazon-transcribe client carries ONE active stream: a second
    start_stream_transcription on a client with a live stream hangs
    (verified live 2026-11-20), so TranscribeFactory keeps a small pool and
    gives every stream its own client; a client whose stream ended is reused
    at once (0.04 s instead of about 1 s for a cold connection), and
    `warm(n)` pre-connects spares so a reopen or a probe starts fast;
  * Transcribe streaming is not served in eu-north-1 (401 NotAuthorized),
    SttConfig.region is eu-central-1 (checked 2026-11-20);
  * audio goes out in 60 ms chunks; the events' at_ms is the arrival time
    on the call clock (stamped by the Transcriber), start_ms and end_ms the
    audio times;
  * a replay (reopen, probe) never sends frames the old stream has not
    been fed: the Transcriber tracks the last fed frame, frames arriving
    during a replay wait in a backlog that is flushed in order.

FakeSttFactory (tests, harness without AWS): returns scripted results.
The script maps caller audio segments (by `say(label=)`, through
`labeller`, usually LocalAdapter.label_at; without one, by the order of
the energy-detected speech segments) to text, language and scores; it
emits partials word by word at `words_per_s` and the final
`final_delay_ms` after the segment's end. Time is audio time (the frames
fed), so it needs no clock. A fixed-language stream that hears another
language's script line returns the text with confidence 0.3 (poor
recognition); an identify stream returns the scripted LID scores.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import re
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Protocol

from voice_gateway import languages, log
from voice_gateway.audio.buffers import RingBuffer
from voice_gateway.audio.frames import Frame, rms_dbfs, silence
from voice_gateway.clock import Clock
from voice_gateway.config import SttConfig
from voice_gateway.events import EventBus, LanguageEvidence, SttFinal, SttPartial

OPEN_TIMEOUT_S = 6                  # a stream that cannot be opened in this time counts as a failed open
CHUNK_MS = 60                       # audio sent per event (Transcribe advises 50 to 200 ms)


class SttUnavailable(Exception):
    pass


@dataclass(frozen=True)
class SttMode:
    language: str | None = None                     # our code; None with identify
    identify: tuple[str, ...] = ()                  # our codes, 2..5, identify-language
    preferred: str | None = None                    # identify: PreferredLanguage
    vocabulary_name: str | None = None


class SttStream(Protocol):
    stream_id: int
    mode: SttMode
    audio_origin_ms: int

    async def feed(self, frame: Frame) -> None: ...

    def results(self) -> AsyncIterator[SttPartial | SttFinal]: ...

    async def close(self) -> None: ...


class SttFactory(Protocol):
    async def open(self, mode: SttMode, *, stream_id: int, audio_origin_ms: int) -> SttStream: ...


# --- the real thing ----------------------------------------------------------------------------

_PATCHED = False


def _install_lid_patch() -> None:
    """Keeps LanguageIdentification (language code and score per result), which
    amazon-transcribe 0.6.4 does not parse, as Result.language_identification."""
    global _PATCHED
    if _PATCHED:
        return
    from amazon_transcribe.deserialize import TranscribeStreamingEventParser
    original = TranscribeStreamingEventParser._parse_result

    def parse_result(self, current_node):
        result = original(self, current_node)
        result.language_identification = tuple(
            (str(x.get("LanguageCode")), float(x.get("Score") or 0.0))
            for x in (current_node.get("LanguageIdentification") or []) if x.get("LanguageCode"))
        return result

    TranscribeStreamingEventParser._parse_result = parse_result
    _PATCHED = True


def _boto3_resolver():
    from amazon_transcribe.auth import Credentials, CredentialResolver

    class Boto3Resolver(CredentialResolver):
        def __init__(self) -> None:
            import boto3
            self._session = boto3.Session()

        async def get_credentials(self):
            def read():
                creds = self._session.get_credentials()
                if creds is None:
                    return None
                frozen = creds.get_frozen_credentials()
                return Credentials(frozen.access_key, frozen.secret_key, frozen.token)
            return await asyncio.to_thread(read)

    return Boto3Resolver()


_WORD_END = re.compile(r"^[.,!?;:]+$")


def _our_code(transcribe_code: str | None, fallback: str | None) -> str | None:
    return languages.to_code(transcribe_code) or fallback


def _convert(result, stream_id: int, mode: SttMode, origin_ms: int) -> SttPartial | SttFinal | None:
    """One Transcribe Result -> our event (None for an empty one)."""
    if not result.alternatives:
        return None
    alt = result.alternatives[0]
    text = (alt.transcript or "").strip()
    if not text:
        return None
    fallback = mode.language or mode.preferred or (mode.identify[0] if mode.identify else None) or "sv"
    lang = _our_code(getattr(result, "language_code", None), fallback) or fallback
    start = origin_ms + int(float(result.start_time or 0) * 1000)
    end = origin_ms + int(float(result.end_time or 0) * 1000)
    if result.is_partial:
        stable: list[str] = []
        for item in alt.items or []:
            if not item.stable:
                break
            piece = item.content or ""
            stable.append(piece if _WORD_END.match(piece) and stable else (" " if stable else "") + piece)
        return SttPartial(end, str(result.result_id), text, "".join(stable).strip(), lang, stream_id)
    confs = [float(i.confidence) for i in alt.items or [] if i.item_type == "pronunciation" and i.confidence is not None]
    scores: dict[str, float] = {}
    for code, score in getattr(result, "language_identification", ()) or ():
        ours = languages.to_code(code)
        if ours:
            scores[ours] = max(scores.get(ours, 0.0), score)
    ordered = tuple(sorted(scores.items(), key=lambda kv: -kv[1]))
    return SttFinal(end, str(result.result_id), text, lang, stream_id, start, end,
                    sum(confs) / len(confs) if confs else None, ordered)


class TranscribeStream:
    """One open Transcribe stream (SttStream)."""

    def __init__(self, client, cfg: SttConfig, mode: SttMode, stream_id: int, audio_origin_ms: int,
                 on_done: Callable[[], None] | None = None) -> None:
        self._client, self.cfg, self.mode = client, cfg, mode
        self._on_done = on_done
        self.stream_id, self.audio_origin_ms = stream_id, audio_origin_ms
        self._stream = None
        self._pending = bytearray()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._reader: asyncio.Task | None = None
        self._closed = False

    async def start(self) -> None:
        cfg, mode = self.cfg, self.mode
        kw = dict(media_sample_rate_hz=cfg.sample_rate, media_encoding="pcm",
                  enable_partial_results_stabilization=True, partial_results_stability=cfg.partial_stability)
        vocabulary = mode.vocabulary_name or cfg.vocabulary_name
        if vocabulary:
            kw["vocabulary_name"] = vocabulary
        if mode.identify:
            options = [languages.to_transcribe(c) for c in mode.identify]
            if len(options) < 2 or not all(options):
                raise ValueError("identify-language needs 2 to 5 languages Transcribe can hear")
            kw.update(language_code=None, identify_language=True, language_options=options)
            if mode.preferred and languages.to_transcribe(mode.preferred):
                kw["preferred_language"] = languages.to_transcribe(mode.preferred)
        else:
            code = languages.to_transcribe(mode.language or "")
            if not code:
                raise ValueError(f"Transcribe cannot hear {mode.language!r}")
            kw["language_code"] = code
        try:
            self._stream = await self._client.start_stream_transcription(**kw)
        except BaseException:
            self._done()
            raise
        self._reader = asyncio.create_task(self._read())

    def _done(self) -> None:
        callback, self._on_done = self._on_done, None
        if callback is not None:
            callback()

    async def _read(self) -> None:
        try:
            async for event in self._stream.output_stream:
                for result in event.transcript.results:
                    ev = _convert(result, self.stream_id, self.mode, self.audio_origin_ms)
                    if ev is not None:
                        self._queue.put_nowait(ev)
            self._queue.put_nowait(None)
        except asyncio.CancelledError:
            self._queue.put_nowait(None)
            raise
        except Exception as exc:
            self._queue.put_nowait(exc)
        finally:
            self._done()

    async def feed(self, frame: Frame) -> None:
        if self._closed:
            return
        self._pending.extend(frame.pcm)
        if len(self._pending) >= self.cfg.sample_rate * CHUNK_MS // 1000 * 2:
            chunk, self._pending = bytes(self._pending), bytearray()
            await self._stream.input_stream.send_audio_event(audio_chunk=chunk)

    async def results(self) -> AsyncIterator[SttPartial | SttFinal]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    async def close(self) -> None:
        """Ends the audio. The service then finalises what it has; the reader
        runs on for a few seconds (probe results) and is then cancelled."""
        if self._closed:
            return
        self._closed = True
        if self._reader is None:
            self._done()
            return
        try:
            if self._pending:
                await self._stream.input_stream.send_audio_event(audio_chunk=bytes(self._pending))
            await self._stream.input_stream.end_stream()
        except Exception:
            pass
        reader = self._reader
        if reader is not None:
            asyncio.get_running_loop().call_later(4.0, reader.cancel)


class _PooledClient:
    def __init__(self, client) -> None:
        self.client, self.busy = client, False


class TranscribeFactory:
    """Opens streams, one client per active stream (see the module docstring).
    `client_factory` (tests) replaces the amazon-transcribe client."""

    def __init__(self, cfg: SttConfig, client_factory: Callable[[], object] | None = None) -> None:
        self.cfg = cfg
        self._client_factory = client_factory
        self._pool: list[_PooledClient] = []

    def _new_client(self):
        if self._client_factory is not None:
            return self._client_factory()
        _install_lid_patch()
        from amazon_transcribe.client import TranscribeStreamingClient
        resolver = _boto3_resolver() if os.environ.get("AWS_PROFILE") else None
        return TranscribeStreamingClient(region=self.cfg.region, credential_resolver=resolver)

    def _checkout(self) -> _PooledClient:
        for pooled in self._pool:
            if not pooled.busy:
                pooled.busy = True
                return pooled
        pooled = _PooledClient(self._new_client())
        pooled.busy = True
        self._pool.append(pooled)
        return pooled

    async def open(self, mode: SttMode, *, stream_id: int, audio_origin_ms: int) -> SttStream:
        pooled = self._checkout()

        def release() -> None:
            pooled.busy = False

        stream = TranscribeStream(pooled.client, self.cfg, mode, stream_id, audio_origin_ms, release)
        await stream.start()
        return stream

    async def warm(self, spares: int = 2) -> None:
        """Connects `spares` clients (an empty stream each, no audio, nothing
        billed) so the first reopen or probe of a call does not pay for a
        cold connection. Failures are ignored: warming is an optimisation."""
        async def one() -> None:
            try:
                mode = SttMode(language="sv")
                stream = await asyncio.wait_for(self.open(mode, stream_id=0, audio_origin_ms=0), 8)
                await stream.close()
            except Exception as exc:
                log.event("stt_warm_failed", level="warning", error=type(exc).__name__)
        await asyncio.gather(*(one() for _ in range(spares)))


# --- the fake ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class ScriptedUtterance:
    """Harness script line: caller audio labelled `label` is heard as `text`."""
    label: str
    text: str
    language: str
    scores: tuple[tuple[str, float], ...] = ()
    confidence: float = 0.9
    duration_ms: int | None = None          # the labelled audio's length: words follow the audio (see _FakeStream)


@dataclass
class FakeSttFactory:
    script: list[ScriptedUtterance] = field(default_factory=list)
    words_per_s: float = 3.0
    final_delay_ms: int = 250
    opened: list[SttMode] = field(default_factory=list)
    labeller: Callable[[int], str | None] | None = None
    silence_gap_ms: int = 400               # label-less audio: this much silence ends a segment
    partial_lag_ms: int = 100               # a word shows this long after its audio (measured live: about 100 ms)
    partial_interval_ms: int = 480          # partials come in updates this far apart (measured live: 480 ms)
    voiced_dbfs: float = -45.0

    def __post_init__(self) -> None:
        self._order: dict[int, int] = {}    # label-less segment start -> script index (stable over replays)

    async def open(self, mode: SttMode, *, stream_id: int, audio_origin_ms: int) -> SttStream:
        self.opened.append(mode)
        return _FakeStream(self, mode, stream_id, audio_origin_ms)

    def line_for(self, label: str | None, seg_start: int) -> ScriptedUtterance | None:
        if label:
            return next((u for u in self.script if u.label == label), None)
        if seg_start not in self._order:
            self._order[seg_start] = len(self._order)
        idx = self._order[seg_start]
        return self.script[idx] if idx < len(self.script) else None


@dataclass
class _Seg:
    key: str
    line: ScriptedUtterance
    start: int
    last: int
    words: list[str]
    emitted: int = 0
    ended: bool = False
    done: bool = False


class _FakeStream:
    def __init__(self, factory: FakeSttFactory, mode: SttMode, stream_id: int, audio_origin_ms: int) -> None:
        self.f, self.mode, self.stream_id, self.audio_origin_ms = factory, mode, stream_id, audio_origin_ms
        self._queue: asyncio.Queue = asyncio.Queue()
        self._segs: dict[str, _Seg] = {}
        self._active: str | None = None
        self._voiced_end = -10**9
        self._energy_start: int | None = None
        self._closed = False
        self._seq = 0

    # --- feeding ---------------------------------------------------------------------------

    async def feed(self, frame: Frame) -> None:
        if self._closed:
            return
        label = self.f.labeller(frame.ts_ms) if self.f.labeller else None
        key = None
        if label:
            key = "L:" + label
        else:
            voiced = rms_dbfs(frame.pcm) > self.f.voiced_dbfs
            if voiced:
                if self._energy_start is None or frame.ts_ms - self._voiced_end > self.f.silence_gap_ms:
                    self._energy_start = frame.ts_ms
                self._voiced_end = frame.end_ms
            if self._energy_start is not None and frame.ts_ms - self._voiced_end <= self.f.silence_gap_ms:
                key = f"E:{self._energy_start}"
        if key and key != self._active and self._active in self._segs:
            self._segs[self._active].ended = True
        if key is None and self._active in self._segs:
            self._segs[self._active].ended = True
        self._active = key
        if key and key not in self._segs:
            line = self.f.line_for(label, self._energy_start if not label else frame.ts_ms)
            if line is not None:
                self._segs[key] = _Seg(key, line, frame.ts_ms, frame.end_ms, line.text.split())
        if key and key in self._segs:
            self._segs[key].last = frame.end_ms
        self._advance(frame.end_ms)

    def _advance(self, t: int, flush: bool = False) -> None:
        for seg in self._segs.values():
            if seg.done:
                continue
            n = len(seg.words)
            while seg.emitted < n:
                due = self._due(seg, seg.emitted)
                if seg.ended or flush:                        # the audio is over: the rest comes at once
                    seg.emitted = n
                    self._partial(seg, t)
                elif due <= t:
                    seg.emitted += 1
                    self._partial(seg, due)
                else:
                    break
            if seg.ended and seg.emitted >= n:
                final_at = seg.last + self.f.final_delay_ms
                if flush or t >= final_at:
                    seg.done = True
                    self._final(seg, final_at)

    def _due(self, seg: _Seg, index: int) -> int:
        """When word `index` shows in a partial. A line with duration_ms spreads its words over the audio by length
        (letters are a fair stand-in for syllables) and shows each one partial_lag_ms after its audio, on the next
        partial update, as Transcribe does: a partial never runs ahead of the speech. Without it, words_per_s."""
        if not seg.line.duration_ms:
            return seg.start + int((index + 1) * 1000 / self.f.words_per_s)
        total = sum(len(w) + 1 for w in seg.words)
        upto = sum(len(w) + 1 for w in seg.words[:index + 1])
        heard = int(seg.line.duration_ms * upto / max(1, total)) + self.f.partial_lag_ms
        step = max(1, self.f.partial_interval_ms)
        return seg.start + -(-heard // step) * step

    def _language(self, seg: _Seg) -> tuple[str, tuple[tuple[str, float], ...], float]:
        line, mode = seg.line, self.mode
        if mode.identify:
            if line.language in mode.identify:
                return line.language, line.scores or ((line.language, 0.95),), line.confidence
            lang = mode.preferred or mode.identify[0]
            return lang, ((lang, 0.4),), 0.4
        lang = mode.language or "sv"
        return lang, (), (line.confidence if line.language == lang else 0.3)

    def _partial(self, seg: _Seg, at: int) -> None:
        lang, _, _ = self._language(seg)
        text = " ".join(seg.words[:seg.emitted])
        stable = " ".join(seg.words[:max(0, seg.emitted - 1)])
        self._seq += 1
        self._queue.put_nowait(SttPartial(at, f"{self.stream_id}-{seg.key}", text, stable, lang, self.stream_id))

    def _final(self, seg: _Seg, at: int) -> None:
        lang, scores, conf = self._language(seg)
        self._queue.put_nowait(SttFinal(at, f"{self.stream_id}-{seg.key}", seg.line.text, lang, self.stream_id,
                                        seg.start, seg.last, conf, tuple(scores)))

    # --- the SttStream surface -------------------------------------------------------------

    async def results(self) -> AsyncIterator[SttPartial | SttFinal]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def close(self) -> None:
        if self._closed:
            return
        for seg in self._segs.values():
            seg.ended = True
        self._advance(max([s.last for s in self._segs.values()] + [0]) + self.f.final_delay_ms, flush=True)
        self._closed = True
        self._queue.put_nowait(None)


# --- the manager -------------------------------------------------------------------------------

class Transcriber:
    def __init__(self, factory: SttFactory, ring: RingBuffer, cfg: SttConfig, bus: EventBus, clock: Clock) -> None:
        self.factory, self.ring, self.cfg, self.bus, self.clock = factory, ring, cfg, bus, clock
        self.language: str | None = None
        self.last_probe_scores: tuple[tuple[str, float], ...] = ()
        self.last_probe_text: tuple[str, str] | None = None     # (language, text) the probe heard, see probe()
        self._live: SttStream | None = None
        self._live_id = 0
        self._mode: SttMode | None = None
        self._next_id = 1
        self._pump_task: asyncio.Task | None = None
        self._replay_task: asyncio.Task | None = None
        self._replaying = False
        self._backlog: list[Frame] = []
        self._fed_end = 0
        self._latest = 0
        self._last_sent = 0
        self._last_final_end: int | None = None
        self._errors = 0
        self._unavailable: SttUnavailable | None = None
        self._identifying = False
        self._background: set[asyncio.Task] = set()
        self._closed = False

    # --- helpers ---------------------------------------------------------------------------

    def _now(self) -> int:
        return max(self.clock.now_ms(), self._latest)

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id - 1

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def _open_stream(self, mode: SttMode, origin_ms: int) -> tuple[SttStream, int]:
        sid = self._new_id()
        last: Exception | None = None
        for attempt in range(2):
            try:
                return await asyncio.wait_for(self.factory.open(mode, stream_id=sid, audio_origin_ms=origin_ms),
                                              OPEN_TIMEOUT_S), sid
            except Exception as exc:
                last = exc
                log.event("stt_open_failed", level="warning", attempt=attempt, error=type(exc).__name__)
        raise SttUnavailable("could not open a Transcribe stream") from last

    def _activate(self, stream: SttStream, sid: int, mode: SttMode) -> None:
        self._live, self._live_id, self._mode = stream, sid, mode
        self.language = mode.language
        self._identifying = bool(mode.identify)
        if self._pump_task is not None:
            self._pump_task.cancel()
        self._pump_task = asyncio.create_task(self._pump(stream))

    async def _pump(self, stream: SttStream) -> None:
        try:
            async for ev in stream.results():
                if stream is not self._live:
                    continue
                if isinstance(ev, SttFinal):
                    self._last_final_end = ev.end_ms
                    self._errors = 0
                self.bus.publish(dataclasses.replace(ev, at_ms=self._now()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if stream is self._live and not self._closed:
                self._on_error(stream, exc)

    def _on_error(self, stream: SttStream, exc: Exception) -> None:
        if stream is not self._live or self._closed or self._unavailable is not None:
            return
        log.event("stt_stream_error", level="warning", error=type(exc).__name__, errors=self._errors)
        if self._errors >= 1:
            self._unavailable = SttUnavailable("Transcribe failed twice")
            return
        self._errors += 1
        self._spawn(self._recover())

    async def _recover(self) -> None:
        """One automatic reopen in the same mode with replay of what the dead
        stream may not have finalised."""
        mode = self._mode
        if mode is None:
            return
        floor = self._fed_end - self.cfg.max_reopen_replay_ms
        start = max(self._last_final_end if self._last_final_end is not None else floor, floor)
        try:
            await self._reopen_with(mode, start)
        except SttUnavailable as exc:
            self._unavailable = exc

    def _replay_frames(self, from_ms: int) -> list[Frame]:
        floor = self._fed_end - self.cfg.max_reopen_replay_ms
        return [f for f in self.ring.since(max(from_ms, floor)) if f.end_ms <= self._fed_end]

    # --- the live stream ---------------------------------------------------------------------

    async def open(self, language: str) -> None:
        await self._reopen_with(SttMode(language=language, vocabulary_name=self.cfg.vocabulary_name), self._fed_end,
                                replay=False)

    async def open_identify(self, candidates: tuple[str, ...], preferred: str | None = None) -> None:
        mode = SttMode(identify=tuple(candidates), preferred=preferred, vocabulary_name=self.cfg.vocabulary_name)
        await self._reopen_with(mode, self._fed_end, replay=False)

    async def reopen(self, language: str, replay_from_ms: int) -> int:
        """Opens a new live stream in `language`, feeds the buffered audio from
        replay_from_ms (paced at replay_speed) then live audio, drops the old
        stream's later results. Returns the new stream id."""
        mode = SttMode(language=language, vocabulary_name=self.cfg.vocabulary_name)
        return await self._reopen_with(mode, replay_from_ms)

    async def _reopen_with(self, mode: SttMode, replay_from_ms: int, replay: bool = True) -> int:
        old = self._live
        frames = self._replay_frames(replay_from_ms) if replay and old is not None else []
        origin = frames[0].ts_ms if frames else (self._fed_end or self.clock.now_ms())
        stream, sid = await self._open_stream(mode, origin)
        if self._replay_task is not None:
            self._replay_task.cancel()
        self._backlog, self._replaying = [], bool(frames)
        self._activate(stream, sid, mode)
        self._last_sent = self._now()
        if frames:
            self._replay_task = asyncio.create_task(self._replay(stream, frames))
        if old is not None:
            self._spawn(self._close_quiet(old))
        return sid

    async def _close_quiet(self, stream: SttStream) -> None:
        try:
            await stream.close()
        except Exception:
            pass

    async def _replay(self, stream: SttStream, frames: list[Frame]) -> None:
        step = max(1, int(20 / max(0.5, self.cfg.replay_speed)))
        try:
            for f in frames:
                if stream is not self._live:
                    return
                await stream.feed(f)
                await self.clock.sleep_ms(step)
            while self._backlog:
                f = self._backlog.pop(0)
                await stream.feed(f)
                await self.clock.sleep_ms(step)
            self._replaying = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._on_error(stream, exc)

    async def feed(self, frame: Frame) -> None:
        if self._unavailable is not None:
            raise self._unavailable
        self._fed_end = max(self._fed_end, frame.end_ms)
        self._latest = max(self._latest, frame.end_ms)
        stream = self._live
        if stream is None:
            return
        if self._replaying:
            self._backlog.append(frame)
            return
        try:
            gap = self._now() - self._last_sent
            if gap >= self.cfg.idle_keepalive_ms:                    # a stall: keep the stream alive first
                await stream.feed(silence(frame.ts_ms))
            await stream.feed(frame)
            self._last_sent = self._now()
        except Exception as exc:
            self._on_error(stream, exc)

    @property
    def identifying(self) -> bool:
        """The live stream is an identify-language stream (first utterance)."""
        return self._identifying

    @property
    def fed_end_ms(self) -> int:
        return self._fed_end

    # --- probe -------------------------------------------------------------------------------

    async def probe(self, candidates: tuple[str, ...], replay_from_ms: int, timeout_ms: int = 2500
                    ) -> LanguageEvidence | None:
        """A short identify-language stream over the buffered utterance only.
        Returns the evidence (source "transcribe_lid") or None; the live
        stream is never touched. `last_probe_scores` keeps every score;
        `last_probe_text` is (language, text) when every final of the probe
        was recognised in the winning language (the utterance's text in it,
        so a switch to that language needs no re-transcription)."""
        self.last_probe_scores = ()
        self.last_probe_text = None
        frames = self._replay_frames(max(replay_from_ms, self._fed_end - 8000))
        if not frames or len(candidates) < 2:
            return None
        mode = SttMode(identify=tuple(candidates), preferred=self.language)
        sid = self._new_id()
        try:
            stream = await asyncio.wait_for(
                self.factory.open(mode, stream_id=sid, audio_origin_ms=frames[0].ts_ms), OPEN_TIMEOUT_S)
        except Exception as exc:
            log.event("stt_probe_failed", level="warning", error=type(exc).__name__)
            return None
        step = max(1, int(20 / max(4.0, self.cfg.replay_speed * 2)))
        finals: list[SttFinal] = []

        async def run() -> None:
            async def feeder() -> None:
                for f in frames:
                    await stream.feed(f)
                    await self.clock.sleep_ms(step)
                await stream.close()
            feed_task = asyncio.create_task(feeder())
            try:
                async for ev in stream.results():
                    if isinstance(ev, SttFinal) and ev.language_scores:
                        finals.append(ev)
            finally:
                feed_task.cancel()

        try:
            await asyncio.wait_for(run(), timeout_ms / 1000)
        except (asyncio.TimeoutError, Exception) as exc:
            if not isinstance(exc, asyncio.TimeoutError):
                log.event("stt_probe_failed", level="warning", error=type(exc).__name__)
        finally:
            await self._close_quiet(stream)
        if not finals:
            return None
        total = sum(max(1, f.end_ms - f.start_ms) for f in finals)
        agg: dict[str, float] = {}
        for f in finals:
            weight = max(1, f.end_ms - f.start_ms) / total
            for code, score in f.language_scores:
                agg[code] = agg.get(code, 0.0) + score * weight
        ordered = tuple(sorted(agg.items(), key=lambda kv: -kv[1]))
        self.last_probe_scores = ordered
        top = ordered[0][0]
        if all(f.language.split("-")[0] == top.split("-")[0] for f in finals):
            text = " ".join(f.text.strip() for f in finals).strip()
            self.last_probe_text = (top, text) if text else None
        words = sum(len(f.text.split()) for f in finals)
        return LanguageEvidence(self._now(), "transcribe_lid", ordered[0][0], ordered[0][1],
                                total, words)

    async def close(self) -> None:
        self._closed = True
        for task in (self._pump_task, self._replay_task, *self._background):
            if task is not None:
                task.cancel()
        live, self._live = self._live, None
        if live is not None:
            await self._close_quiet(live)

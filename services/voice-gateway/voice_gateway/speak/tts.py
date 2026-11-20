"""Text to speech. Owner: builder 'converse'.

Tts protocol: `synthesize(text, language)` -> TtsAudio (canonical PCM16
16 kHz). PollyTts: Amazon Polly SynthesizeSpeech in SpeakConfig.polly_region
(eu-central-1: Elin neural lives only there), voice and engine from
languages.VOICES (never a voice of another language: the native voice
guard), OutputFormat pcm at 16000, TextType ssml:
  <speak><prosody rate="{speech_rate}">{body}</prosody></speak>
where body = copy.ssml_names(xml-escaped text) so "Booqy" is read "Bookie"
(shared/features/phone_agent/copy.py). boto3 runs in a worker thread
(asyncio.to_thread) with SpeakConfig.tts_timeout_ms and tts_retry; the
client is created once per process with that region. `stream()` yields
PCM chunks as Polly's AudioStream delivers them (first audio before the
whole sentence is synthesised); the queue uses it for the first sentence.

FakeTts (tests and harness without AWS): deterministic audio, a 220 Hz
tone at -20 dBFS for 55 ms per character (min 200 ms), no network.

A language with no voice (languages.can_speak False) raises
NoVoice: callers must never fall back to another language's voice.

Retries: one PollyTts call is one attempt bounded by tts_timeout_ms;
`synthesize_with_retry` adds the tts_retry attempts for any Tts (the
queue and the clip cache use it), so retries are counted once.
"""

from __future__ import annotations

import asyncio
import math
import re
import sys
import threading
import time
from array import array
from dataclasses import dataclass
from typing import AsyncIterator, Protocol
from xml.sax.saxutils import escape

from voice_gateway import languages, log
from voice_gateway.config import SpeakConfig

_BYTES_PER_MS = 32          # PCM16 mono at 16 kHz
_FRAME_BYTES = 640          # 20 ms
_TAG = re.compile(r"<[^>]+>")
_STREAM_CHUNK = 3200        # 100 ms: what the fake streams per chunk and Polly is read in


class NoVoice(Exception):
    pass


class TtsError(Exception):
    pass


@dataclass(frozen=True)
class TtsAudio:
    pcm: bytes                  # canonical PCM16 16 kHz mono
    duration_ms: int
    voice: str
    language: str
    first_byte_ms: int = 0      # time to the first audio byte (metrics)


class Tts(Protocol):
    async def synthesize(self, text: str, language: str, *, ssml_body: bool = False) -> TtsAudio: ...

    def stream(self, text: str, language: str) -> AsyncIterator[bytes]: ...


def voice_for(language: str):
    """The native voice row for `language`, NoVoice otherwise (never another language's voice)."""
    if not languages.can_speak(language):
        raise NoVoice(str(language))
    return languages.spec(language)


def ssml(text: str, rate: str, *, body: bool = False) -> str:
    """The SSML document Polly reads. `body=True`: `text` is already an SSML
    body (backchannel tokens), used as it is."""
    if not body:
        from shared.features.phone_agent import copy as pa_copy
        text = pa_copy.ssml_names(escape(" ".join(str(text).split())))
    return f'<speak><prosody rate="{escape(rate, {chr(34): "&quot;"})}">{text}</prosody></speak>'


def whole_frames(pcm: bytes) -> bytes:
    """Pads PCM16 with silence to a whole number of 20 ms frames."""
    rest = len(pcm) % _FRAME_BYTES
    return pcm + bytes(_FRAME_BYTES - rest) if rest else pcm


async def synthesize_with_retry(tts: Tts, cfg: SpeakConfig, text: str, language: str, *,
                                ssml_body: bool = False) -> TtsAudio:
    """tts.synthesize with cfg.tts_retry more attempts; NoVoice is never retried."""
    last: Exception | None = None
    for attempt in range(1 + max(0, cfg.tts_retry)):
        try:
            return await tts.synthesize(text, language, ssml_body=ssml_body)
        except NoVoice:
            raise
        except Exception as exc:        # TtsError, timeouts, botocore errors
            last = exc
            log.event("tts_retry", level="warning", language=language, attempt=attempt + 1,
                      error=type(exc).__name__)
    raise TtsError(type(last).__name__ if last else "failed")


# --- Amazon Polly ----------------------------------------------------------------------------

_clients: dict[str, object] = {}
_clients_lock = threading.Lock()


def polly_client(region: str):
    """One boto3 Polly client per process and region (thread safe after creation)."""
    with _clients_lock:
        client = _clients.get(region)
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client("polly", region_name=region, config=Config(
                connect_timeout=2, read_timeout=5, retries={"max_attempts": 1, "mode": "standard"},
                max_pool_connections=32))
            _clients[region] = client
        return client


class PollyTts:
    def __init__(self, cfg: SpeakConfig, client=None) -> None:
        self.cfg, self._client = cfg, client

    @property
    def client(self):
        if self._client is None:
            self._client = polly_client(self.cfg.polly_region)
        return self._client

    def _request(self, text: str, language: str, ssml_body: bool) -> dict:
        row = voice_for(language)
        req = {"Text": ssml(text, self.cfg.speech_rate, body=ssml_body), "TextType": "ssml",
               "VoiceId": row.polly_voice, "Engine": row.polly_engine or "neural", "OutputFormat": "pcm",
               "SampleRate": str(self.cfg.sample_rate)}
        if row.polly_language:
            req["LanguageCode"] = row.polly_language
        return req

    async def warm(self, language: str) -> None:
        """Opens the Polly connection at call start (one short request in the call's voice), so the first
        sentence does not pay for the TLS handshake. Never fails the call."""
        try:
            await self.synthesize("Mm.", language)
        except Exception:   # noqa: BLE001  (only a warm-up)
            pass

    async def synthesize(self, text: str, language: str, *, ssml_body: bool = False) -> TtsAudio:
        req = self._request(text, language, ssml_body)
        started = time.monotonic()

        def call() -> tuple[bytes, int]:
            resp = self.client.synthesize_speech(**req)
            body = resp["AudioStream"]
            try:
                first = body.read(_STREAM_CHUNK)
                first_ms = int((time.monotonic() - started) * 1000)
                return first + body.read(), first_ms
            finally:
                body.close()

        try:
            pcm, first_ms = await asyncio.wait_for(asyncio.to_thread(call), self.cfg.tts_timeout_ms / 1000)
        except NoVoice:
            raise
        except Exception as exc:
            raise TtsError(type(exc).__name__) from exc
        pcm = whole_frames(pcm[: len(pcm) // 2 * 2])
        return TtsAudio(pcm, len(pcm) // _BYTES_PER_MS, req["VoiceId"], language, first_ms)

    async def stream(self, text: str, language: str) -> AsyncIterator[bytes]:
        """PCM chunks (whole 20 ms frames) as Polly delivers them. The first
        chunk must arrive within tts_timeout_ms, else TtsError; a stream that
        breaks after audio was yielded just ends (the caller heard a part)."""
        req = self._request(text, language, False)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()
        done = object()

        def worker() -> None:
            try:
                resp = self.client.synthesize_speech(**req)
                body = resp["AudioStream"]
                try:
                    while not stop.is_set():
                        chunk = body.read(_STREAM_CHUNK)
                        if not chunk:
                            break
                        loop.call_soon_threadsafe(queue.put_nowait, chunk)
                finally:
                    body.close()
                loop.call_soon_threadsafe(queue.put_nowait, done)
            except Exception as exc:        # handed to the async side
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, exc)
                except RuntimeError:        # loop closed: the call is over
                    pass

        thread = threading.Thread(target=worker, name="polly-stream", daemon=True)
        thread.start()
        rest = b""
        yielded = False
        try:
            while True:
                try:
                    timeout = None if yielded else self.cfg.tts_timeout_ms / 1000
                    item = await asyncio.wait_for(queue.get(), timeout)
                except asyncio.TimeoutError as exc:
                    raise TtsError("timeout") from exc
                if item is done:
                    break
                if isinstance(item, Exception):
                    if yielded:
                        log.event("tts_stream_broken", level="warning", language=language,
                                  error=type(item).__name__)
                        break
                    raise TtsError(type(item).__name__) from item
                rest += item
                cut = len(rest) // _FRAME_BYTES * _FRAME_BYTES
                if cut:
                    chunk, rest = rest[:cut], rest[cut:]
                    yielded = True
                    yield chunk
            if rest:
                yield whole_frames(rest[: len(rest) // 2 * 2])
        finally:
            stop.set()


# --- the fake --------------------------------------------------------------------------------

def tone(duration_ms: int, *, hz: float = 220.0, dbfs: float = -20.0, sample_rate: int = 16000) -> bytes:
    """A sine tone of whole 20 ms frames (PCM16 LE)."""
    n = (duration_ms + 19) // 20 * 20 * sample_rate // 1000
    amp = 32767 * 10 ** (dbfs / 20) * math.sqrt(2)      # rms = dbfs
    a = array("h", (int(amp * math.sin(2 * math.pi * hz * i / sample_rate)) for i in range(n)))
    if sys.byteorder == "big":
        a.byteswap()
    return a.tobytes()


class FakeTts:
    """Deterministic, offline (an SSML body is measured without its tags). Test hooks: `fail[text] = n` makes the next
    n calls for `text` raise TtsError; `latency_ms` with a `clock` delays
    each call on that clock (a stream: before its first chunk)."""

    def __init__(self, ms_per_char: int = 55, min_ms: int = 200, *, clock=None, latency_ms: int = 0) -> None:
        self.ms_per_char, self.min_ms = ms_per_char, min_ms
        self.calls: list[tuple[str, str]] = []
        self.fail: dict[str, int] = {}
        self.clock, self.latency_ms = clock, latency_ms
        self._cache: dict[int, bytes] = {}

    def duration_for(self, text: str) -> int:
        return max(self.min_ms, len(text) * self.ms_per_char)

    def _audio(self, text: str) -> bytes:
        ms = self.duration_for(text)
        if ms not in self._cache:
            self._cache[ms] = tone(ms)
        return self._cache[ms]

    async def _check(self, text: str, language: str) -> None:
        voice_for(language)
        self.calls.append((text, language))
        if self.clock is not None and self.latency_ms:
            await self.clock.sleep_ms(self.latency_ms)
        if self.fail.get(text, 0) > 0:
            self.fail[text] -= 1
            raise TtsError("fake failure")

    async def synthesize(self, text: str, language: str, *, ssml_body: bool = False) -> TtsAudio:
        await self._check(text, language)
        pcm = self._audio(_TAG.sub("", text) if ssml_body else text)
        return TtsAudio(pcm, len(pcm) // _BYTES_PER_MS, "Fake", language, self.latency_ms)

    async def stream(self, text: str, language: str) -> AsyncIterator[bytes]:
        await self._check(text, language)
        pcm = self._audio(text)
        for offset in range(0, len(pcm), _STREAM_CHUNK):
            yield pcm[offset:offset + _STREAM_CHUNK]

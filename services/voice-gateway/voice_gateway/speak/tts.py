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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol

from voice_gateway.config import SpeakConfig


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


class PollyTts:
    def __init__(self, cfg: SpeakConfig, client=None) -> None:
        self.cfg, self._client = cfg, client

    async def synthesize(self, text: str, language: str, *, ssml_body: bool = False) -> TtsAudio:
        raise NotImplementedError

    async def stream(self, text: str, language: str) -> AsyncIterator[bytes]:
        raise NotImplementedError
        yield b""


class FakeTts:
    def __init__(self, ms_per_char: int = 55, min_ms: int = 200) -> None:
        self.ms_per_char, self.min_ms = ms_per_char, min_ms
        self.calls: list[tuple[str, str]] = []

    async def synthesize(self, text: str, language: str, *, ssml_body: bool = False) -> TtsAudio:
        raise NotImplementedError

    async def stream(self, text: str, language: str) -> AsyncIterator[bytes]:
        raise NotImplementedError
        yield b""

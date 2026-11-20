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

FakeSttFactory (tests, harness without AWS): returns scripted results.
The script maps caller audio segments (by LocalAdapter label / time) to
text, language and scores; it emits partials word by word at a fixed
rate and the final `final_delay_ms` after the segment's end.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol

from voice_gateway.audio.buffers import RingBuffer
from voice_gateway.audio.frames import Frame
from voice_gateway.clock import Clock
from voice_gateway.config import SttConfig
from voice_gateway.events import EventBus, LanguageEvidence, SttFinal, SttPartial


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


class TranscribeFactory:
    def __init__(self, cfg: SttConfig) -> None:
        self.cfg = cfg

    async def open(self, mode: SttMode, *, stream_id: int, audio_origin_ms: int) -> SttStream:
        raise NotImplementedError


@dataclass(frozen=True)
class ScriptedUtterance:
    """Harness script line: caller audio labelled `label` is heard as `text`."""
    label: str
    text: str
    language: str
    scores: tuple[tuple[str, float], ...] = ()
    confidence: float = 0.9


@dataclass
class FakeSttFactory:
    script: list[ScriptedUtterance] = field(default_factory=list)
    words_per_s: float = 3.0
    final_delay_ms: int = 250
    opened: list[SttMode] = field(default_factory=list)

    async def open(self, mode: SttMode, *, stream_id: int, audio_origin_ms: int) -> SttStream:
        raise NotImplementedError


class Transcriber:
    def __init__(self, factory: SttFactory, ring: RingBuffer, cfg: SttConfig, bus: EventBus, clock: Clock) -> None:
        self.factory, self.ring, self.cfg, self.bus, self.clock = factory, ring, cfg, bus, clock
        self.language: str | None = None

    async def open(self, language: str) -> None:
        raise NotImplementedError

    async def open_identify(self, candidates: tuple[str, ...], preferred: str | None = None) -> None:
        raise NotImplementedError

    async def feed(self, frame: Frame) -> None:
        raise NotImplementedError

    async def reopen(self, language: str, replay_from_ms: int) -> None:
        raise NotImplementedError

    async def probe(self, candidates: tuple[str, ...], replay_from_ms: int, timeout_ms: int = 2500
                    ) -> LanguageEvidence | None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    @property
    def identifying(self) -> bool:
        """The live stream is an identify-language stream (first utterance)."""
        raise NotImplementedError

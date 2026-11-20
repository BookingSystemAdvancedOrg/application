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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Literal

from voice_gateway.clock import Clock
from voice_gateway.config import SpeakConfig
from voice_gateway.speak.tts import Tts


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


class SpeechQueue:
    def __init__(self, tts: Tts, cfg: SpeakConfig, clock: Clock) -> None:
        self.tts, self.cfg, self.clock = tts, cfg, clock

    def enqueue(self, item: SpeechItem) -> None:
        raise NotImplementedError

    def ready(self) -> AsyncIterator[ReadyAudio]:
        raise NotImplementedError

    def cancel(self) -> list[SpeechItem]:
        raise NotImplementedError

    @property
    def pending(self) -> int:
        """Items enqueued and not yet fully handed to the mixer."""
        raise NotImplementedError

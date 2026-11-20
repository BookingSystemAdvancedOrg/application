"""Pre-rendered clips (backchannels and fillers). Owner: builder 'converse'.

ClipCache.warm() renders every backchannel token (tokens.TOKENS) and every
filler (speak.fillers.FILLERS) for the given languages with the speak.tts
Tts at task start, applies BackchannelConfig.clip_volume_db to
backchannels, trims leading and trailing silence (a backchannel clip
should be 150 to 450 ms), and keeps canonical PCM in memory. A disk cache
(`cache_dir`, keyed by sha256 of voice + engine + text + rate) avoids
Polly calls on restarts and in tests; the harness may ship the cache.
warm() never blocks call handling: a missing clip just means no
backchannel or no filler for that token.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from voice_gateway.config import BackchannelConfig, SpeakConfig
from voice_gateway.speak.tts import Tts


@dataclass(frozen=True)
class Clip:
    key: str            # "bc:sv:mm" or "filler:sv:0"
    language: str
    pcm: bytes          # canonical PCM16 16 kHz
    duration_ms: int


class ClipCache:
    def __init__(self, tts: Tts, speak: SpeakConfig, backchannel: BackchannelConfig,
                 cache_dir: Path | None = None) -> None:
        self.tts, self.speak, self.backchannel, self.cache_dir = tts, speak, backchannel, cache_dir

    async def warm(self, languages: tuple[str, ...]) -> int:
        """Renders what is missing; returns the number of clips ready."""
        raise NotImplementedError

    def backchannel(self, language: str, token: str) -> Clip | None:
        raise NotImplementedError

    def filler(self, language: str, index: int) -> Clip | None:
        raise NotImplementedError

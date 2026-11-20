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

The disk cache holds the raw TTS audio (before trim and gain), so tuning
the volume or the trim never needs Polly again; the key also names the
Tts class, so fake audio never answers for Polly. A backchannel that is
still longer than MAX_BACKCHANNEL_MS after the trim is not kept (a long
"mm" sounds like the assistant wants the turn).
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from array import array
from dataclasses import dataclass
from pathlib import Path

from voice_gateway import log
from voice_gateway.audio.frames import gain
from voice_gateway.backchannel.tokens import TOKENS, base_language
from voice_gateway.config import BackchannelConfig, SpeakConfig
from voice_gateway.speak.tts import NoVoice, Tts, synthesize_with_retry, voice_for, whole_frames

MAX_BACKCHANNEL_MS = 700
_RATE = 16000
_WINDOW = 160                   # 10 ms
_LEAD_PAD_MS, _TAIL_PAD_MS, _FADE_MS = 10, 30, 5
_PARALLEL = 4


@dataclass(frozen=True)
class Clip:
    key: str            # "bc:sv:mm" or "filler:sv:0"
    language: str
    pcm: bytes          # canonical PCM16 16 kHz
    duration_ms: int


def _samples(pcm: bytes) -> array:
    a = array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        a.byteswap()
    return a


def _pcm(a: array) -> bytes:
    if sys.byteorder == "big":
        a = array("h", a)
        a.byteswap()
    return a.tobytes()


def trim(pcm: bytes, *, rel_db: float = 28.0, floor_dbfs: float = -55.0) -> bytes:
    """Cuts leading and trailing silence (10 ms windows quieter than the
    loudest window minus `rel_db`, and never louder than `floor_dbfs`
    counts as speech), keeps a short pad, fades 5 ms in and out (no click)
    and pads to whole 20 ms frames."""
    a = _samples(pcm)
    if not a:
        return b""
    energies = []
    for i in range(0, len(a), _WINDOW):
        w = a[i:i + _WINDOW]
        energies.append(sum(x * x for x in w) / max(1, len(w)))
    peak = max(energies)
    if peak <= 0:
        return b""
    threshold = max(peak * 10 ** (-rel_db / 10), (32768.0 ** 2) * 10 ** (floor_dbfs / 10))
    voiced = [i for i, e in enumerate(energies) if e >= threshold]
    if not voiced:
        return b""
    start = max(0, voiced[0] * _WINDOW - _LEAD_PAD_MS * _RATE // 1000)
    end = min(len(a), (voiced[-1] + 1) * _WINDOW + _TAIL_PAD_MS * _RATE // 1000)
    out = array("h", a[start:end])
    fade = min(len(out) // 2, _FADE_MS * _RATE // 1000)
    for i in range(fade):
        f = i / fade
        out[i] = int(out[i] * f)
        out[-1 - i] = int(out[-1 - i] * f)
    return whole_frames(_pcm(out))


class ClipCache:
    def __init__(self, tts: Tts, speak: SpeakConfig, backchannel: BackchannelConfig,
                 cache_dir: Path | None = None) -> None:
        # the config is `backchannel_cfg`: `backchannel` is the lookup method
        self.tts, self.speak, self.backchannel_cfg, self.cache_dir = tts, speak, backchannel, cache_dir
        self._clips: dict[str, Clip] = {}
        self.rendered = 0           # TTS calls made (the rest came from disk or memory)
        self.rejected: list[str] = []

    # -- lookups ------------------------------------------------------------------------------
    def backchannel(self, language: str, token: str) -> Clip | None:
        return self._clips.get(f"bc:{base_language(language)}:{token}")

    def filler(self, language: str, index: int) -> Clip | None:
        return self._clips.get(f"filler:{base_language(language)}:{index}")

    def has_backchannel(self, language: str, token: str) -> bool:
        return self.backchannel(language, token) is not None

    def __len__(self) -> int:
        return len(self._clips)

    # -- warming ------------------------------------------------------------------------------
    async def warm(self, languages: tuple[str, ...]) -> int:
        """Renders what is missing; returns the number of clips ready."""
        from voice_gateway.speak.fillers import FILLERS

        jobs = []
        for lang in dict.fromkeys(base_language(x) for x in languages):
            for token, _, text in TOKENS.get(lang, ()):
                jobs.append((f"bc:{lang}:{token}", lang, text, True))
            for index, text in enumerate(FILLERS.get(lang, ())):
                jobs.append((f"filler:{lang}:{index}", lang, text, False))
        jobs = [j for j in jobs if j[0] not in self._clips]
        gate = asyncio.Semaphore(_PARALLEL)

        async def one(key: str, lang: str, text: str, is_bc: bool) -> None:
            async with gate:
                try:
                    raw = await self._raw(lang, text, is_bc)
                except NoVoice:
                    return
                except Exception as exc:
                    log.event("clip_failed", level="warning", key=key, error=type(exc).__name__)
                    return
            pcm = trim(raw)
            if is_bc:
                pcm = gain(pcm, self.backchannel_cfg.clip_volume_db) if pcm else pcm
            ms = len(pcm) // 32
            if not pcm or (is_bc and ms > MAX_BACKCHANNEL_MS):
                self.rejected.append(key)
                log.event("clip_rejected", level="warning", key=key, ms=ms)
                return
            self._clips[key] = Clip(key, lang, pcm, ms)

        await asyncio.gather(*(one(*j) for j in jobs))
        wanted = {base_language(x) for x in languages}
        return sum(1 for c in self._clips.values() if c.language in wanted)

    def _key(self, lang: str, text: str, is_bc: bool) -> str:
        row = voice_for(lang)
        raw = "|".join((type(self.tts).__name__, row.polly_voice or "", row.polly_engine or "",
                        row.polly_language or "", self.speak.speech_rate, "ssml" if is_bc else "text", text))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def _raw(self, lang: str, text: str, is_bc: bool) -> bytes:
        key = self._key(lang, text, is_bc)          # NoVoice for a language without a voice
        path = self.cache_dir / f"{key}.pcm" if self.cache_dir else None
        if path is not None and path.is_file():
            return path.read_bytes()
        audio = await synthesize_with_retry(self.tts, self.speak, text, lang, ssml_body=is_bc)
        self.rendered += 1
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(audio.pcm)
                tmp.replace(path)
            except OSError as exc:
                log.event("clip_cache_write_failed", level="warning", error=type(exc).__name__)
        return audio.pcm

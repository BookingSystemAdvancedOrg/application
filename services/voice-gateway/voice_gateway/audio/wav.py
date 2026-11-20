"""WAV helpers for LocalAdapter and the harness (stdlib `wave`).
Owner: builder 'listen'."""

from __future__ import annotations

from pathlib import Path


def read_wav(path: str | Path, target_rate: int = 16000) -> bytes:
    """Mono PCM16 at target_rate from a WAV file (8-bit mu-law WAVs,
    stereo and other rates are converted)."""
    raise NotImplementedError


def write_wav(path: str | Path, pcm: bytes, sample_rate: int = 16000, channels: int = 1) -> None:
    raise NotImplementedError


def interleave(left: bytes, right: bytes) -> bytes:
    """Two mono PCM16 tracks -> one stereo PCM16 (shorter one padded)."""
    raise NotImplementedError

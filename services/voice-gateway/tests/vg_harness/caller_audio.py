"""Caller audio from Polly. Owner: builder 'harness'.

CALLER_VOICES: a Polly voice per language that is NOT the assistant's
voice where possible (sv: Astrid standard, eu-north-1, since Elin is the
assistant; en: Brian; ar: Zayd; de: Daniel; es: Sergio; fr: Remi; it:
Adriano; pl: Ola (no second pl voice); tr: Burcu (no second tr voice)).
Languages with no Polly voice (fa, bs, id) use committed WAV fixtures
(tests/fixtures/caller/<lang>-<hash>.wav), recorded or supplied by the
owner; until present, those scenarios run with FakeStt only.

render(text, language) -> PCM16 16 kHz: cached on disk by sha256(voice,
engine, text) in tests/fixtures/caller/ so CI and reruns cost nothing
(profile booqy-dev, tiny volumes; never in root CI). line(pcm) applies
the phone line: 8 kHz mu-law round trip, optional noise.
"""

from __future__ import annotations

from pathlib import Path

CACHE = Path(__file__).resolve().parents[1] / "fixtures" / "caller"

CALLER_VOICES: dict[str, tuple[str, str, str]] = {   # lang -> (voice, engine, region)
    "sv": ("Astrid", "standard", "eu-north-1"),
    "en": ("Brian", "neural", "eu-central-1"),
    "ar": ("Zayd", "neural", "eu-central-1"),
    "de": ("Daniel", "neural", "eu-central-1"),
    "es": ("Sergio", "neural", "eu-central-1"),
    "fr": ("Remi", "neural", "eu-central-1"),
    "it": ("Adriano", "neural", "eu-central-1"),
    "pl": ("Ola", "neural", "eu-central-1"),
    "tr": ("Burcu", "neural", "eu-central-1"),
}


def render(text: str, language: str) -> bytes:
    raise NotImplementedError


def line(pcm16: bytes, *, noise_dbfs: float | None = None) -> bytes:
    raise NotImplementedError

"""The gateway's voice table (FROZEN shape).

One row per language code the agent uses (shared/features/phone_agent/
language.py codes). The Polly voice and engine come from the canonical
aws_languages.json (the same voices the Connect line speaks, so callers hear
the same assistant on both lines); the Transcribe streaming code is added
here. Languages Transcribe can hear but no Polly voice can speak (fa, bs, id,
so, ...) are HEARD_ONLY: detected, then answered by the fixed unsupported
sentences and a callback (owner 2026-11-20: no ElevenLabs, AWS voices only).

Facts checked 2026-11-20 with booqy-dev: Polly neural voices in
eu-central-1 include every voice below (Elin only there, not eu-north-1);
the Transcribe streaming endpoint resolves in eu-north-1 but answers 401
NotAuthorized there, so the gateway streams to eu-central-1 (SttConfig.region).
Verified there with live streams (builder 'listen'): sv-SE and en-GB with
partial stabilization, identify-language with sv-SE among the options, and
a stream opens for every code in TRANSCRIBE (opening proves the code is
accepted, not the recognition quality: Arabic and Persian are unverified on
real callers).
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.features.phone_agent import language as pa_language


@dataclass(frozen=True)
class VoiceSpec:
    code: str                  # our code (sv, en, ar, ...)
    transcribe: str | None     # Transcribe streaming language code, None: cannot be heard
    polly_voice: str | None    # None: cannot be spoken (HEARD_ONLY)
    polly_engine: str = "neural"
    polly_language: str | None = None   # Polly LanguageCode when the voice speaks several


# Transcribe streaming language codes for our codes (verify: bs-BA, fa-IR, id-ID, so-SO, is-IS, ca-ES
# in STREAMING, not only batch; ar: ar-SA (MSA, broader) is the default, ar-AE the alternative,
# MULTILINGUAL.md decision 6).
TRANSCRIBE = {
    "sv": "sv-SE", "en": "en-GB", "da": "da-DK", "fi": "fi-FI", "de": "de-DE", "fr": "fr-FR", "es": "es-ES",
    "it": "it-IT", "nl": "nl-NL", "pl": "pl-PL", "pt": "pt-PT", "ar": "ar-SA", "tr": "tr-TR", "cs": "cs-CZ",
    "ca": "ca-ES", "ja": "ja-JP", "ko": "ko-KR", "hi": "hi-IN", "ru": "ru-RU", "is": "is-IS", "no": "no-NO",
    "fa": "fa-IR", "id": "id-ID", "bs": "bs-BA", "so": "so-SO", "uk": "uk-UA",
    # stage 1 voices (2026-11-20); not opened live yet (the dev SSO session had expired): a stream that will not
    # open keeps the call on the current stream (Transcriber.reopen), it never ends the call
    "zh": "zh-CN", "yue": "zh-HK", "ro": "ro-RO", "cy": "cy-WL",
}
TRANSCRIBE_TO_CODE = {v: k for k, v in TRANSCRIBE.items()} | {"ar-AE": "ar", "en-US": "en", "en-AU": "en",
                                                              "es-US": "es", "fr-CA": "fr", "pt-BR": "pt",
                                                              "de-CH": "de", "hr-HR": "bs", "sr-RS": "bs"}


def _table() -> dict[str, VoiceSpec]:
    rows = {}
    for code, spec in pa_language.AWS_LANGUAGES.items():
        rows[code] = VoiceSpec(code, TRANSCRIBE.get(code), spec.get("voice"), spec.get("engine") or "neural",
                               spec.get("connectLanguage"))
    for code, tcode in TRANSCRIBE.items():
        rows.setdefault(code, VoiceSpec(code, tcode, None))
    return rows


VOICES: dict[str, VoiceSpec] = _table()
SPOKEN: tuple[str, ...] = tuple(c for c, v in VOICES.items() if v.polly_voice)       # the call may switch to these
HEARD_ONLY: tuple[str, ...] = tuple(c for c, v in VOICES.items() if not v.polly_voice and v.transcribe)


def spec(code: str) -> VoiceSpec | None:
    return VOICES.get(pa_language.normalise(code) if code else code)


def can_speak(code: str | None) -> bool:
    row = spec(code) if code else None
    return bool(row and row.polly_voice)


def to_code(transcribe_code: str | None) -> str | None:
    """Our code for a Transcribe language code (sv-SE -> sv), else None."""
    return TRANSCRIBE_TO_CODE.get(str(transcribe_code or ""))


def to_transcribe(code: str) -> str | None:
    row = spec(code)
    return row.transcribe if row else None

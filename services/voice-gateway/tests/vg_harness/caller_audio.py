"""Caller audio from Polly. Owner: builder 'harness'.

CALLER_VOICES: a Polly voice per language that is NOT the assistant's
voice where possible (sv: Astrid standard, eu-north-1, since Elin is the
assistant; en: Brian; ar: Zayd; de: Daniel; es: Sergio; fr: Remi; it:
Adriano; pl: Ola (no second pl voice); tr: Burcu (no second tr voice)).
Languages with no Polly voice (fa, bs, id) use committed fixtures
(tests/fixtures/caller/<lang>-<hash>.wav or .ulaw), recorded or supplied by
the owner; until present, those scenarios run on the synthetic speech
stand-in below (fake mode only).

render(text, language) -> PCM16 16 kHz: cached on disk by sha256(voice,
engine, text) in tests/fixtures/caller/ so CI and reruns cost nothing
(profile booqy-dev, tiny volumes; never in root CI). line(pcm) applies
the phone line: 8 kHz mu-law round trip, optional noise.

Cache format: `<lang>-<hash16>.ulaw`, raw G.711 mu-law at 8 kHz (8 KB per
second, so the whole scenario set stays under 1 MB). Polly is asked for
8 kHz PCM directly, so the stored clip already is what a phone line would
carry; render() decodes and upsamples to the gateway's canonical 16 kHz.

Three sources, in this order (`Rendered.source`):
  "cache"  a fixture file on disk (free, deterministic);
  "polly"  a fresh Polly call, written to the cache (needs AWS credentials:
           profile VG_AWS_PROFILE, default booqy-dev; refused unless
           `allow_network` is true);
  "synth"  `synth_speech`: a deterministic speech-like signal (harmonics
           under a syllable envelope) for fake mode, where Transcribe is
           scripted and only an energy VAD has to see "speech". Never used
           in live mode.

Run `python -m vg_harness.caller_audio --warm` from services/voice-gateway/tests
to fill the cache for every scenario (a few cents, once).
"""

from __future__ import annotations

import hashlib
import math
import os
import random
import re
import sys
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path

CACHE = Path(__file__).resolve().parents[1] / "fixtures" / "caller"
RATE = 16000
LINE_RATE = 8000

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


class CallerAudioMissing(Exception):
    """No cached clip and no way to make one (no voice, no network allowed)."""


@dataclass(frozen=True)
class Rendered:
    pcm: bytes                  # PCM16 16 kHz mono
    source: str                 # "cache" | "polly" | "synth"
    voice: str | None
    duration_ms: int
    key: str


# --- sample helpers (pure Python: root CI has no numpy) -----------------------------------------------------

def _samples(pcm: bytes) -> array:
    a = array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        a.byteswap()
    return a


def _pcm(samples) -> bytes:
    a = array("h", samples)
    if sys.byteorder == "big":
        a.byteswap()
    return a.tobytes()


def duration_ms(pcm: bytes, rate: int = RATE) -> int:
    return len(pcm) // 2 * 1000 // rate


def silence_pcm(ms: int, rate: int = RATE) -> bytes:
    return bytes(rate * max(0, ms) // 1000 * 2)


def _upsample2(pcm8k: bytes) -> bytes:
    """8 kHz -> 16 kHz by linear interpolation (the line is band limited to 4 kHz anyway)."""
    a = _samples(pcm8k)
    out = array("h")
    for i, x in enumerate(a):
        nxt = a[i + 1] if i + 1 < len(a) else x
        out.append(x)
        out.append((x + nxt) // 2)
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def _downsample2(pcm16k: bytes) -> bytes:
    """16 kHz -> 8 kHz: a 3 tap low pass (1 2 1) then every second sample."""
    a = _samples(pcm16k)
    out = array("h")
    last = len(a) - 1
    for i in range(0, len(a), 2):
        prev, nxt = a[max(0, i - 1)], a[min(last, i + 1)] if a else 0
        out.append((prev + 2 * a[i] + nxt) // 4)
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


# --- the phone line -----------------------------------------------------------------------------------------

def line(pcm16: bytes, *, noise_dbfs: float | None = None, seed: int = 1) -> bytes:
    """PCM16 16 kHz through an 8 kHz mu-law phone line and back (what a PSTN call does to Polly audio),
    plus optional white noise at `noise_dbfs` (deterministic per seed). Same length out as in."""
    from voice_gateway.audio.frames import mulaw_decode, mulaw_encode
    narrow = mulaw_decode(mulaw_encode(_downsample2(pcm16)))
    out = _upsample2(narrow)[: len(pcm16)]
    if len(out) < len(pcm16):
        out += bytes(len(pcm16) - len(out))
    if noise_dbfs is not None:
        rng = random.Random(seed)
        amp = 32768.0 * 10 ** (noise_dbfs / 20)
        out = _pcm(max(-32768, min(32767, int(x + rng.gauss(0, amp)))) for x in _samples(out))
    return out


# --- fixtures on disk ---------------------------------------------------------------------------------------

def cache_key(text: str, language: str) -> str:
    voice = CALLER_VOICES.get(language)
    basis = f"{voice[0]}|{voice[1]}|{language}|{text}" if voice else f"-|-|{language}|{text}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def cache_path(text: str, language: str, suffix: str = ".ulaw") -> Path:
    return CACHE / f"{language}-{cache_key(text, language)}{suffix}"


def _read_wav(path: Path) -> bytes:
    """A supplied fixture WAV (mono or stereo PCM16, any rate) -> PCM16 mono 16 kHz."""
    with wave.open(str(path), "rb") as w:
        channels, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"{path.name}: PCM16 expected")
    a = _samples(frames)
    if channels > 1:
        a = array("h", (sum(a[i:i + channels]) // channels for i in range(0, len(a), channels)))
    pcm = _pcm(a)
    if rate == RATE:
        return pcm
    if rate == LINE_RATE:
        return _upsample2(pcm)
    # any other rate: nearest neighbour is enough for a fixture (it is resampled again by the adapter)
    n = len(a) * RATE // rate
    return _pcm(a[min(len(a) - 1, i * rate // RATE)] for i in range(n))


def _read_cached(text: str, language: str) -> bytes | None:
    from voice_gateway.audio.frames import mulaw_decode
    path = cache_path(text, language)
    if path.is_file():
        return _upsample2(mulaw_decode(path.read_bytes()))
    wav_path = cache_path(text, language, ".wav")
    if wav_path.is_file():
        return _read_wav(wav_path)
    return None


def _write_cache(text: str, language: str, pcm8k: bytes) -> Path:
    from voice_gateway.audio.frames import mulaw_encode
    CACHE.mkdir(parents=True, exist_ok=True)
    path = cache_path(text, language)
    path.write_bytes(mulaw_encode(pcm8k))
    return path


# --- Polly --------------------------------------------------------------------------------------------------

_clients: dict[str, object] = {}


def polly_client(region: str):
    """One boto3 Polly client per region on the dev profile (VG_AWS_PROFILE, default booqy-dev).
    Imported lazily: root CI has no use for it."""
    if region not in _clients:
        import boto3
        profile = os.environ.get("VG_AWS_PROFILE", "booqy-dev")
        _clients[region] = boto3.Session(profile_name=profile, region_name=region).client("polly")
    return _clients[region]


def polly_pcm8k(text: str, language: str, client=None) -> bytes:
    """Polly speaks `text` with the caller voice of `language`; PCM16 mono at 8 kHz."""
    voice, engine, region = CALLER_VOICES[language]
    client = client or polly_client(region)
    out = client.synthesize_speech(Text=text, OutputFormat="pcm", SampleRate=str(LINE_RATE), VoiceId=voice,
                                   Engine=engine, TextType="text")
    return out["AudioStream"].read()


def render_detailed(text: str, language: str, *, allow_network: bool = False, synth_fallback: bool = False,
                    client=None) -> Rendered:
    key = cache_key(text, language)
    cached = _read_cached(text, language)
    if cached is not None:
        return Rendered(cached, "cache", CALLER_VOICES.get(language, (None,))[0], duration_ms(cached), key)
    if language in CALLER_VOICES and (allow_network or client is not None):
        pcm8k = polly_pcm8k(text, language, client)
        _write_cache(text, language, pcm8k)
        pcm = _upsample2(pcm8k)
        return Rendered(pcm, "polly", CALLER_VOICES[language][0], duration_ms(pcm), key)
    if synth_fallback:
        pcm = synth_speech(text, language)
        return Rendered(pcm, "synth", None, duration_ms(pcm), key)
    why = "no Polly voice and no fixture" if language not in CALLER_VOICES else "not cached and network not allowed"
    raise CallerAudioMissing(f"{language}: {why}: {cache_path(text, language).name}")


def render(text: str, language: str) -> bytes:
    """PCM16 16 kHz of `text` spoken by the caller voice. Cache first; a missing clip is fetched from Polly only
    when VG_CALLER_NETWORK=1 (live runs and --warm), never silently."""
    return render_detailed(text, language, allow_network=os.environ.get("VG_CALLER_NETWORK") == "1").pcm


# --- speech stand-in for fake mode --------------------------------------------------------------------------

_VOWELS = re.compile(r"[aeiouyåäöéèáàíóúüæøاوي]+", re.IGNORECASE)


def syllables(text: str) -> int:
    n = len(_VOWELS.findall(text))
    return max(1, n if n else len(text) // 3)


def synth_speech(text: str, language: str = "sv", *, level_dbfs: float = -24.0) -> bytes:
    """A deterministic speech-like clip: a glottal-ish harmonic stack at 110 to 150 Hz under a syllable envelope
    (about 5.5 syllables a second), with a short gap at every comma or full stop. It is loud and voiced enough for an
    energy VAD and for the echo and level logic; it is not intelligible and never goes to a real recogniser."""
    seed = int(hashlib.sha256(f"{language}|{text}".encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)
    f0_base = 105 + rng.random() * 40
    parts = [p for p in re.split(r"(?<=[,.;?!])\s+", text.strip()) if p] or [text]
    out = array("h")
    phase = 0.0
    for part in parts:
        syl = syllables(part)
        ms = max(220, int(syl * 182))
        n = RATE * ms // 1000
        syl_len = n / syl
        harmonics = 7
        weights = [1.0 / (k ** 1.1) for k in range(1, harmonics + 1)]
        norm = sum(w * w for w in weights) ** 0.5
        body = []
        for i in range(n):
            pos = (i % syl_len) / syl_len
            env = math.sin(math.pi * pos) ** 1.4
            f0 = f0_base * (1.0 + 0.08 * math.sin(2 * math.pi * i / RATE * 2.3))
            phase += 2 * math.pi * f0 / RATE
            v = sum(w * math.sin(k * phase) for k, w in enumerate(weights, 1)) / norm
            body.append(env * v)
        peak_rms = math.sqrt(sum(x * x for x in body) / len(body)) or 1.0
        gain = 32768 * 10 ** (level_dbfs / 20) / peak_rms
        out.extend(int(max(-32768, min(32767, x * gain))) for x in body)
        if len(parts) > 1:
            out.extend([0] * (RATE * 90 // 1000))
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


# --- a scenario step as audio -------------------------------------------------------------------------------

@dataclass(frozen=True)
class PieceAudio:
    label: str
    text: str
    language: str
    pcm: bytes
    pause_after_ms: int
    source: str


def step_audio(scenario_key: str, step_index: int, step, *, allow_network: bool = False, synth_fallback: bool = False,
               client=None) -> list[PieceAudio]:
    """Every piece of a Step rendered; the labels are what the fake recogniser keys its script on."""
    pieces = []
    for j, piece in enumerate(step.pieces):
        r = render_detailed(piece.text, step.language, allow_network=allow_network, synth_fallback=synth_fallback,
                            client=client)
        pieces.append(PieceAudio(piece_label(scenario_key, step_index, j), piece.text, step.language, r.pcm,
                                 piece.pause_after_ms, r.source))
    return pieces


def piece_label(scenario_key: str, step_index: int, piece_index: int) -> str:
    return f"{scenario_key}/s{step_index}p{piece_index}"


def join_pieces(pieces: list[PieceAudio]) -> bytes:
    """The whole step as one clip with the real pauses (for a WAV preview or a one-shot say())."""
    out = bytearray()
    for i, p in enumerate(pieces):
        out += p.pcm
        if i < len(pieces) - 1:
            out += silence_pcm(p.pause_after_ms)
    return bytes(out)


def warm(scenarios=None, *, dry_run: bool = False, client_factory=None) -> list[str]:
    """Fills the cache for every Polly-voiced caller piece of `scenarios` (default: all). Returns what was
    (or, dry run, would be) rendered. A language without a voice and without a fixture is reported, not an error."""
    from vg_harness import scenarios as sc
    todo, seen = [], set()
    for scenario in scenarios or sc.ALL:
        for step in scenario.steps:
            for piece in step.pieces:
                path = cache_path(piece.text, step.language)
                wav = cache_path(piece.text, step.language, ".wav")
                if path in seen or path.is_file() or wav.is_file():
                    continue
                seen.add(path)
                todo.append((step.language, piece.text, path))
    report = []
    for language, text, path in todo:
        if language not in CALLER_VOICES:
            report.append(f"no voice: {language} {path.name} (supply a fixture WAV)")
            continue
        if dry_run:
            report.append(f"would render: {language} {path.name} {len(text)} chars")
            continue
        client = client_factory(CALLER_VOICES[language][2]) if client_factory else None
        pcm8k = polly_pcm8k(text, language, client)
        _write_cache(text, language, pcm8k)
        report.append(f"rendered: {language} {path.name} {duration_ms(pcm8k, LINE_RATE)} ms")
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Caller audio cache for the replay harness")
    parser.add_argument("--warm", action="store_true", help="render every missing caller clip with Polly (booqy-dev)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.warm:
        parser.print_help()
        return 0
    for line_ in warm(dry_run=args.dry_run):
        print(line_)
    return 0


if __name__ == "__main__":
    sys.exit(main())

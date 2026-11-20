"""Renders the listen builder's audio fixtures with Amazon Polly (booqy-dev,
a few hundred characters in total) as 8 kHz mu-law WAVs, the format of a
telephone line. Run from the application root:

    AWS_PROFILE=booqy-dev python services/voice-gateway/tests/fixtures/listen/make_fixtures.py

The voices differ from the assistant's (Elin) on purpose: the caller is
somebody else. Fixture text is generic, no personal data."""

import struct
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from voice_gateway.audio import frames  # noqa: E402

OUT = Path(__file__).resolve().parent
CLIPS = {
    "sv_booking": ("Astrid", "standard", "sv-SE", "Hej, jag skulle vilja boka ett bord för fyra personer på fredag klockan sju."),
    "sv_yes": ("Astrid", "standard", "sv-SE", "Ja, det stämmer."),
    "en_booking": ("Brian", "standard", "en-GB", "Hi, I would like to book a table for four people on Friday evening please."),
}


def write_mulaw_wav(path: Path, pcm8k: bytes) -> None:
    ulaw = frames.mulaw_encode(pcm8k)
    fmt = struct.pack("<HHIIHHH", 7, 1, 8000, 8000, 1, 8, 0)
    body = b"fmt " + struct.pack("<I", 18) + fmt + b"fact" + struct.pack("<II", 4, len(ulaw)) \
        + b"data" + struct.pack("<I", len(ulaw)) + ulaw + (b"\0" if len(ulaw) & 1 else b"")
    path.write_bytes(b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body)


def main() -> None:
    polly = boto3.client("polly", region_name="eu-central-1")
    for name, (voice, engine, lang, text) in CLIPS.items():
        resp = polly.synthesize_speech(Text=text, VoiceId=voice, Engine=engine, LanguageCode=lang,
                                       OutputFormat="pcm", SampleRate="8000")
        write_mulaw_wav(OUT / f"{name}.wav", resp["AudioStream"].read())
        print("wrote", name)


if __name__ == "__main__":
    main()

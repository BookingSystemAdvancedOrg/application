"""TTS (Polly request shape with a stub client, FakeTts) and the clip cache
(builder 'converse'). No network."""

import asyncio
import io

import pytest

from voice_gateway.speak.openers import MAX_OPENER_MS, OPENERS
from voice_gateway.audio.frames import rms_dbfs
from voice_gateway.backchannel import tokens as tk
from voice_gateway.backchannel.clips import MAX_BACKCHANNEL_MS, ClipCache, trim
from voice_gateway.config import BackchannelConfig, SpeakConfig
from voice_gateway.speak.fillers import FILLERS
from voice_gateway.speak.tts import FakeTts, NoVoice, PollyTts, TtsError, ssml, synthesize_with_retry, tone

CFG = SpeakConfig()


class StubPolly:
    def __init__(self, pcm=b"\x01\x00" * 8000, fail=0):
        self.requests, self.pcm, self.fail = [], pcm, fail

    def synthesize_speech(self, **req):
        self.requests.append(req)
        if self.fail:
            self.fail -= 1
            raise RuntimeError("throttled")
        return {"AudioStream": io.BytesIO(self.pcm)}


def run(coro):
    return asyncio.run(coro)


def test_polly_request_uses_the_native_voice_ssml_and_bookie():
    stub = StubPolly()
    audio = run(PollyTts(CFG, stub).synthesize("Välkommen till Booqys <test> & co.", "sv"))
    req = stub.requests[0]
    assert req["VoiceId"] == "Elin" and req["Engine"] == "neural" and req["OutputFormat"] == "pcm"
    assert req["SampleRate"] == "16000" and req["TextType"] == "ssml"
    assert req["Text"].startswith('<speak><prosody rate="100%">') and req["Text"].endswith("</prosody></speak>")
    assert '<sub alias="Bookies">Booqys</sub>' in req["Text"]
    assert "&lt;test&gt; &amp; co." in req["Text"]
    assert audio.voice == "Elin" and audio.duration_ms == 500 and len(audio.pcm) % 640 == 0


def test_polly_never_speaks_another_languages_voice():
    stub = StubPolly()
    with pytest.raises(NoVoice):
        run(PollyTts(CFG, stub).synthesize("salam", "fa"))
    assert stub.requests == []
    run(PollyTts(CFG, stub).synthesize("Hello", "en"))
    run(PollyTts(CFG, stub).synthesize("مرحبا", "ar"))
    voices = [r["VoiceId"] for r in stub.requests]
    assert voices[0] != "Elin" and voices[1] not in ("Elin", voices[0])


def test_polly_ssml_body_is_used_as_it_is():
    stub = StubPolly()
    run(PollyTts(CFG, stub).synthesize("Mm.", "sv", ssml_body=True))
    assert stub.requests[0]["Text"] == '<speak><prosody rate="100%">Mm.</prosody></speak>'
    assert ssml("a  b\n c", "92%") == '<speak><prosody rate="92%">a b c</prosody></speak>'


def test_polly_errors_become_tts_error_and_retry_counts_once():
    stub = StubPolly(fail=1)
    tts = PollyTts(CFG, stub)
    with pytest.raises(TtsError):
        run(tts.synthesize("Hej", "sv"))
    stub.fail = 1
    assert run(synthesize_with_retry(tts, CFG, "Hej", "sv")).duration_ms == 500
    assert len(stub.requests) == 3
    stub.fail = 5
    with pytest.raises(TtsError):
        run(synthesize_with_retry(tts, CFG, "Hej", "sv"))


def test_polly_stream_yields_whole_frames():
    stub = StubPolly(pcm=b"\x02\x00" * 8100)          # 506.25 ms: the tail is padded to frame 26

    async def main():
        return [c async for c in PollyTts(CFG, stub).stream("Hej då", "sv")]

    got = run(main())
    assert len(got) >= 2 and all(len(c) % 640 == 0 for c in got)
    assert sum(len(c) for c in got) == 26 * 640


def test_polly_stream_failure_before_audio_raises():
    async def main():
        return [c async for c in PollyTts(CFG, StubPolly(fail=1)).stream("Hej", "sv")]

    with pytest.raises(TtsError):
        run(main())


def test_fake_tts_is_deterministic_tone():
    tts = FakeTts()
    a = run(tts.synthesize("Hej", "sv"))
    assert a.duration_ms == 200 and abs(rms_dbfs(a.pcm) + 20) < 0.5
    b = run(tts.synthesize("x" * 20, "sv"))
    assert b.duration_ms == 1100 and b.pcm == run(tts.synthesize("y" * 20, "sv")).pcm
    assert tts.calls[0] == ("Hej", "sv")
    with pytest.raises(NoVoice):
        run(tts.synthesize("salam", "fa"))

    async def stream():
        return b"".join([c async for c in tts.stream("x" * 20, "sv")])

    assert run(stream()) == b.pcm
    tts.fail["boom"] = 1
    with pytest.raises(TtsError):
        run(tts.synthesize("boom", "sv"))
    assert run(tts.synthesize("boom", "sv")).duration_ms > 0


# --- clips -----------------------------------------------------------------------------------

def test_trim_cuts_silence_pads_and_fades():
    pcm = bytes(16000) + tone(300) + bytes(32000)       # 500 ms silence, 300 ms tone, 1 s silence
    out = trim(pcm)
    ms = len(out) // 32
    assert 300 <= ms <= 380 and len(out) % 640 == 0
    assert abs(int.from_bytes(out[0:2], "little", signed=True)) < 50     # faded in
    assert trim(bytes(6400)) == b"" and trim(b"") == b""


def test_warm_renders_tokens_and_fillers_with_gain():
    async def main():
        tts = FakeTts()
        cache = ClipCache(tts, CFG, BackchannelConfig())
        n = await cache.warm(("sv", "en", "fa"))
        return tts, cache, n

    tts, cache, n = run(main())
    openers = sum(len(OPENERS[x]) for x in ("sv", "en"))
    too_long = [k for k in cache.rejected if k.startswith("opener:")]      # FakeTts: 55 ms a letter
    expected = sum(len(tk.TOKENS[x]) + len(FILLERS.get(x, ())) for x in ("sv", "en")) + openers - len(too_long)
    assert n == expected and len(cache) == expected
    assert cache.opener("sv", "mm") is not None and cache.has_opener("en", "okay") and not cache.has_opener("fa", "mm")
    assert all(c.duration_ms <= MAX_OPENER_MS for k, c in cache._clips.items() if k.startswith("opener:"))
    mm = cache.backchannel("sv", "mm")
    assert mm.key == "bc:sv:mm" and 150 <= mm.duration_ms <= 450
    assert abs(rms_dbfs(mm.pcm[640:-640]) - (-20 + BackchannelConfig().clip_volume_db)) < 1.0
    filler = cache.filler("sv", 0)
    assert filler.key == "filler:sv:0" and abs(rms_dbfs(filler.pcm[640:-640]) + 20) < 1.0
    assert cache.backchannel("fa", "mm") is None and cache.filler("sv", 9) is None
    assert (tk.polly_text("sv", "mm"), "sv") in tts.calls and all(lang != "fa" for _, lang in tts.calls)


def test_disk_cache_avoids_tts_on_restart(tmp_path):
    async def warm(tts):
        cache = ClipCache(tts, CFG, BackchannelConfig(), cache_dir=tmp_path)
        await cache.warm(("sv",))
        return cache

    first = run(warm(FakeTts()))
    assert first.rendered > 0 and list(tmp_path.glob("*.pcm"))
    second_tts = FakeTts()
    second = run(warm(second_tts))
    assert second.rendered == 0 and second_tts.calls == []
    assert second.backchannel("sv", "mm").pcm == first.backchannel("sv", "mm").pcm


def test_failed_or_too_long_clips_are_left_out():
    async def main():
        tts = FakeTts()
        tts.fail[tk.polly_text("sv", "okej")] = 99
        slow = FakeTts(ms_per_char=400)                # "Precis." would be 2.8 s
        a = ClipCache(tts, CFG, BackchannelConfig())
        b = ClipCache(slow, CFG, BackchannelConfig())
        await a.warm(("sv",))
        await b.warm(("sv",))
        return a, b

    a, b = run(main())
    assert a.backchannel("sv", "okej") is None and a.backchannel("sv", "mm") is not None
    assert b.backchannel("sv", "precis") is None and "bc:sv:precis" in b.rejected
    assert all(c.duration_ms <= MAX_BACKCHANNEL_MS for c in [b.backchannel("sv", t) for t in ("mm", "ja")] if c)


def test_one_syllable_backchannels_are_cut_to_fit_short_pauses():
    from voice_gateway.backchannel.clips import BACKCHANNEL_TARGET_MS, shorten
    long = tone(400)
    out = shorten(long, BACKCHANNEL_TARGET_MS)
    assert len(out) // 32 == BACKCHANNEL_TARGET_MS and len(out) % 640 == 0
    assert abs(int.from_bytes(out[-2:], "little", signed=True)) < 200          # faded out, no click
    assert shorten(tone(100), BACKCHANNEL_TARGET_MS) == tone(100)

    async def main():
        cache = ClipCache(FakeTts(ms_per_char=150), CFG, BackchannelConfig())   # every clip long
        await cache.warm(("sv",))
        return cache
    cache = run(main())
    assert cache.backchannel("sv", "mm").duration_ms <= BACKCHANNEL_TARGET_MS     # one syllable: cut
    assert cache.backchannel("sv", "ja").duration_ms <= BACKCHANNEL_TARGET_MS

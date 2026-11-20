"""The amazon-transcribe side: result conversion, the LID patch, stream arguments
(fake client, no network), and the opt-in real streaming tests (listen builder)."""

import asyncio
import os

import pytest

from test_vg_listen_common import FIXTURES, pcm_frames, silence_pcm
from voice_gateway.config import SttConfig
from voice_gateway.events import SttFinal, SttPartial
from voice_gateway.listen import transcribe as tr
from voice_gateway.listen.transcribe import SttMode

sdk = pytest.importorskip("amazon_transcribe")
from amazon_transcribe.deserialize import TranscribeStreamingEventParser  # noqa: E402


def _event(results):
    return TranscribeStreamingEventParser()._parse_transcript_event({"Transcript": {"Results": results}})


def _items(words, stable=True, conf=0.9):
    out, t = [], 0.0
    for w in words:
        if w in ".,?!":
            out.append({"Type": "punctuation", "Content": w, "Stable": stable})
        else:
            out.append({"Type": "pronunciation", "Content": w, "StartTime": t, "EndTime": t + 0.3,
                        "Confidence": conf, "Stable": stable})
            t += 0.3
    return out


def test_partial_and_final_results_become_our_events():
    tr._install_lid_patch()
    items = _items(["Jag", "vill", "boka"])
    items[2]["Stable"] = False
    ev = _event([{"ResultId": "r1", "StartTime": 0.5, "EndTime": 1.4, "IsPartial": True,
                  "Alternatives": [{"Transcript": "Jag vill boka", "Items": items}]}])
    (p,) = [tr._convert(r, 7, SttMode(language="sv"), 1000) for r in ev.transcript.results]
    assert isinstance(p, SttPartial) and (p.text, p.stable_text, p.language, p.stream_id) == ("Jag vill boka", "Jag vill", "sv", 7)
    assert p.at_ms == 2400 and p.segment_id == "r1"
    assert p.last_word_ms == 1900                     # the words end at 0.9 s, the result covers audio to 1.4 s
    ev = _event([{"ResultId": "r1", "StartTime": 0.5, "EndTime": 1.8, "IsPartial": False,
                  "Alternatives": [{"Transcript": "Jag vill boka bord.", "Items": _items(["Jag", "vill", "boka", "bord", "."],
                                                                                      conf=0.8)}]}])
    (f,) = [tr._convert(r, 7, SttMode(language="sv"), 1000) for r in ev.transcript.results]
    assert isinstance(f, SttFinal) and f.text == "Jag vill boka bord." and f.start_ms == 1500 and f.end_ms == 2800
    assert f.confidence == pytest.approx(0.8) and f.language_scores == ()
    assert f.last_word_ms == 2200


def test_identify_language_scores_survive_the_sdk_parser():
    tr._install_lid_patch()
    tr._install_lid_patch()                                    # idempotent
    ev = _event([{"ResultId": "r", "StartTime": 0.0, "EndTime": 2.0, "IsPartial": False, "LanguageCode": "en-US",
                  "LanguageIdentification": [{"LanguageCode": "en-US", "Score": 0.91},
                                             {"LanguageCode": "sv-SE", "Score": 0.05},
                                             {"LanguageCode": "fa-IR", "Score": 0.01}],
                  "Alternatives": [{"Transcript": "hello there", "Items": _items(["hello", "there"])}]}])
    f = tr._convert(ev.transcript.results[0], 1, SttMode(identify=("sv", "en")), 0)
    assert f.language == "en" and f.language_scores == (("en", 0.91), ("sv", 0.05), ("fa", 0.01))


def test_empty_results_are_skipped():
    ev = _event([{"ResultId": "r", "StartTime": 0, "EndTime": 1, "IsPartial": True,
                  "Alternatives": [{"Transcript": "", "Items": []}]},
                 {"ResultId": "r2", "StartTime": 0, "EndTime": 1, "IsPartial": False, "Alternatives": []}])
    assert [tr._convert(r, 1, SttMode(language="sv"), 0) for r in ev.transcript.results] == [None, None]


class _FakeClient:
    def __init__(self, results=()):
        self.calls, self.sent, self.ended, self.results = [], [], False, list(results)

    async def start_stream_transcription(self, **kw):
        self.calls.append(kw)
        client = self

        class Input:
            async def send_audio_event(self, audio_chunk):
                client.sent.append(audio_chunk)

            async def end_stream(self):
                client.ended = True

        class Output:
            async def __aiter__(self):
                for r in client.results:
                    yield r

        class Stream:
            input_stream, output_stream = Input(), Output()
        return Stream()


def test_stream_arguments_for_fixed_and_identify_modes():
    async def main():
        c = _FakeClient()
        cfg = SttConfig(vocabulary_name="booqy-vocab")
        s1 = tr.TranscribeStream(c, cfg, SttMode(language="sv"), 1, 0)
        await s1.start()
        s2 = tr.TranscribeStream(c, cfg, SttMode(identify=("sv", "en", "fa"), preferred="sv"), 2, 0)
        await s2.start()
        return c.calls
    fixed, ident = asyncio.run(main())
    assert fixed["language_code"] == "sv-SE" and fixed["media_sample_rate_hz"] == 16000 and fixed["media_encoding"] == "pcm"
    assert fixed["enable_partial_results_stabilization"] is True and fixed["partial_results_stability"] == "high"
    assert fixed["vocabulary_name"] == "booqy-vocab" and "identify_language" not in fixed
    assert ident["language_code"] is None and ident["identify_language"] is True
    assert ident["language_options"] == ["sv-SE", "en-GB", "fa-IR"] and ident["preferred_language"] == "sv-SE"


def test_stream_rejects_unhearable_languages_and_one_language_identify():
    async def main():
        c = _FakeClient()
        with pytest.raises(ValueError):
            await tr.TranscribeStream(c, SttConfig(), SttMode(language="xx"), 1, 0).start()
        with pytest.raises(ValueError):
            await tr.TranscribeStream(c, SttConfig(), SttMode(identify=("sv",)), 1, 0).start()
    asyncio.run(main())


def test_audio_goes_out_in_60_ms_chunks_and_results_come_back_in_order():
    tr._install_lid_patch()
    res = _event([{"ResultId": "r", "StartTime": 0, "EndTime": 1, "IsPartial": True,
                   "Alternatives": [{"Transcript": "hej", "Items": _items(["hej"])}]},
                  {"ResultId": "r", "StartTime": 0, "EndTime": 1, "IsPartial": False,
                   "Alternatives": [{"Transcript": "Hej.", "Items": _items(["Hej", "."])}]}]).transcript.results

    async def main():
        c = _FakeClient([type("E", (), {"transcript": type("T", (), {"results": res})()})()])
        s = tr.TranscribeStream(c, SttConfig(), SttMode(language="sv"), 3, 500)
        await s.start()
        for f in pcm_frames(bytes(640 * 7)):
            await s.feed(f)
        await s.close()
        out = [e async for e in s.results()]
        return c, out
    c, out = asyncio.run(main())
    assert [len(x) for x in c.sent] == [1920, 1920, 640 * 1] and c.ended          # 3 frames, 3 frames, the rest on close
    assert [type(e).__name__ for e in out] == ["SttPartial", "SttFinal"] and out[1].start_ms == 500


def test_a_failing_output_stream_raises_from_results():
    class Boom(_FakeClient):
        async def start_stream_transcription(self, **kw):
            s = await super().start_stream_transcription(**kw)

            class Output:
                async def __aiter__(self):
                    raise ConnectionError("reset")
                    yield
            s.output_stream = Output()
            return s

    async def main():
        s = tr.TranscribeStream(Boom(), SttConfig(), SttMode(language="sv"), 1, 0)
        await s.start()
        with pytest.raises(ConnectionError):
            async for _ in s.results():
                pass
    asyncio.run(main())


class _PoolClient(_FakeClient):
    """Finishes its output when the audio ends, like the service."""

    async def start_stream_transcription(self, **kw):
        stream = await super().start_stream_transcription(**kw)
        ended = asyncio.Event()
        real_end = stream.input_stream.end_stream

        async def end_stream():
            await real_end()
            ended.set()
        stream.input_stream.end_stream = end_stream

        class Output:
            async def __aiter__(self):
                await ended.wait()
                return
                yield
        stream.output_stream = Output()
        return stream


def test_one_client_carries_one_stream_and_a_finished_clients_is_reused():
    async def main():
        made = []

        def make():
            made.append(_PoolClient())
            return made[-1]
        f = tr.TranscribeFactory(SttConfig(), client_factory=make)
        a = await f.open(SttMode(language="sv"), stream_id=1, audio_origin_ms=0)
        b = await f.open(SttMode(identify=("sv", "en")), stream_id=2, audio_origin_ms=0)    # needs its own client
        assert len(made) == 2
        await a.close()
        for _ in range(5):
            await asyncio.sleep(0)
        c = await f.open(SttMode(language="en"), stream_id=3, audio_origin_ms=0)          # takes a's finished client
        assert len(made) == 2
        d = await f.open(SttMode(language="de"), stream_id=4, audio_origin_ms=0)          # both busy: a third client
        assert len(made) == 3
        for s_ in (b, c, d):
            await s_.close()
    asyncio.run(main())


def test_a_failed_open_frees_its_client_and_warm_primes_spares():
    async def main():
        class Failing(_PoolClient):
            async def start_stream_transcription(self, **kw):
                raise ConnectionError("no route")
        made = []

        def make():
            made.append(Failing())
            return made[-1]
        f = tr.TranscribeFactory(SttConfig(), client_factory=make)
        with pytest.raises(ConnectionError):
            await f.open(SttMode(language="sv"), stream_id=1, audio_origin_ms=0)
        with pytest.raises(ConnectionError):
            await f.open(SttMode(language="sv"), stream_id=2, audio_origin_ms=0)
        assert len(made) == 1                                   # the same client again: it was freed

        ok = []

        def make_ok():
            ok.append(_PoolClient())
            return ok[-1]
        g = tr.TranscribeFactory(SttConfig(), client_factory=make_ok)
        await g.warm(2)
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(ok) == 2 and all(len(c.calls) == 1 and c.ended for c in ok)
        await f.warm(1)                                         # a failing warm-up never raises
    asyncio.run(main())


# --- opt-in: the real service (booqy-dev, a few seconds of audio) -------------------------------

AWS = pytest.mark.skipif(not os.environ.get("VG_AWS"), reason="set VG_AWS=1 (and AWS_PROFILE=booqy-dev) for live tests")


def _clip(name):
    from voice_gateway.audio.wav import read_wav
    return read_wav(FIXTURES / f"{name}.wav", 16000)


async def _stream_clip(mode, pcm, *, speed=1.0, tail_ms=1500):
    factory = tr.TranscribeFactory(SttConfig())
    stream = await factory.open(mode, stream_id=1, audio_origin_ms=0)
    got = []

    async def reader():
        async for ev in stream.results():
            got.append(ev)

    task = asyncio.create_task(reader())
    for f in pcm_frames(pcm + silence_pcm(tail_ms)):
        await stream.feed(f)
        await asyncio.sleep(0.02 / speed)
    await stream.close()
    await asyncio.wait_for(task, 20)
    return got


@pytest.mark.aws
@AWS
def test_live_swedish_streaming_in_eu_north_1():
    got = asyncio.run(_stream_clip(SttMode(language="sv"), _clip("sv_booking")))
    finals = [e for e in got if isinstance(e, SttFinal)]
    assert finals and any(isinstance(e, SttPartial) for e in got)
    text = " ".join(f.text for f in finals).lower()
    assert "bord" in text and "fyra" in text or "4" in text
    assert all(f.language == "sv" for f in finals)


@pytest.mark.aws
@AWS
def test_live_identify_language_scores_swedish_and_english():
    for name, expected in (("sv_booking", "sv"), ("en_booking", "en")):
        got = asyncio.run(_stream_clip(SttMode(identify=("sv", "en", "ar", "de", "pl"), preferred="sv"), _clip(name)))
        finals = [e for e in got if isinstance(e, SttFinal)]
        assert finals, name
        assert finals[-1].language == expected and finals[-1].language_scores[0][0] == expected, (name, finals[-1])
        assert finals[-1].language_scores[0][1] > 0.6


@pytest.mark.aws
@AWS
def test_live_faster_than_real_time_replay_is_accepted():
    got = asyncio.run(_stream_clip(SttMode(language="en"), _clip("en_booking"), speed=3.0))
    finals = [e for e in got if isinstance(e, SttFinal)]
    assert finals and "table" in " ".join(f.text for f in finals).lower()


@pytest.mark.aws
@AWS
def test_live_transcriber_probe_and_reopen_end_to_end():
    from voice_gateway.audio.buffers import RingBuffer
    from voice_gateway.clock import RealClock
    from voice_gateway.events import EventBus
    from voice_gateway.listen.transcribe import Transcriber

    async def main():
        clock, bus, ring = RealClock(), EventBus(), RingBuffer(12000)
        bus.history = []
        t = Transcriber(tr.TranscribeFactory(SttConfig()), ring, SttConfig(), bus, clock)
        await t.open("sv")
        for f in pcm_frames(_clip("en_booking") + silence_pcm(800)):
            ring.push(f)
            await t.feed(f)
            await asyncio.sleep(0.02)
        ev = await t.probe(("sv", "en"), replay_from_ms=0, timeout_ms=8000)
        await t.close()
        return ev, t.last_probe_scores
    ev, scores = asyncio.run(main())
    assert ev is not None and ev.code == "en" and ev.score > 0.6, scores


def test_a_chunk_for_a_cancelled_stream_is_kept_quietly_not_a_traceback():
    """Live 2026-11-20: awscrt printed InvalidStateError when a stream was cancelled while a chunk arrived."""
    pytest.importorskip("amazon_transcribe")
    from concurrent.futures import Future

    from amazon_transcribe.httpsession import AwsCrtHttpResponse
    from voice_gateway.listen.transcribe import _install_cancel_patch

    _install_cancel_patch()
    response = AwsCrtHttpResponse()
    gone, waiting = Future(), Future()
    gone.cancel()
    response._chunk_futures = [gone, waiting]
    response._on_body(b"one")                       # skips the cancelled future, hands the chunk on
    assert waiting.result() == b"one"
    late = Future()
    late.cancel()
    response._chunk_futures = [late]
    response._on_body(b"two")                       # nobody waits: kept, as the SDK does
    assert response._received_chunks == [b"two"]
    response._status_code_future.cancel()
    response._on_headers(200, [])                   # no InvalidStateError either
    assert response._headers_future.result() == []

"""The replay runner's own logic (builder 'harness'): how the caller is driven, what the fake recogniser is told, the
inline thread switch and the readiness probe. The caller driver runs against a stub line on a FakeClock, so none of
this needs the gateway, numpy or AWS."""

import asyncio

import pytest

from vg_harness import runner
from vg_harness import scenarios as sc
from vg_harness.caller_audio import PieceAudio, piece_label, silence_pcm
from voice_gateway.clock import FakeClock


class StubCaller:
    """LocalAdapter.caller on a FakeClock: say() takes the audio's duration, the waits are scripted."""

    def __init__(self, clock, reply_after_ms=0):
        self.clock, self.log = clock, []
        self.reply_after_ms = reply_after_ms

    async def say(self, pcm, *, label=""):
        self.log.append(("say", label, self.clock.now_ms()))
        await self.clock.sleep_ms(len(pcm) // 32)

    async def silence_ms(self, ms):
        self.log.append(("silence", ms, self.clock.now_ms()))
        await self.clock.sleep_ms(ms)

    async def wait_assistant_audio(self, min_ms=1, timeout_ms=15000):
        self.log.append(("wait_audio", min_ms, timeout_ms))
        await self.clock.sleep_ms(self.reply_after_ms)
        return self.clock.now_ms()

    async def wait_assistant_quiet(self, quiet_ms=700, timeout_ms=30000):
        self.log.append(("wait_quiet", quiet_ms))
        await self.clock.sleep_ms(quiet_ms)
        return self.clock.now_ms()

    async def hang_up(self):
        self.log.append(("hang_up", self.clock.now_ms()))


class StubAdapter:
    def __init__(self, clock, **kw):
        self.caller = StubCaller(clock, **kw)


def audio_for(scenario, ms_per_piece=300):
    return [[PieceAudio(piece_label(scenario.key, i, j), p.text, step.language, bytes(ms_per_piece * 32),
                        p.pause_after_ms, "synth") for j, p in enumerate(step.pieces)]
            for i, step in enumerate(scenario.steps)]


def drive(scenario, **kw):
    async def main():
        clock = FakeClock()
        adapter = StubAdapter(clock, **kw)
        driver = asyncio.create_task(clock.drive())
        segments = []
        try:
            await runner.drive_caller(scenario, adapter, clock, audio_for(scenario), segments=segments)
        finally:
            driver.cancel()
        return adapter.caller.log, segments

    return asyncio.run(main())


def test_hesitation_pauses_are_real_silence_between_labelled_pieces():
    log, segments = drive(sc.get("voice-A"))
    says = [e for e in log if e[0] == "say"]
    assert [s[1] for s in says] == ["voice-A/s0p0", "voice-A/s0p1", "voice-A/s0p2"]
    assert [e[1] for e in log if e[0] == "silence"] == [400, 1500]
    # piece 1 starts 300 (audio) + 400 (pause) after piece 0 began; piece 2 starts 300 + 1500 after piece 1
    assert [s.start_ms - segments[0].start_ms for s in segments] == [0, 700, 2500]
    assert [s.end_ms - s.start_ms for s in segments] == [300, 300, 300]
    assert log[-1][0] == "hang_up"


def test_the_first_step_waits_for_the_greeting_then_a_quiet_gap():
    log, _ = drive(sc.get("lang-A"), reply_after_ms=1200)
    kinds = [e[0] for e in log]
    assert kinds[:3] == ["wait_audio", "wait_quiet", "say"]
    assert ("wait_quiet", 700) in log


def test_every_step_waits_for_the_reply_before_speaking_again():
    log, _ = drive(sc.get("voice-C"))
    kinds = [e[0] for e in log]
    # three steps: each is wait_audio, wait_quiet, say; then the closing waits and the hang up
    assert kinds.count("say") == 3 and kinds.count("wait_audio") == 4 and kinds[-3:] == ["wait_audio", "wait_quiet", "hang_up"][-3:]
    assert ("wait_quiet", 1500) in log


def test_a_barge_in_step_talks_over_the_reply_after_its_offset():
    log, _ = drive(sc.get("voice-D"))
    waits = [e for e in log if e[0] == "wait_audio"]
    assert waits[1][1] == 1200                                  # voice-D step 2: during_reply after_ms=1200
    d = [i for i, e in enumerate(log) if e[0] == "wait_audio" and e[1] == 1200][0]
    assert log[d + 1][0] == "say"                               # and speaks at once, no quiet wait in between


def test_a_wait_that_times_out_does_not_abort_the_caller():
    class TimingOut(StubCaller):
        async def wait_assistant_audio(self, min_ms=1, timeout_ms=15000):
            raise asyncio.TimeoutError

    async def main():
        clock = FakeClock()
        adapter = StubAdapter(clock)
        adapter.caller = TimingOut(clock)
        driver = asyncio.create_task(clock.drive())
        try:
            return await runner.drive_caller(sc.get("lang-A"), adapter, clock, audio_for(sc.get("lang-A")))
        finally:
            driver.cancel()

    assert len(asyncio.run(main())) == 4


# --- what the fake recogniser is told --------------------------------------------------------------------------------

def test_lid_scores_are_clear_for_a_sentence_and_weak_for_a_word():
    strong = dict(runner.lid_scores("en", "Hi, I would like to book a table"))
    assert strong["en"] >= 0.9 and sum(strong.values()) <= 1.01
    weak = dict(runner.lid_scores("en", "Okay."))
    assert weak["en"] < 0.6          # rule 4: never switch on a short utterance
    assert dict(runner.lid_scores("fa", "x y z w"))["fa"] >= 0.9


def test_stt_script_has_one_line_per_piece_keyed_by_label():
    pytest.importorskip("voice_gateway.listen.transcribe")
    scenario = sc.get("voice-A")
    try:
        script = runner.stt_script(scenario)
    except ImportError as exc:
        pytest.skip(str(exc))
    assert [u.label for u in script] == ["voice-A/s0p0", "voice-A/s0p1", "voice-A/s0p2"]
    assert [u.text for u in script][1] == "ehm" and all(u.language == "sv" for u in script)


def test_inline_threads_runs_the_function_on_the_loop_and_restores():
    import threading
    real = asyncio.to_thread

    async def main():
        with runner.inline_threads():
            return await asyncio.to_thread(lambda: threading.current_thread().name)

    assert asyncio.run(main()) == threading.current_thread().name
    assert asyncio.to_thread is real


def test_recording_tts_records_text_language_and_voice():
    class Inner:
        async def synthesize(self, text, language, *, ssml_body=False):
            class A:
                voice = "Elin"
            return A()

        def stream(self, text, language):
            return "stream"

    tts = runner.RecordingTts(Inner())
    asyncio.run(tts.synthesize("Hej", "sv"))
    assert tts.calls == [("Hej", "sv", "Elin")] and tts.stream("Hej", "sv") == "stream"


def test_fake_config_uses_the_energy_vad_and_leaves_the_rest_alone():
    from voice_gateway.config import GatewayConfig
    cfg = runner.fake_config()
    assert cfg.vad.model == "energy" and GatewayConfig().vad.model == "silero"
    assert cfg.turn == GatewayConfig().turn and cfg.backchannel == GatewayConfig().backchannel


def test_missing_components_is_a_list_of_dotted_names():
    missing = runner.missing_components()
    assert isinstance(missing, list) and all(m.startswith("voice_gateway.") for m in missing)


def test_run_rejects_an_unknown_mode():
    with pytest.raises(ValueError):
        asyncio.run(runner.run(sc.get("voice-A"), "turbo"))


def test_driver_works_against_the_real_local_adapter_with_a_stand_in_gateway():
    """drive_caller on the real LocalAdapter (clean 16 kHz line, FakeClock): a stand-in gateway answers the call, plays a
    greeting and a short reply after the first caller words, and counts the caller frames until the caller hangs up."""
    from voice_gateway.audio.local import LocalAdapter

    scenario = sc.get("voice-F")

    async def main():
        clock = FakeClock()
        adapter = LocalAdapter(clock, paced=True)
        driver = asyncio.create_task(clock.drive())
        got = {"frames": 0, "loud": 0}

        async def gateway():
            from voice_gateway.audio.frames import Frame
            await adapter.accept()

            async def pump():                                    # the listener: consume the caller from the first frame
                async for frame in adapter.caller_frames():
                    got["frames"] += 1
                    got["loud"] += 1 if any(frame.pcm) else 0

            pumping = asyncio.create_task(pump())
            for i in range(50):                                  # a one second greeting
                await adapter.play(Frame(b"\x10\x10" * 320, i * 20))
                await clock.sleep_ms(20)
            await pumping

        task = asyncio.create_task(gateway())
        await asyncio.sleep(0)
        audio = audio_for(scenario, ms_per_piece=400)
        for step in audio:
            for piece in step:
                object.__setattr__(piece, "pcm", b"\x00\x10" * 6400)        # 400 ms of audible samples
        segments = []
        try:
            await asyncio.wait_for(runner.drive_caller(scenario, adapter, clock, audio, reply_timeout_ms=1500,
                                                       end_quiet_ms=200, segments=segments), 30)
            await asyncio.wait_for(task, 10)
        finally:
            driver.cancel()
        return segments, got, adapter

    segments, got, adapter = asyncio.run(main())
    assert [s.label for s in segments] == ["voice-F/s0p0", "voice-F/s1p0"]
    assert segments[1].start_ms > segments[0].end_ms and all(s.end_ms - s.start_ms >= 380 for s in segments)
    assert got["frames"] > 50 and got["loud"] >= 36                    # two 400 ms pieces reached the gateway as audio
    assert adapter.closed

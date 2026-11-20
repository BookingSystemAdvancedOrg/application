"""The frozen contracts of the voice gateway skeleton (DESIGN.md 3 and 5):
every module imports without the runtime-only libraries, the shared
modules (clock, events, config, languages, log, frames) behave, and the
builder interfaces keep their names. Pure stdlib + the application's own
deps, so it runs in root CI."""

import asyncio
import dataclasses
import importlib
import inspect
import json

import pytest

MODULES = (
    "voice_gateway", "voice_gateway.clock", "voice_gateway.events", "voice_gateway.config",
    "voice_gateway.languages", "voice_gateway.log", "voice_gateway.metrics", "voice_gateway.orchestrator",
    "voice_gateway.server",
    "voice_gateway.audio.frames", "voice_gateway.audio.resample", "voice_gateway.audio.buffers",
    "voice_gateway.audio.adapter", "voice_gateway.audio.local", "voice_gateway.audio.wav",
    "voice_gateway.listen.vad", "voice_gateway.listen.transcribe", "voice_gateway.listen.language_id",
    "voice_gateway.listen.turn", "voice_gateway.listen.barge_in", "voice_gateway.listen.pipeline",
    "voice_gateway.backchannel.tokens", "voice_gateway.backchannel.clips", "voice_gateway.backchannel.controller",
    "voice_gateway.speak.tts", "voice_gateway.speak.chunker", "voice_gateway.speak.queue",
    "voice_gateway.speak.mixer", "voice_gateway.speak.fillers", "voice_gateway.speak.speaker",
    "voice_gateway.brain.lambda_app", "voice_gateway.brain.voice_port", "voice_gateway.brain.streaming",
    "voice_gateway.brain.agent_brain",
)

# The interface names builders code against (DESIGN.md 5). A rename breaks the other builders.
INTERFACES = {
    "voice_gateway.audio.adapter": {"TelephonyAdapter": ["accept", "caller_frames", "play", "clear", "hangup",
                                                         "transfer", "closed"]},
    "voice_gateway.audio.local": {"LocalAdapter": ["accept", "caller_frames", "play", "clear", "hangup", "transfer",
                                                   "assistant_pcm", "save"],
                                  "CallerSide": ["say", "silence_ms", "wait_assistant_audio",
                                                 "wait_assistant_quiet", "hang_up"]},
    "voice_gateway.listen.vad": {"VadSegmenter": ["on_frame", "end_turn"]},
    "voice_gateway.listen.transcribe": {"Transcriber": ["open", "open_identify", "feed", "reopen", "probe", "close"]},
    "voice_gateway.listen.language_id": {"LanguageTracker": ["observe", "wants_probe", "follow"]},
    "voice_gateway.listen.turn": {"TurnDetector": ["on_vad", "on_partial", "on_final", "on_tick", "set_context",
                                                   "set_language", "threshold_ms", "reset"]},
    "voice_gateway.listen.barge_in": {"BargeInDetector": ["on_frame", "on_partial", "reset"]},
    "voice_gateway.listen.pipeline": {"Listener": ["run", "set_context", "follow_language", "turn_taken"]},
    "voice_gateway.backchannel.controller": {"BackchannelController": ["on_event", "on_tick"]},
    "voice_gateway.backchannel.clips": {"ClipCache": ["warm", "backchannel", "filler"]},
    "voice_gateway.speak.chunker": {"SentenceChunker": ["feed", "flush", "reset"]},
    "voice_gateway.speak.queue": {"SpeechQueue": ["enqueue", "ready", "cancel"]},
    "voice_gateway.speak.mixer": {"OutputMixer": ["run", "request_backchannel", "request_filler", "flush", "drain",
                                                  "assistant_playing", "assistant_item_started_ms",
                                                  "playback_dbfs_at", "busy"]},
    "voice_gateway.speak.speaker": {"Speaker": ["run", "say", "flush", "drain", "set_language", "busy"]},
    "voice_gateway.brain.streaming": {"StreamingAgentModel": ["_once", "converse"],
                                      "StreamBridge": ["open_turn", "close_turn", "round_started", "round_restarted",
                                                       "text_delta", "tool_started", "round_stopped"]},
    "voice_gateway.brain.agent_brain": {"AgentBrain": ["start", "turn", "set_language", "mark_not_heard",
                                                       "greeting_interrupted", "greeting_played", "transfer_result",
                                                       "finish", "fixed", "language"]},
    "voice_gateway.orchestrator": {"CallOrchestrator": ["run"]},
    "voice_gateway.metrics": {"MetricsRecorder": ["on_event", "summary"]},
}


@pytest.mark.parametrize("name", MODULES)
def test_module_imports_without_runtime_libraries(name):
    importlib.import_module(name)


@pytest.mark.parametrize("module", sorted(INTERFACES))
def test_interface_names(module):
    mod = importlib.import_module(module)
    for cls, members in INTERFACES[module].items():
        for member in members:
            assert hasattr(getattr(mod, cls), member), f"{module}.{cls}.{member}"


def test_streaming_model_keeps_the_agent_model_port():
    from shared.agent_model_real import RealAgentModel
    from voice_gateway.brain.streaming import StreamingAgentModel
    assert issubclass(StreamingAgentModel, RealAgentModel)
    assert StreamingAgentModel.converse is RealAgentModel.converse      # only _once is overridden


def test_gateway_voice_defers_speech_and_transfer():
    from voice_gateway.brain.voice_port import GatewayVoice
    assert GatewayVoice.inline_speech and GatewayVoice.deferred_tools


def test_call_ref_prefix_selects_the_aws_voice_languages():
    from shared.features.phone_agent import language
    from voice_gateway.config import BrainConfig
    from voice_gateway.languages import SPOKEN
    ref = BrainConfig().call_ref_prefix + "abc"
    assert language.call_languages(ref) == language.AWS_CALL_LANGUAGES
    assert set(language.AWS_CALL_LANGUAGES) == set(SPOKEN)


def test_lambda_app_path_points_at_the_phone_agent_call_module():
    from voice_gateway.brain import lambda_app
    assert (lambda_app.app_root() / "functions" / "phone-agent-call" / "app.py").is_file()


# --- clock -----------------------------------------------------------------------------------

def test_fake_clock_wakes_sleepers_in_order():
    from voice_gateway.clock import FakeClock

    async def main():
        clock, woke = FakeClock(), []

        async def sleeper(ms):
            await clock.sleep_ms(ms)
            woke.append((ms, clock.now_ms()))

        driver = asyncio.create_task(clock.drive())
        await asyncio.gather(sleeper(300), sleeper(20), sleeper(100))
        driver.cancel()
        return woke

    assert asyncio.run(main()) == [(20, 20), (100, 100), (300, 300)]


# --- events ----------------------------------------------------------------------------------

def test_bus_fans_out_by_type_and_never_blocks():
    from voice_gateway.events import BargeIn, EventBus, VadStart

    async def main():
        bus = EventBus(maxsize=2)
        bus.history = []
        seen = []
        bus.listen(seen.append, VadStart)
        sub = bus.subscribe(BargeIn)
        for i in range(3):
            bus.publish(BargeIn(i, 0, "duration"))
        bus.publish(VadStart(5, 4, 0.9))
        got = [await sub.get(), await sub.get()]
        return seen, got, bus.dropped, len(bus.history)

    seen, got, dropped, history = asyncio.run(main())
    assert [e.at_ms for e in seen] == [5]
    assert [e.at_ms for e in got] == [1, 2] and dropped == 1 and history == 4


def test_events_are_frozen_and_stamped():
    from voice_gateway import events
    for name, cls in inspect.getmembers(events, dataclasses.is_dataclass):
        if name == "ProviderAction":
            continue
        assert cls.__dataclass_params__.frozen, name
        assert "at_ms" in {f.name for f in dataclasses.fields(cls)}, name


# --- config ----------------------------------------------------------------------------------

def test_config_overrides_and_rejects_typos():
    from voice_gateway.config import GatewayConfig
    cfg = GatewayConfig.from_json(json.dumps({"turn": {"base_silence_ms": 600},
                                              "language": {"always_candidates": ["sv", "en"]}}))
    assert cfg.turn.base_silence_ms == 600 and cfg.language.always_candidates == ("sv", "en")
    assert cfg.backchannel.cooldown_ms == 4000
    with pytest.raises(ValueError):
        GatewayConfig.from_json(json.dumps({"turn": {"base_silense_ms": 1}}))
    with pytest.raises(ValueError):
        GatewayConfig.from_json(json.dumps({"turns": {}}))


def test_config_thresholds_match_the_multilingual_plan():
    from shared.features.phone_agent import language
    from voice_gateway.config import LanguageConfig
    cfg = LanguageConfig()
    assert (cfg.strong_p, cfg.accumulated_p, cfg.min_speech_ms, cfg.min_content_words) == (0.85, 0.6, 1500, 3)
    assert cfg.max_auto_switches == language.MAX_CONNECT_SWITCHES
    assert len(cfg.always_candidates) + len(cfg.default_extra_candidates) <= 5


# --- languages -------------------------------------------------------------------------------

def test_voice_table():
    from voice_gateway import languages
    assert languages.spec("sv").polly_voice == "Elin" and languages.spec("sv").transcribe == "sv-SE"
    assert languages.can_speak("ar") and not languages.can_speak("fa")
    assert {"fa", "bs", "id"} <= set(languages.HEARD_ONLY)
    assert languages.to_code("sv-SE") == "sv" and languages.to_code("ar-AE") == "ar"
    for code in languages.SPOKEN:
        assert languages.to_transcribe(code), code


# --- log -------------------------------------------------------------------------------------

def test_log_never_carries_text_or_numbers():
    from voice_gateway import log
    log.sink = []
    try:
        log.event("turn", text="jag heter Anna", caller="+46701234567", note="ring 0701234567", turn=3,
                  call=log.call_tag("call-1"))
        log.metric("EosToFirstAudio", 950, language="sv")
        record, metric = log.sink
    finally:
        log.sink = None
    assert "text" not in record and "caller" not in record and record["note"] == "ring #####"
    assert record["turn"] == 3 and len(record["call"]) == 12
    assert metric["EosToFirstAudio"] == 950 and metric["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["language"]]


# --- frames ----------------------------------------------------------------------------------

def test_frames_and_mulaw():
    from voice_gateway.audio import frames
    pcm = b"".join(int(v).to_bytes(2, "little", signed=True) for v in (0, 1000, -1000, 32000, -32000) * 64)
    back = frames.mulaw_decode(frames.mulaw_encode(pcm))
    assert len(back) == len(pcm)
    a = [int.from_bytes(pcm[i:i + 2], "little", signed=True) for i in range(0, len(pcm), 2)]
    b = [int.from_bytes(back[i:i + 2], "little", signed=True) for i in range(0, len(back), 2)]
    assert all(abs(x - y) <= max(16, abs(x) // 16) for x, y in zip(a, b))
    chunks = list(frames.frames_from_pcm(bytes(700), start_ms=40))
    assert [c.ts_ms for c in chunks] == [40, 60] and all(c.duration_ms == 20 for c in chunks)
    assert frames.TELEPHONY_MULAW_8K.frame_bytes == 160 and frames.PCM16_16K.frame_bytes == 640
    assert frames.rms_dbfs(bytes(640)) == -120.0


def test_lambda_app_loads_with_the_gateway_voice():
    from voice_gateway.brain import lambda_app
    from voice_gateway.brain.voice_port import GatewayVoice
    app = lambda_app.load()
    assert isinstance(app._clients["voice"], GatewayVoice)
    for name in ("start_call", "finish", "add_callback_for", "sms_sender", "_context", "updated_or", "invoker"):
        assert callable(getattr(app, name)), name

"""Runs one Scenario through the whole gateway. Owner: builder 'harness'.

run(scenario, mode) -> Result
  mode "fake":  FakeClock (fast), FakeSttFactory scripted from the steps,
                FakeTts, the real AgentBrain over the application's moto
                fixtures (tests/test_phone_agent_support.py: tables, the
                fake tool Lambdas) with shared.agent_model's simulator;
                free, deterministic, runs in root CI when its imports exist.
  mode "live":  RealClock (real-time pace), real Transcribe, Polly and
                Bedrock on profile booqy-dev (VG_HARNESS=live), the same
                moto tables and fake tool Lambdas (no dev data is written,
                nothing to clean up); costs cents per scenario.
Records: bus.history, LocalAdapter timeline and stereo WAV
(tests/out/<scenario>.wav, git-ignored), metrics summary, the fake tools'
calls (holds, bookings and their inputs), the AgentSentence texts.

How the caller is driven (`drive_caller`, shared by both modes): each Step
waits for its trigger, then every Piece is said through LocalAdapter's
CallerSide with the label `<scenario>/s<step>p<piece>` (the key of the fake
recogniser's script) and a real pause of `pause_after_ms` between pieces.
The timing is what the gateway sees from a real caller: nothing is cut out,
no piece is skipped, the pauses are real silence frames.

Fake mode in one sentence: the same orchestrator, listener, mixer, brain and
tools as a live call, with Transcribe replaced by a script (what each piece is
heard as), Polly by a tone and the LLM by the application's scripted
simulator; the application's blocking work (`asyncio.to_thread`) runs inline,
so virtual time stands still while the brain thinks (DESIGN.md 4).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import io
import os
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from vg_harness.scenarios import Scenario

OUT_DIR = Path(__file__).resolve().parents[1] / "out"
DEFAULT_LIVE_MODEL = "eu.anthropic.claude-haiku-5-5"      # the dev phone-agent-call Lambda's PHONE_AGENT_MODEL_ID (not a secret)
BEDROCK_REGION = "eu-north-1"
WATCHDOG_S = {"fake": 90, "live": 300}


@dataclass(frozen=True)
class Segment:
    """One piece of caller audio as it was sent (call clock)."""
    step: int
    piece: int
    label: str
    text: str
    language: str
    start_ms: int
    end_ms: int
    source: str = ""        # "cache" | "polly" | "synth"


@dataclass
class Result:
    scenario: str
    events: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    sentences: list[tuple[str, str]] = field(default_factory=list)   # (language, text)
    tool_calls: list[dict] = field(default_factory=list)
    wav_path: str | None = None
    mode: str = "fake"
    caller_segments: list[Segment] = field(default_factory=list)
    line_events: list = field(default_factory=list)
    tts_calls: list[tuple] = field(default_factory=list)             # (text, language, voice)
    callbacks: list[dict] = field(default_factory=list)
    reservations: list[dict] = field(default_factory=list)
    sms: list[dict] = field(default_factory=list)
    offered_times: set = field(default_factory=set)
    end_reason: str | None = None
    elapsed_s: float = 0.0
    error: str | None = None
    audio_sources: dict = field(default_factory=dict)


# --- what the fake recogniser is told ------------------------------------------------------------------------------

def lid_scores(language: str, text: str) -> tuple[tuple[str, float], ...]:
    """The identify-language scores the fake Transcribe reports for a piece: a clear winner for a real sentence
    (three or more words), a weak one for a short "Okay." (rule 4 of MULTILINGUAL.md must not switch on it)."""
    words = len(text.split())
    others = [c for c in ("sv", "en") if c != language] or ["sv"]
    if words >= 3:
        rest = round((1 - 0.93) / (len(others) + 1), 3)
        return ((language, 0.93),) + tuple((c, rest) for c in others) + (("ar" if language != "ar" else "de", rest),)
    return ((language, 0.55), (others[0], 0.35), (others[-1] if len(others) > 1 else "de", 0.10))


def stt_script(scenario: Scenario, audio=None):
    """One ScriptedUtterance per piece, keyed by the label the caller audio is sent with. With the rendered `audio`
    (prepare_audio), each line knows its audio's length, so the fake recogniser's partials follow the speech."""
    from vg_harness.caller_audio import piece_label
    from voice_gateway.listen.transcribe import ScriptedUtterance
    script = []
    for i, step in enumerate(scenario.steps):
        for j, piece in enumerate(step.pieces):
            duration = len(audio[i][j].pcm) // 32 if audio else None          # canonical PCM16 16 kHz
            script.append(ScriptedUtterance(piece_label(scenario.key, i, j), piece.text, step.language,
                                            lid_scores(step.language, piece.text), 0.9, duration))
    return script


def prepare_audio(scenario: Scenario, *, allow_network: bool = False, synth_fallback: bool = False):
    """The caller audio of every step: Polly clips from the cache (or Polly itself when allowed), the synthetic stand-in
    only when `synth_fallback` (fake mode)."""
    from vg_harness.caller_audio import step_audio
    return [step_audio(scenario.key, i, step, allow_network=allow_network, synth_fallback=synth_fallback)
            for i, step in enumerate(scenario.steps)]


# --- the caller ------------------------------------------------------------------------------------------------------

# A caller answers a reply, not an "mm": assistant audio counts as the reply once one utterance has played this long
# (longer than any backchannel clip, about 200 to 520 ms).
REPLY_MIN_MS = 700


async def _wait(coro) -> bool:
    """Runs a CallerSide wait; a timeout or a call that ended meanwhile is not an error here (the assertions say
    what was missing). Returns whether the wait completed."""
    try:
        await coro
        return True
    except (asyncio.TimeoutError, TimeoutError):
        return False


async def drive_caller(scenario: Scenario, adapter, clock, audio, *, reply_timeout_ms: int = 20000,
                       quiet_ms: int | None = None, end_quiet_ms: int = 1500,
                       segments: list[Segment] | None = None) -> list[Segment]:
    """Plays the scenario's caller: waits for each step's trigger, says every piece with its label and pauses, and
    hangs up after the last reply. Returns where every piece sat on the call clock. Works with any adapter that has
    LocalAdapter's `caller` (say, silence_ms, wait_assistant_audio, wait_assistant_quiet, hang_up)."""
    caller = adapter.caller
    segments = [] if segments is None else segments        # a list the caller keeps: partial results survive a cancel
    for i, step in enumerate(scenario.steps):
        if step.trigger == "during_reply":
            await _wait(caller.wait_assistant_audio(min_ms=max(1, step.after_ms), timeout_ms=reply_timeout_ms))
        elif step.trigger == "after_quiet":
            # the reply (or the greeting) first, then its end: a quiet gap before the reply even starts must not count
            await _wait(caller.wait_assistant_audio(min_ms=REPLY_MIN_MS, timeout_ms=reply_timeout_ms))
            await _wait(caller.wait_assistant_quiet(quiet_ms=quiet_ms if quiet_ms is not None else 700))
        for j, piece in enumerate(audio[i]):
            start = clock.now_ms()
            await caller.say(piece.pcm, label=piece.label)
            segments.append(Segment(i, j, piece.label, piece.text, piece.language, start, clock.now_ms(), piece.source))
            if piece.pause_after_ms and j < len(audio[i]) - 1:
                await caller.silence_ms(piece.pause_after_ms)
    await _wait(caller.wait_assistant_audio(min_ms=REPLY_MIN_MS, timeout_ms=reply_timeout_ms))
    await _wait(caller.wait_assistant_quiet(quiet_ms=end_quiet_ms))
    await caller.hang_up()
    return segments


# --- helpers shared by both modes ---------------------------------------------------------------------------------

@contextlib.contextmanager
def inline_threads():
    """asyncio.to_thread runs the function inline on the loop. Fake mode only: the application's blocking calls
    (DynamoDB on moto, the scripted model) take no virtual time, and a worker thread would let FakeClock jump ahead
    while the brain is still thinking."""
    real = asyncio.to_thread

    async def inline(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    asyncio.to_thread = inline
    try:
        yield
    finally:
        asyncio.to_thread = real


class RecordingTts:
    """Wraps a Tts and records (text, language, voice) of every synthesis (the native voice guard check)."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls: list[tuple] = []

    async def synthesize(self, text, language, *, ssml_body=False):
        audio = await self.inner.synthesize(text, language, ssml_body=ssml_body)
        self.calls.append((text, language, getattr(audio, "voice", None)))
        return audio

    def stream(self, text, language):
        self.calls.append((text, language, None))
        return self.inner.stream(text, language)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def fake_config(base=None):
    """Fake mode's gateway config: the energy VAD (the synthetic and cached caller audio is plainly voiced; the
    silero model is for live calls). Every other default stays the production value."""
    from voice_gateway.config import GatewayConfig
    cfg = base or GatewayConfig()
    return dataclasses.replace(cfg, vad=dataclasses.replace(cfg.vad, model="energy"))


def _spoken_languages(scenario: Scenario) -> tuple[str, ...]:
    from voice_gateway import languages
    seen = ["sv"] + [s.language for s in scenario.steps]
    return tuple(dict.fromkeys(c for c in seen if languages.can_speak(c)))


def _collect(result: Result, scenario: Scenario, world, adapter, tts, summary, segments, started: float) -> Result:
    from voice_gateway.events import AgentSentence
    result.events = list(summary.events or []) if summary is not None else []
    result.sentences = [(e.language, e.text) for e in result.events if isinstance(e, AgentSentence)]
    result.metrics = dataclasses.asdict(summary.metrics) if summary is not None and summary.metrics is not None else {}
    result.end_reason = getattr(summary, "end_reason", None)
    result.caller_segments = list(segments)
    result.line_events = list(adapter.timeline)
    result.tts_calls = list(tts.calls)
    result.tool_calls = list(world.invoke.log)
    result.reservations = list(world.invoke.reservations.values())
    result.offered_times = set(world.invoke.offered_times)
    result.callbacks = world.callbacks()
    result.sms = [c for c in world.lam.calls]
    result.elapsed_s = time.monotonic() - started
    return result


def save_wav(adapter, scenario: Scenario, mode: str, out_dir: Path | None = None) -> str | None:
    path = (out_dir or OUT_DIR) / f"{scenario.key}-{mode}.wav"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        adapter.save(str(path))
        return str(path)
    except NotImplementedError:
        return None


def missing_components() -> list[str]:
    """Which gateway parts still raise NotImplementedError (the other builders' work in progress): "module.Class.method".
    Empty: the whole call can run. Read from the source (AST), so no call is made."""
    import ast
    import importlib
    import inspect
    needed = {
        "voice_gateway.audio.local": {"LocalAdapter": ["accept", "caller_frames", "play", "clear", "hangup", "save"],
                                      "CallerSide": ["say", "silence_ms", "wait_assistant_audio", "wait_assistant_quiet",
                                                     "hang_up"]},
        "voice_gateway.listen.transcribe": {"FakeSttFactory": ["open"], "Transcriber": ["open", "feed"]},
        "voice_gateway.listen.pipeline": {"Listener": ["run"]},
        "voice_gateway.speak.tts": {"FakeTts": ["synthesize"]},
        "voice_gateway.speak.speaker": {"Speaker": ["run", "say"]},
        "voice_gateway.speak.mixer": {"OutputMixer": ["run"]},
        "voice_gateway.brain.agent_brain": {"AgentBrain": ["start", "turn"]},
        "voice_gateway.orchestrator": {"CallOrchestrator": ["run"]},
        "voice_gateway.metrics": {"MetricsRecorder": ["on_event", "summary"]},
        "voice_gateway.backchannel.clips": {"ClipCache": ["warm"]},
    }
    missing = []
    for module_name, classes in needed.items():
        module = importlib.import_module(module_name)
        for cls_name, methods in classes.items():
            cls = getattr(module, cls_name, None)
            for name in methods:
                fn = getattr(cls, name, None) if cls else None
                if fn is None:
                    missing.append(f"{module_name}.{cls_name}.{name} (absent)")
                    continue
                try:
                    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
                except (OSError, TypeError, SyntaxError):
                    continue
                body = [n for n in tree.body[0].body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
                if len(body) == 1 and isinstance(body[0], ast.Raise) and "NotImplementedError" in ast.dump(body[0]):
                    missing.append(f"{module_name}.{cls_name}.{name}")
    return missing


# --- fake mode -----------------------------------------------------------------------------------------------------

async def run_fake(scenario: Scenario, *, cfg=None, wire: Literal["mulaw", "pcm"] = "mulaw", out_dir: Path | None = None,
                   echo_db: float | None = None, noise_dbfs: float | None = None, on_adapter=None) -> Result:
    from voice_gateway.audio.frames import PCM16_16K, TELEPHONY_MULAW_8K
    from voice_gateway.audio.local import LocalAdapter
    from voice_gateway.backchannel.clips import ClipCache
    from voice_gateway.brain.agent_brain import AgentBrain
    from voice_gateway.clock import FakeClock
    from voice_gateway.listen.transcribe import FakeSttFactory
    from voice_gateway.orchestrator import CallOrchestrator, Components
    from voice_gateway.speak.tts import FakeTts
    from vg_harness.textrun import glosses_of
    from vg_harness.world import fake_world

    started = time.monotonic()
    result = Result(scenario.key, mode="fake")
    audio = prepare_audio(scenario, synth_fallback=True)
    result.audio_sources = {p.label: p.source for step in audio for p in step}
    cfg = fake_config(cfg)
    glosses, languages = glosses_of(scenario)
    clock = FakeClock()
    log = io.StringIO()
    with fake_world(scenario.tenant, now_ms=clock.now_ms, glosses=glosses, languages=languages) as world, \
            inline_threads(), contextlib.redirect_stdout(log):
        stt = FakeSttFactory(script=stt_script(scenario, audio))
        tts = RecordingTts(FakeTts())
        clips = ClipCache(FakeTts(), cfg.speak, cfg.backchannel, cache_dir=None)
        await clips.warm(_spoken_languages(scenario))
        adapter = LocalAdapter(clock, call_ref=f"harness-{scenario.key}", dialed=world.number, caller=scenario.caller,
                               wire_format=TELEPHONY_MULAW_8K if wire == "mulaw" else PCM16_16K, paced=True,
                               echo_db=echo_db, noise_dbfs=noise_dbfs)
        stt.labeller = adapter.label_at      # the script follows the caller audio's labels, not the energy segments
        if on_adapter:
            on_adapter(adapter)
        comps = Components(
            stt=stt, tts=tts, clips=clips, clock=clock, rng_seed=7, record_events=True,
            brain_factory=lambda c, bus, clk: AgentBrain(c, bus, clk, app=world.live, model_factory=lambda sink: world.model))
        orchestrator = CallOrchestrator(cfg, adapter, comps)
        driver = asyncio.create_task(clock.drive())
        segments: list[Segment] = []
        caller_task = asyncio.create_task(drive_caller(scenario, adapter, clock, audio, segments=segments))
        summary = None
        try:
            summary = await asyncio.wait_for(orchestrator.run(), WATCHDOG_S["fake"])
        except Exception as exc:     # the assertions report a failed call; the cause is kept
            result.error = f"{type(exc).__name__}: {exc}"
        finally:
            caller_task.cancel()
            driver.cancel()
            await asyncio.gather(caller_task, driver, return_exceptions=True)
        _collect(result, scenario, world, adapter, tts, summary, segments, started)
        result.wav_path = save_wav(adapter, scenario, "fake", out_dir)
    result.metrics["app_log_lines"] = len(log.getvalue().splitlines())
    return result


# --- live mode -----------------------------------------------------------------------------------------------------

def _live_clients():
    """Real Polly and Bedrock clients and frozen credentials of profile VG_AWS_PROFILE (default booqy-dev), made BEFORE
    the moto world starts (moto only mocks clients created while it is on). Credentials never leave this process."""
    import boto3
    from botocore.config import Config
    session = boto3.Session(profile_name=os.environ.get("VG_AWS_PROFILE", "booqy-dev"))
    frozen = session.get_credentials().get_frozen_credentials()
    kw = dict(aws_access_key_id=frozen.access_key, aws_secret_access_key=frozen.secret_key,
              aws_session_token=frozen.token)
    polly = boto3.client("polly", region_name="eu-central-1", **kw)
    bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION, config=Config(read_timeout=20), **kw)
    env = {"AWS_ACCESS_KEY_ID": frozen.access_key, "AWS_SECRET_ACCESS_KEY": frozen.secret_key,
           "AWS_SESSION_TOKEN": frozen.token or ""}
    return polly, bedrock, env


async def run_live(scenario: Scenario, *, cfg=None, wire: Literal["mulaw", "pcm"] = "mulaw", out_dir: Path | None = None,
                   echo_db: float | None = None, noise_dbfs: float | None = None, on_adapter=None) -> Result:
    """Real Transcribe, Polly and Bedrock (profile booqy-dev, tiny volumes: a scenario is one call of about 30 s), the
    fake world for the restaurant. Never run by CI. Needs the caller audio cached or VG_CALLER_NETWORK=1."""
    from voice_gateway.audio.frames import PCM16_16K, TELEPHONY_MULAW_8K
    from voice_gateway.audio.local import LocalAdapter
    from voice_gateway.backchannel.clips import ClipCache
    from voice_gateway.brain.agent_brain import AgentBrain
    from voice_gateway.brain.streaming import StreamingAgentModel
    from voice_gateway.clock import RealClock
    from voice_gateway.config import GatewayConfig
    from voice_gateway.listen.transcribe import TranscribeFactory
    from voice_gateway.orchestrator import CallOrchestrator, Components
    from voice_gateway.speak.tts import PollyTts
    from vg_harness.textrun import glosses_of
    from vg_harness.world import fake_world

    started = time.monotonic()
    result = Result(scenario.key, mode="live")
    audio = prepare_audio(scenario, allow_network=os.environ.get("VG_CALLER_NETWORK") == "1")
    result.audio_sources = {p.label: p.source for step in audio for p in step}
    cfg = cfg or GatewayConfig()
    polly, bedrock, creds = _live_clients()
    model_id = os.environ.get("PHONE_AGENT_MODEL_ID") or DEFAULT_LIVE_MODEL
    glosses, languages = glosses_of(scenario)
    clock = RealClock()
    with fake_world(scenario.tenant, now_ms=clock.now_ms, glosses=glosses, languages=languages,
                    real_credentials=creds) as world:
        tts = RecordingTts(PollyTts(cfg.speak, client=polly))
        clips = ClipCache(PollyTts(cfg.speak, client=polly), cfg.speak, cfg.backchannel, cache_dir=(out_dir or OUT_DIR) / "clips")
        await clips.warm(_spoken_languages(scenario))
        adapter = LocalAdapter(clock, call_ref=f"harness-{scenario.key}", dialed=world.number, caller=scenario.caller,
                               wire_format=TELEPHONY_MULAW_8K if wire == "mulaw" else PCM16_16K, paced=True,
                               echo_db=echo_db, noise_dbfs=noise_dbfs)
        if on_adapter:
            on_adapter(adapter)
        comps = Components(
            stt=TranscribeFactory(cfg.stt), tts=tts, clips=clips, clock=clock, rng_seed=7, record_events=True,
            brain_factory=lambda c, bus, clk: AgentBrain(
                c, bus, clk, app=world.live,
                model_factory=lambda sink: StreamingAgentModel(sink, c.brain, client=bedrock, model_id=model_id)))
        orchestrator = CallOrchestrator(cfg, adapter, comps)
        segments: list[Segment] = []
        caller_task = asyncio.create_task(drive_caller(scenario, adapter, clock, audio, reply_timeout_ms=25000,
                                                       segments=segments))
        summary = None
        try:
            summary = await asyncio.wait_for(orchestrator.run(), WATCHDOG_S["live"])
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        finally:
            caller_task.cancel()
            await asyncio.gather(caller_task, return_exceptions=True)
        _collect(result, scenario, world, adapter, tts, summary, segments, started)
        result.wav_path = save_wav(adapter, scenario, "live", out_dir)
    return result


async def run(scenario: Scenario, mode: Literal["fake", "live"] = "fake", **options) -> Result:
    if mode == "fake":
        return await run_fake(scenario, **options)
    if mode == "live":
        return await run_live(scenario, **options)
    raise ValueError(f"mode must be fake or live, not {mode!r}")

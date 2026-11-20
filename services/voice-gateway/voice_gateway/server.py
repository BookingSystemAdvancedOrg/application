"""Entry point. Owner: builder 'brain'.

  python -m voice_gateway.server local --caller caller.wav --out call.wav [--fake] [--dialed +46...]
      one call over LocalAdapter: the WAV is the caller (played once, then
      silence until the agent ends or 60 s), the stereo recording and a
      metrics JSON are written next to --out; --fake uses FakeSttFactory,
      FakeTts and a scripted brain (no AWS).
  python -m voice_gateway.server ws [--port 8765]
      WebSocketAdapter on 127.0.0.1 for manual tests.
  python -m voice_gateway.server serve
      Fargate (later, DESIGN.md 10): config from SSM, /health on :8080,
      the AWS phone adapter. Not built in this step: exits with 2.
Process start: GatewayConfig, ClipCache.warm for every languages.SPOKEN
code with a token or filler (never blocks accepting calls).

Every runtime piece (LocalAdapter, the listener, the speaker, Polly,
Transcribe) is imported lazily, so `--help` and the fake mode need none
of the runtime-only libraries.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from voice_gateway import languages, log
from voice_gateway.clock import RealClock
from voice_gateway.config import GatewayConfig

CALL_LIMIT_S = 60


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="voice_gateway.server", description="Booqy voice gateway")
    sub = p.add_subparsers(dest="mode", required=True)
    local = sub.add_parser("local", help="one call over LocalAdapter from a caller WAV")
    local.add_argument("--caller", required=True, help="caller audio, WAV (any rate, mono)")
    local.add_argument("--out", required=True, help="stereo recording to write (caller left, assistant right)")
    local.add_argument("--fake", action="store_true", help="FakeStt, FakeTts and a scripted brain: no AWS")
    local.add_argument("--dialed", default="+46000000000", help="the Booqy number the caller dials")
    local.add_argument("--transcript", default="", help="--fake: what the fake recognizer hears for the WAV")
    ws = sub.add_parser("ws", help="WebSocketAdapter on 127.0.0.1 for manual tests")
    ws.add_argument("--port", type=int, default=8765)
    ws.add_argument("--fake", action="store_true")
    sub.add_parser("serve", help="Fargate service (later)")
    return p


def components(cfg: GatewayConfig, clock, *, fake: bool, transcript: str = "", record: bool = False):
    """The Components of one call: fakes (no AWS) or the real AWS clients."""
    from voice_gateway.backchannel.clips import ClipCache
    from voice_gateway.orchestrator import Components
    if fake:
        from voice_gateway.brain.scripted import factory
        from voice_gateway.listen.transcribe import FakeSttFactory, ScriptedUtterance
        from voice_gateway.speak.tts import FakeTts
        tts = FakeTts()
        script = [ScriptedUtterance("caller", transcript, cfg.language.start_language)] if transcript else []
        return Components(FakeSttFactory(script), tts, ClipCache(tts, cfg.speak, cfg.backchannel), clock, factory(),
                          record_events=record)
    from voice_gateway.brain.agent_brain import AgentBrain
    from voice_gateway.listen.transcribe import TranscribeFactory
    from voice_gateway.speak.tts import PollyTts
    tts = PollyTts(cfg.speak)
    return Components(TranscribeFactory(cfg.stt), tts, ClipCache(tts, cfg.speak, cfg.backchannel), clock,
                      lambda c, bus, clk: AgentBrain(c, bus, clk), record_events=record)


async def warm(comps) -> None:
    """Pre-renders backchannel and filler clips; a failure never stops calls."""
    try:
        await comps.clips.warm(languages.SPOKEN)
    except Exception as exc:   # noqa: BLE001
        log.event("clips_warm_failed", level="warning", error=type(exc).__name__)


async def run_local(args, cfg: GatewayConfig) -> int:
    from voice_gateway.audio.local import LocalAdapter
    from voice_gateway.audio.wav import read_wav
    from voice_gateway.orchestrator import CallOrchestrator
    clock = RealClock()
    adapter = LocalAdapter(clock, dialed=args.dialed)
    comps = components(cfg, clock, fake=args.fake, transcript=args.transcript, record=True)
    await warm(comps)
    pcm = read_wav(args.caller)

    async def caller():
        await adapter.caller.wait_assistant_quiet()
        await adapter.caller.say(pcm, label="caller")
        await adapter.caller.silence_ms(CALL_LIMIT_S * 1000)
        await adapter.caller.hang_up()

    acting = asyncio.create_task(caller())
    summary = await asyncio.wait_for(CallOrchestrator(cfg, adapter, comps).run(), CALL_LIMIT_S + 30)
    acting.cancel()
    out = Path(args.out)
    adapter.save(str(out))
    out.with_suffix(".metrics.json").write_text(json.dumps(summary.metrics_summary, indent=2), "utf-8")
    log.event("local_call_done", reason=summary.end_reason)
    return 0


async def run_ws(args, cfg: GatewayConfig) -> int:
    from voice_gateway.audio.local import WebSocketAdapter
    from voice_gateway.orchestrator import CallOrchestrator
    clock = RealClock()
    server = WebSocketAdapter(clock, port=args.port)
    comps = components(cfg, clock, fake=args.fake)
    await warm(comps)
    async for adapter in server.serve():
        asyncio.create_task(CallOrchestrator(cfg, adapter, comps).run())
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    cfg = GatewayConfig.from_env()
    if args.mode == "serve":
        log.event("serve_not_built", level="warning")
        return 2
    runner = run_local if args.mode == "local" else run_ws
    return asyncio.run(runner(args, cfg))


if __name__ == "__main__":
    sys.exit(main())

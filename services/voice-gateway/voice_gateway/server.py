"""Entry point. Owner: builder 'brain'.

  python -m voice_gateway.server local --caller caller.wav --out call.wav [--fake] [--dialed +46...]
      one call over LocalAdapter: the WAV is the caller (played once, then
      silence until the agent ends or 60 s), the stereo recording and a
      metrics JSON are written next to --out; --fake uses FakeSttFactory,
      FakeTts and a scripted brain (no AWS).
  python -m voice_gateway.server ws [--port 8765]
      WebSocketAdapter on 127.0.0.1 for manual tests.
  python -m voice_gateway.server serve [--port 8080]
      the container's process (Fargate later, DESIGN.md 10): GET /health
      on :8080 (the Docker HEALTHCHECK) and the clip cache warmed for every
      spoken language. It answers no calls yet: the AWS phone adapter is
      the missing piece, so /health says "phoneAdapter": "missing" (still
      200: the process is healthy, there is just no line to take calls).
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
    serve = sub.add_parser("serve", help="the container process: /health, warm clips (no phone adapter yet)")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--host", default="0.0.0.0")   # noqa: S104  (inside the container, behind the task's security group)
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
    out.parent.mkdir(parents=True, exist_ok=True)
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


class Health:
    """What GET /health reports: the process state, never call content."""

    def __init__(self) -> None:
        self.clips = 0
        self.warm = "pending"
        self.calls = 0

    def body(self) -> dict:
        return {"status": "ok", "phoneAdapter": "missing", "clipsWarm": self.warm, "clips": self.clips,
                "activeCalls": self.calls}


async def health_server(health: Health, host: str, port: int) -> asyncio.AbstractServer:
    """A minimal HTTP/1.1 responder: GET /health -> 200 JSON, anything else 404. No framework needed."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readline(), 5)
            while (await asyncio.wait_for(reader.readline(), 5)) not in (b"\r\n", b"\n", b""):
                pass                                            # headers are not needed
            parts = request.decode("latin-1").split()
            ok = len(parts) >= 2 and parts[0] in ("GET", "HEAD") and parts[1].split("?")[0] == "/health"
            body = json.dumps(health.body() if ok else {"status": "not_found"}).encode()
            head = (f"HTTP/1.1 {'200 OK' if ok else '404 Not Found'}\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
            writer.write(head + (body if parts and parts[0] != "HEAD" else b""))
            await writer.drain()
        except Exception:   # noqa: BLE001  (a bad probe never takes the process down)
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, host, port)


async def run_serve(args, cfg: GatewayConfig, *, stop: asyncio.Event | None = None) -> int:
    health = Health()
    server = await health_server(health, args.host, args.port)
    log.event("serve_started", port=server.sockets[0].getsockname()[1], phone_adapter="missing")
    comps = components(cfg, RealClock(), fake=False)

    async def warm_clips() -> None:
        await warm(comps)
        health.clips, health.warm = len(comps.clips), "done"

    warming = asyncio.create_task(warm_clips())
    try:
        await (stop.wait() if stop is not None else asyncio.Future())
    finally:
        warming.cancel()
        server.close()
        await server.wait_closed()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    cfg = GatewayConfig.from_env()
    if args.mode == "serve":
        return asyncio.run(run_serve(args, cfg))
    runner = run_local if args.mode == "local" else run_ws
    return asyncio.run(runner(args, cfg))


if __name__ == "__main__":
    sys.exit(main())

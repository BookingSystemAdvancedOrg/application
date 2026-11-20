"""`python -m voice_gateway.replay <scenario>`: replays one scenario through the gateway and prints the timeline.
Owner: builder 'harness'. (voice_gateway/replay.py is the three line entry point; the code lives here, next to
the scenarios.)

  python -m voice_gateway.replay list
  python -m voice_gateway.replay voice-A                 fake mode (virtual clock, scripted Transcribe, no AWS)
  python -m voice_gateway.replay voice-D --verbose       also VAD pauses, partial transcripts, state changes
  python -m voice_gateway.replay lang-B --mode live      real Transcribe, Polly, Bedrock on booqy-dev (cents)
  python -m voice_gateway.replay all --summary           every scenario, one line each (exit 1 on any failure)
  python -m voice_gateway.replay voice-C --text          only the dialog logic, no audio (works before the gateway is built)
  python -m voice_gateway.replay voice-B --wire pcm --echo-db -12 --noise-dbfs -55
  python -m voice_gateway.replay --warm                  fill the caller audio cache from Polly (booqy-dev, once)

Exit status: 0 every check passed, 1 a check failed, 2 the call could not run.
The WAV (left caller, right assistant) goes to services/voice-gateway/tests/out/ (git-ignored).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import sys


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m voice_gateway.replay", description=__doc__.split("\n\n")[0])
    p.add_argument("scenario", nargs="?", help="a key (voice-A, lang-C, ...), 'all' or 'list'")
    p.add_argument("--mode", choices=("fake", "live"), default="fake")
    p.add_argument("--text", action="store_true", help="dialog logic only: the text of each step through the real agent")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--no-text", action="store_true", help="hide transcripts and sentences in the timeline")
    p.add_argument("--summary", action="store_true", help="one line per scenario")
    p.add_argument("--json", action="store_true", help="the checks and numbers as JSON")
    p.add_argument("--wire", choices=("mulaw", "pcm"), default="mulaw", help="the line: 8 kHz mu-law (default) or clean 16 kHz")
    p.add_argument("--echo-db", type=float, default=None, help="mix the assistant back into the caller side at this level")
    p.add_argument("--noise-dbfs", type=float, default=None, help="line noise level")
    p.add_argument("--out-dir", default=None, help="where the WAV goes")
    p.add_argument("--warm", action="store_true", help="render the caller audio cache from Polly and exit")
    p.add_argument("--sim-latency", action="store_true",
                   help="fake mode with the live costs on the virtual clock (Bedrock rounds, tool calls, Polly, the final)")
    p.add_argument("--config-json", default=None, help="GatewayConfig overrides as JSON (as VG_CONFIG_JSON)")
    return p


def _list() -> int:
    from vg_harness import scenarios as sc
    for s in sc.ALL:
        steps = "; ".join(f"{st.language}:{st.trigger}" for st in s.steps)
        print(f"{s.key:8} {s.title}  [{steps}]")
    return 0


def _text_mode(scenario) -> int:
    from vg_harness.textrun import run_text
    with contextlib.redirect_stdout(io.StringIO()):
        out = run_text(scenario)
    print(f"== {scenario.key}: {scenario.title}  [text]")
    for t in out.turns:
        print(f"  caller:    {t.caller}")
        print(f"  assistant: {t.reply}" + (f"   <{', '.join(a['name'] for a in t.actions)}>" if t.actions else ""))
    made = [c for c in out.tool_calls if c["fn"] == "create-pending-reservation" and c.get("status") == 201]
    print(f"  language {out.language}; reservations {len(made)}; callbacks {[c.get('topic') for c in out.callbacks]}")
    return 0


async def _one(scenario, args):
    from vg_harness import runner
    from vg_harness.asserts import check
    from vg_harness.timeline import render
    options = {"wire": args.wire, "echo_db": args.echo_db, "noise_dbfs": args.noise_dbfs}
    if args.config_json:
        from voice_gateway.config import GatewayConfig
        options["cfg"] = GatewayConfig.from_json(args.config_json)
    if args.sim_latency:
        if args.mode != "fake":
            raise SystemExit("--sim-latency is for fake mode")
        options["sim_latency"] = dict(runner.SIM_LATENCY)
    if args.out_dir:
        from pathlib import Path
        options["out_dir"] = Path(args.out_dir)
    result = await runner.run(scenario, args.mode, **options)
    failures = check(scenario, result)
    return result, failures, render


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    from vg_harness import scenarios as sc
    if args.warm:
        from vg_harness import caller_audio
        return caller_audio.main(["--warm"])
    if not args.scenario or args.scenario == "list":
        return _list() if args.scenario == "list" else (_parser().print_help() or 0)
    try:
        chosen = list(sc.ALL) if args.scenario == "all" else [sc.get(args.scenario)]
    except KeyError as exc:
        print(exc.args[0], file=sys.stderr)
        return 2
    if args.text:
        return max(_text_mode(s) for s in chosen)
    from vg_harness import runner
    waiting = runner.missing_components()
    if waiting and args.mode == "fake":
        print("The gateway is not complete yet; still raising NotImplementedError:", file=sys.stderr)
        for w in waiting:
            print(f"  {w}", file=sys.stderr)
        print("Use --text to check the dialog logic meanwhile.", file=sys.stderr)
        return 2
    status, rows = 0, []
    for s in chosen:
        result, failures, render = asyncio.run(_one(s, args))
        if result.error:
            status = max(status, 2)
        elif failures:
            status = max(status, 1)
        from vg_harness import analysis as an
        p50 = an.percentile(an.eos_to_first_audio(result.events), 0.5)
        reply50 = an.percentile(an.eos_to_reply_audio(result.events), 0.5)
        detect = [r.turn_detect for r in an.replies(result.events)]
        rows.append({"scenario": s.key, "pass": not failures and not result.error, "failures": failures,
                     "error": result.error, "eos_to_first_audio_p50_ms": p50, "eos_to_reply_audio_p50_ms": reply50,
                     "turn_detect_p50_ms": an.percentile(detect, 0.5), "openers": len(an.openers(result.events)),
                     "eos_to_first_audio_ms": an.eos_to_first_audio(result.events),
                     "eos_to_reply_audio_ms": an.eos_to_reply_audio(result.events), "turn_detect_ms": detect,
                     "switches": an.switches(result.events),
                     "backchannels": len(an.backchannels(result.events)),
                     "overlaps": an.backchannel_overlaps(result.events), "wav": result.wav_path})
        if args.summary or args.json:
            continue
        print(render(result, s, failures, show_text=not args.no_text, verbose=args.verbose))
        print()
    if args.json:
        print(json.dumps(rows, indent=2))
    elif args.summary:
        for r in rows:
            p50 = "-" if r["eos_to_first_audio_p50_ms"] is None else f"{r['eos_to_first_audio_p50_ms']:.0f} ms"
            reply = "-" if r["eos_to_reply_audio_p50_ms"] is None else f"{r['eos_to_reply_audio_p50_ms']:.0f} ms"
            detect = "-" if r["turn_detect_p50_ms"] is None else f"{r['turn_detect_p50_ms']:.0f} ms"
            print(f"{'PASS' if r['pass'] else 'FAIL':4}  {r['scenario']:8} p50 {p50:>8} (reply {reply:>8}, detect "
                  f"{detect:>7}, openers {r['openers']})  backchannels {r['backchannels']}"
                  f" overlaps {r['overlaps']}  switches {r['switches']}" + (f"  {r['error']}" if r["error"] else ""))
            for f in r["failures"]:
                print(f"        {f}")
        pooled = {k: [v for r in rows for v in r[k + "_ms"] if v is not None]
                  for k in ("eos_to_first_audio", "eos_to_reply_audio", "turn_detect")}
        if pooled["eos_to_first_audio"]:
            p = {k: an.percentile(v, 0.5) for k, v in pooled.items()}
            p95 = an.percentile(pooled["eos_to_first_audio"], 0.95)
            print(f"ALL   {len(pooled['eos_to_first_audio'])} turns: end of speech to first audio p50 "
                  f"{p['eos_to_first_audio']:.0f} ms (p95 {p95:.0f}), to the reply p50 {p['eos_to_reply_audio']:.0f} ms,"
                  f" turn detection p50 {p['turn_detect']:.0f} ms; openers {sum(r['openers'] for r in rows)}")
    return status


if __name__ == "__main__":
    sys.exit(main())

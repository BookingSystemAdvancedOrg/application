#!/usr/bin/env python3
"""Runs the phone agent evaluation (tests/phone_agent_scripts) and prints a
table per scenario and language: pass or fail per grader, median and p95
turn latency. No transcripts are written; replies are printed to this
terminal only with --verbose.

Simulator (no AWS, what CI runs):
    python scripts/phone_agent_eval.py

Real Bedrock in dev (Claude Haiku, a few US cents per call; the whole
matrix is about 120 calls, well under USD 10):
    AWS_PROFILE=booqy-dev python scripts/phone_agent_eval.py --live eu.anthropic.claude-haiku-5-5
    AWS_PROFILE=booqy-dev python scripts/phone_agent_eval.py --live eu.anthropic.claude-haiku-5-5 \\
        --langs sv,so,ru --scenarios 01,10 --verbose

Exit status 0 when every call passed every grader.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", metavar="MODEL_ID", help="Bedrock model id (needs AWS credentials for dev)")
    parser.add_argument("--langs", default="", help="comma separated call keys, e.g. sv,so or sv>so")
    parser.add_argument("--scenarios", default="", help="comma separated scenario numbers or ids, e.g. 01,10")
    parser.add_argument("--verbose", action="store_true", help="print every turn to this terminal")
    parser.add_argument("--workers", default="4", help="parallel calls (pytest-xdist), default 4")
    parser.add_argument("--tts-model", default="eleven_v4_turbo",
                        help="the voice model whose languages the call may switch to (PHONE_AGENT_TTS_MODEL); "
                             "default eleven_v4_turbo, the target (Somali); eleven_flash_v2_5 is today's default")
    args = parser.parse_args(argv)

    report = Path(tempfile.mkdtemp(prefix="phone-eval-")) / "report.jsonl"
    env = {**os.environ, "PHONE_AGENT_EVAL_REPORT": str(report), "PYTHONPATH": f"{ROOT}:{ROOT / 'tests'}",
           "PHONE_AGENT_EVAL_LANGS": args.langs, "PHONE_AGENT_EVAL_SCENARIOS": args.scenarios,
           "PHONE_AGENT_EVAL_TTS_MODEL": args.tts_model}
    selector = "test_scenario_on_the_simulator"
    if args.live:
        env["AGENT_MODEL_LIVE_TEST"] = args.live
        selector = "test_scenario_on_real_bedrock"
    if args.verbose:
        env["PHONE_AGENT_EVAL_VERBOSE"] = "1"
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(ROOT / "tests" /
               "test_phone_agent_eval.py"), "-k", selector]
    if args.verbose:
        command += ["-s", "-n", "0"]
    elif args.workers != "0":
        command += ["-n", args.workers]
    subprocess.run(command, cwd=ROOT, env=env, check=False)
    rows = [json.loads(line) for line in report.read_text(encoding="utf-8").splitlines()] if report.exists() else []
    if not rows:
        print("No calls ran.")
        return 1
    return print_table(rows)


def print_table(rows):
    graders = list(rows[0]["passed"])
    print()
    print(f"{'scenario':28} {'call':7} " + " ".join(f"{g:9}" for g in graders) + "  median   p95  failures")
    per_lang = defaultdict(lambda: [0, 0])
    for row in sorted(rows, key=lambda r: (r["scenario"], r["call"])):
        lat = sorted(row["latencyMs"]) or [0]
        p95 = lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))]
        marks = " ".join(f"{'pass' if row['passed'][g] else 'FAIL':9}" for g in graders)
        print(f"{row['scenario'][:28]:28} {row['call'][:7]:7} {marks}  {int(statistics.median(lat)):6} {p95:5}  "
              + ", ".join(row["failures"]))
        ok = all(row["passed"].values())
        per_lang[row["call"]][0] += ok
        per_lang[row["call"]][1] += 1
    print()
    print("per call language: " + ", ".join(f"{k} {a}/{b}" for k, (a, b) in sorted(per_lang.items())))
    total = sum(all(r["passed"].values()) for r in rows)
    print(f"all graders passed: {total}/{len(rows)}")
    return 0 if total == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())

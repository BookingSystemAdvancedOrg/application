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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from vg_harness.scenarios import Scenario


@dataclass
class Result:
    scenario: str
    events: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    sentences: list[tuple[str, str]] = field(default_factory=list)   # (language, text)
    tool_calls: list[dict] = field(default_factory=list)
    wav_path: str | None = None


async def run(scenario: Scenario, mode: Literal["fake", "live"] = "fake") -> Result:
    raise NotImplementedError

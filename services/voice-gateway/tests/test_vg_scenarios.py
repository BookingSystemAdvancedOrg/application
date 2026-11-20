"""Scenarios voice A to F and language A to H through the whole gateway
(builder 'harness').

Fake mode runs by default: the real orchestrator, listener, mixer and brain on a virtual clock with the scripted
Transcribe, a tone for Polly and the application's scripted agent model, so it is free, deterministic and part of root
CI. It needs moto (the application's dev dependency) and every gateway component to be implemented: while another
builder's part still raises NotImplementedError the scenarios skip and say which one (`runner.missing_components`),
and start running by themselves the moment the last part lands. The mu-law line needs numpy (the resampler); without
it the line is plain 16 kHz PCM.

VG_HARNESS=live runs them against AWS (booqy-dev, cents): real Transcribe, Polly and Bedrock, caller audio from the
committed cache. VG_HARNESS=off skips them. `python -m voice_gateway.replay <scenario>` prints the same run as a
timeline.
"""

import asyncio
import importlib.util
import os

import pytest

from vg_harness.scenarios import ALL

MODE = os.environ.get("VG_HARNESS", "fake")

pytestmark = pytest.mark.skipif(MODE not in ("fake", "live"), reason="VG_HARNESS=off")


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.key)
def test_scenario(scenario):
    pytest.importorskip("moto")
    from vg_harness import runner
    from vg_harness.asserts import check
    from vg_harness.timeline import render
    waiting = runner.missing_components()
    if waiting and MODE == "fake":
        pytest.skip("gateway not complete yet: " + ", ".join(waiting[:4]) + (" ..." if len(waiting) > 4 else ""))
    wire = "mulaw" if importlib.util.find_spec("numpy") else "pcm"
    result = asyncio.run(runner.run(scenario, MODE, wire=wire))
    failures = check(scenario, result)
    assert failures == [], "\n" + render(result, scenario, failures)
